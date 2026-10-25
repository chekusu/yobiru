#!/usr/bin/env python3
"""Yobiru private resource service: owner-approved text exports for one bot, released per caller.

The owner puts deliberate snapshots here (`add ID --file F` copies the text; nothing links back to the
original file). An agent answering a started job can ask this service, over loopback with an access token, for
one export by id together with the job's capability ticket. The service redeems the ticket at the gateway with
its own per-bot verifier credential and releases the export only if the gateway says that this job's caller
may read `resource:<id>` right now (current grant generation, current binding, live lease). Authorization is
asked again after the export was read and before it is released; both answers must describe the same job,
caller and generation. Ids are 1-32 of a-z, 0-9, _ and -: there is no path, URL, pattern or "all" request,
and the export file is opened without following symbolic links.

Same-user processes are an interface boundary, not an OS sandbox: an agent that runs as the same OS user
could read this directory itself. Run the service under another account or in a container (or on another
machine) when that matters (docs/PUBLIC_BOTS.md).

  bot_resources.py init --dir DIR --gateway URL --bot BOT_ID
  bot_resources.py add --dir DIR ID --file FILE [--title TITLE]
  bot_resources.py remove --dir DIR ID
  bot_resources.py list --dir DIR                 (ids, titles, sizes, hashes; never content)
  bot_resources.py serve --dir DIR [--port N]     (always 127.0.0.1)
Python 3.8+, stdlib only.
"""
import argparse
import hashlib
import hmac
import json
import os
import re
import stat
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import bot_common as C  # noqa: E402

SERVICE_KEYS = {"v", "gateway", "bot_id", "verifier_token_file", "access_token_hash"}
MAX_EXPORT_BYTES = 64 * 1024
GRANT_IDENTITY = ("bot_id", "message_id", "conversation_id", "subject", "generation", "capability", "resource")


def log(*parts):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *parts, file=sys.stderr, flush=True)


def die(msg, code=1):
    print("bot_resources: error: " + msg, file=sys.stderr)
    sys.exit(code)


class Refuse(Exception):
    """A fixed, data-free refusal code."""

    def __init__(self, status, code):
        Exception.__init__(self, code)
        self.status, self.code = status, code


def export_text(raw):
    """Owner-supplied export bytes -> text, or a reason: UTF-8, bounded, no NUL or other control characters
    except newline and tab."""
    if len(raw) > MAX_EXPORT_BYTES:
        return None, "larger than %d bytes" % MAX_EXPORT_BYTES
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, "not UTF-8 text"
    err = C.text_error(text, MAX_EXPORT_BYTES, multiline=True)
    if err:
        return None, err
    return text, None


class Service(object):
    def __init__(self, directory):
        # resolved once: links above the folder (macOS /var, /tmp) are its real path; one swapped in later is refused
        self.dir = Path(os.path.realpath(str(Path(directory).expanduser())))
        raw = C.read_json_file(self.dir / "service.json", private=True, what="service config")
        if not isinstance(raw, dict) or set(raw) != SERVICE_KEYS or raw["v"] != 1:
            raise ValueError("service.json: unexpected fields or version")
        self.gateway = C.normalize_base_url(raw["gateway"])
        if not (isinstance(raw["bot_id"], str) and C.ID_RE.match(raw["bot_id"]) and raw["bot_id"].startswith("bot_")):
            raise ValueError("service.json: bot_id")
        if not isinstance(raw["access_token_hash"], str) or not C.HASH_RE.match(raw["access_token_hash"]):
            raise ValueError("service.json: access_token_hash")
        self.bot_id, self.access_hash = raw["bot_id"], raw["access_token_hash"]
        self.verifier_file = self.dir / raw["verifier_token_file"]
        self.exports = self.dir / "exports"
        self.lock = threading.Lock()

    def export_path(self, rid):
        if not isinstance(rid, str) or not C.RESOURCE_ID_RE.match(rid):
            raise Refuse(400, "invalid_resource")
        return self.exports / ("%s.json" % rid)

    def read_export(self, rid):
        """The stored export, opened without following links; anything unexpected is `not_found`."""
        path = self.export_path(rid)
        if os.path.realpath(str(self.exports)) != str(self.exports):
            raise Refuse(503, "not_configured")
        try:
            fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError:
            raise Refuse(404, "not_found")
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_size > 4 * MAX_EXPORT_BYTES:
                raise Refuse(404, "not_found")
            with os.fdopen(fd, "rb") as f:
                fd = None
                data = C.strict_loads(f.read())
        except (OSError, C.StrictJSONError):
            raise Refuse(404, "not_found")
        finally:
            if fd is not None:
                os.close(fd)
        if not isinstance(data, dict) or data.get("id") != rid or not isinstance(data.get("content"), str):
            raise Refuse(404, "not_found")
        return data

    def redeem(self, verifier, ticket, rid):
        try:
            status, grant = C.api_call(self.gateway, "POST", "/v1/capability/redeem",
                                       {"ticket": ticket, "capability": "resource:" + rid}, token=verifier, timeout=10)
        except (C.ApiError, ValueError):
            raise Refuse(503, "gateway_unreachable")
        if status != 200 or grant.get("ok") is not True:
            raise Refuse(403, "not_authorized")
        if grant.get("bot_id") != self.bot_id or grant.get("capability") != "resource:" + rid or \
                grant.get("resource") != rid or not isinstance(grant.get("message_id"), str) or \
                not isinstance(grant.get("subject"), str) or not isinstance(grant.get("generation"), int):
            raise Refuse(403, "not_authorized")
        return {k: grant.get(k) for k in GRANT_IDENTITY}

    def release(self, ticket, rid):
        """authorize -> read -> authorize again -> read again. The export is released only if both answers describe
        the same job and caller and the export still exists unchanged after the second authorization: an owner's
        remove or replacement that completed before that point releases nothing stale. Delivery after this point
        cannot be recalled."""
        try:
            verifier = C.read_token_file(self.verifier_file, "verifier")
        except ValueError:
            raise Refuse(503, "not_configured")
        with self.lock:
            grant = self.redeem(verifier, ticket, rid)
            data = self.read_export(rid)
            if self.redeem(verifier, ticket, rid) != grant:          # changed while reading
                raise Refuse(403, "not_authorized")
            if self.read_export(rid) != data:                       # removed meanwhile: 404 not_found
                raise Refuse(409, "resource_changed")             # any field replaced, title included
        log("released resource=%s message=%s subject=%s" % (rid, grant["message_id"], grant["subject"]))
        return {"resource": rid, "title": data.get("title"), "sha256": data.get("sha256"),
                "content": data["content"], "as_of": data.get("added_at")}


def make_handler(service):
    class Handler(BaseHTTPRequestHandler):
        server_version = "yobiru-resources"
        sys_version = ""
        timeout = 10

        def version_string(self):
            return self.server_version

        def log_message(self, fmt, *args):
            log("%s %s" % (self.client_address[0], (fmt % args)[:120]))

        def send_error(self, code, message=None, explain=None):
            self.reply(code, {"ok": False, "code": "bad_request"})

        def reply(self, status, obj):
            body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

        def do_GET(self):
            if self.path == "/v1/health":
                return self.reply(200, {"ok": True, "service": "yobiru-resources"})
            self.reply(404, {"ok": False, "code": "not_found"})

        def do_POST(self):
            try:
                if self.path != "/v1/resource":
                    raise Refuse(404, "not_found")
                auth = self.headers.get("Authorization") or ""
                token = auth[7:].strip() if auth.startswith("Bearer ") else ""
                if C.token_kind(token) != "access" or \
                        not hmac.compare_digest(C.token_hash(token), service.access_hash):
                    raise Refuse(401, "unauthorized")
                try:
                    n = int(self.headers.get("Content-Length") or "-1")
                except ValueError:
                    n = -1
                if not 0 < n <= 4096 or self.headers.get("Transfer-Encoding"):
                    raise Refuse(400, "bad_request")
                try:
                    body = C.strict_loads(self.rfile.read(n))
                except C.StrictJSONError:
                    raise Refuse(400, "bad_request")
                if not isinstance(body, dict) or set(body) != {"ticket", "resource"} or \
                        C.token_kind(body["ticket"]) != "ticket":
                    raise Refuse(400, "bad_request")
                self.reply(200, dict(service.release(body["ticket"], body["resource"]), ok=True))
            except Refuse as r:
                self.reply(r.status, {"ok": False, "code": r.code})
            except Exception as e:
                log("internal error:", type(e).__name__)
                self.reply(500, {"ok": False, "code": "internal_error"})

    return Handler


# ---------------------------------------------------------------- commands
def service_dir(value):
    d = Path(value).expanduser().absolute()
    if not (d / "service.json").is_file():
        die("%s is not a resource service directory (run: init)" % d)
    return d


def cmd_init(a):
    os.umask(0o077)
    d = Path(a.dir).expanduser().absolute()
    if (d / "service.json").exists():
        die("%s already has service.json; nothing was changed" % d)
    try:
        gateway = C.normalize_base_url(a.gateway)
    except ValueError as e:
        die("--gateway: %s" % e, 2)
    if not (C.ID_RE.match(a.bot) and a.bot.startswith("bot_")):
        die("--bot must be a bot id (bot_...)", 2)
    C.ensure_private_dir(d)
    C.ensure_private_dir(d / "exports")
    token = C.new_token("access")
    C.create_private_file(d / "access.token", token + "\n")
    C.create_private_file(d / "service.json", json.dumps({
        "v": 1, "gateway": gateway, "bot_id": a.bot, "verifier_token_file": "verifier.token",
        "access_token_hash": C.token_hash(token)}, indent=2) + "\n")
    print(json.dumps({"ok": True, "dir": str(d), "access_token_file": str(d / "access.token"),
                      "verifier_token_file": str(d / "verifier.token"),
                      "next": ["create the verifier credential: yobiru public verifier bind BOT --token-file %s"
                               % (d / "verifier.token"),
                               "add exports: resource-service add --dir %s ID --file FILE" % d,
                               "offer them: yobiru public bot edit BOT --offered ...,resource:ID and grant them",
                               "start: resource-service serve --dir %s" % d]}, indent=2))
    return 0


def cmd_add(a):
    os.umask(0o077)
    d = service_dir(a.dir)
    if not C.RESOURCE_ID_RE.match(a.id):
        die("ID: 1-32 of a-z, 0-9, _ and -, starting with a letter or digit", 2)
    title = a.title or a.id
    err = C.text_error(title, 120)
    if err:
        die("--title %s" % err, 2)
    source = Path(a.file).expanduser()
    try:
        st = os.lstat(str(source))
    except OSError:
        die("%s does not exist" % source)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        die("%s must be a regular file, not a link" % source)
    text, problem = export_text(source.read_bytes())
    if problem:
        die("%s: %s" % (source, problem))
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    C.ensure_private_dir(d / "exports")
    C.write_private_json(d / "exports" / ("%s.json" % a.id), {
        "v": 1, "id": a.id, "title": C.clean_label(title, 120), "sha256": digest, "bytes": len(text.encode("utf-8")),
        "content": text, "added_at": C.iso(C.now_ts())})
    print(json.dumps({"ok": True, "resource": a.id, "capability": "resource:" + a.id, "sha256": digest,
                      "note": "a copy: later changes to the original file are not exported until you add it again"},
                     indent=2))
    return 0


def cmd_remove(a):
    d = service_dir(a.dir)
    if not C.RESOURCE_ID_RE.match(a.id):
        die("invalid ID", 2)
    path = d / "exports" / ("%s.json" % a.id)
    existed = os.path.lexists(str(path))
    if existed:
        os.unlink(str(path))
    print(json.dumps({"ok": True, "resource": a.id, "removed": existed}, indent=2))
    return 0


def cmd_list(a):
    d = service_dir(a.dir)
    rows = []
    for p in sorted((d / "exports").glob("*.json")):
        try:
            data = C.read_json_file(p, private=True, what="export")
        except ValueError:
            continue
        rows.append({"resource": data.get("id"), "title": data.get("title"), "bytes": data.get("bytes"),
                     "sha256": data.get("sha256"), "added_at": data.get("added_at")})
    print(json.dumps({"ok": True, "exports": rows}, indent=2))
    return 0


def cmd_serve(a):
    os.umask(0o077)
    try:
        service = Service(service_dir(a.dir))
    except ValueError as e:
        die(str(e))
    srv = C.NumericHTTPServer(("127.0.0.1", a.port or 0), make_handler(service))
    srv.daemon_threads = True
    print(json.dumps({"listening": "http://127.0.0.1:%d" % srv.server_address[1], "service": "yobiru-resources"}),
          flush=True)
    log("resource service for %s on 127.0.0.1:%d" % (service.bot_id, srv.server_address[1]))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bot_resources", description="private owner-approved resource exports")
    sp = ap.add_subparsers(dest="cmd")
    sp.required = True
    p = sp.add_parser("init")
    p.add_argument("--dir", required=True)
    p.add_argument("--gateway", required=True)
    p.add_argument("--bot", required=True)
    p = sp.add_parser("add")
    p.add_argument("--dir", required=True)
    p.add_argument("id")
    p.add_argument("--file", required=True)
    p.add_argument("--title")
    for name in ("remove",):
        p = sp.add_parser(name)
        p.add_argument("--dir", required=True)
        p.add_argument("id")
    p = sp.add_parser("list")
    p.add_argument("--dir", required=True)
    p = sp.add_parser("serve")
    p.add_argument("--dir", required=True)
    p.add_argument("--port", type=int, default=0)
    a = ap.parse_args(argv)
    return {"init": cmd_init, "add": cmd_add, "remove": cmd_remove, "list": cmd_list, "serve": cmd_serve}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
