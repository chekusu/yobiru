"""Public bot contact tests: gateway and calendar services as isolated subprocesses on ephemeral loopback
ports, with synthetic homes only. Nothing here reads ~/.tailmsg, a real calendar, Tailscale or the network
beyond 127.0.0.1."""
import json
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

from support.pkg import REPO, free_port, python_env

PUBLIC = REPO / "public_bots"
if str(PUBLIC) not in sys.path:
    sys.path.insert(0, str(PUBLIC))
import bot_common as C  # noqa: E402


def cli(home, *args):
    """bot_client in this process: -> (exit code, parsed JSON stdout or text, stderr). Never for `mcp`, which
    rebinds file descriptor 1: run that one as a subprocess."""
    import io
    from contextlib import redirect_stderr, redirect_stdout
    import bot_client
    out, err, code = io.StringIO(), io.StringIO(), 0
    with redirect_stdout(out), redirect_stderr(err):
        try:
            bot_client.main(["--home", str(home)] + list(args))
        except SystemExit as e:
            code = e.code or 0
    text = out.getvalue()
    try:
        return code, json.loads(text), err.getvalue()
    except ValueError:
        return code, text, err.getvalue()


def plain_env(home):
    # Linux setup-python builds need their own shared libpython, while HOME and
    # all other test settings remain isolated from the user's configuration.
    return python_env({"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8",
                       "LC_ALL": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"})


class Service(object):
    """A long-running subprocess that prints one JSON line {"listening": URL} once it serves."""

    def __init__(self, argv, env, log_path):
        self.argv, self.env, self.log_path = list(argv), env, Path(log_path)
        self.proc, self.url = None, None

    def start(self, timeout=15):
        log = self.log_path.open("a")
        self.proc = subprocess.Popen(self.argv, stdout=subprocess.PIPE, stderr=log, env=self.env, text=True,
                                     start_new_session=True)
        log.close()
        ready, _, _ = select.select([self.proc.stdout], [], [], timeout)
        line = self.proc.stdout.readline() if ready else ""
        if not line:
            self.kill()
            raise AssertionError("service did not start: %s" % self.log_path.read_text()[-2000:])
        self.url = json.loads(line)["listening"]
        return self

    def _signal(self, sig):
        if self.proc is None:
            return
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, sig)
            except OSError:
                pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(self.proc.pid, signal.SIGKILL)
            self.proc.wait(timeout=10)
        self.proc.stdout.close()
        self.proc = None

    def stop(self):
        self._signal(signal.SIGTERM)

    def kill(self):
        self._signal(signal.SIGKILL)


def legacy_gateway_prefix(script):
    """Frozen gateway protocol/storage with a numeric-only listener metadata shim."""
    return [sys.executable, str(Path(__file__).with_name("legacy_gateway.py")), str(script)]


class Gateway(object):
    """A real bot_gateway.py from the source tree (or a given command prefix) with its own home and port."""

    def __init__(self, root, name="gateway", limits=None, prefix=None, env=None):
        self.home = Path(root) / name
        self.port = free_port()
        self.prefix = prefix or [sys.executable, str(PUBLIC / "bot_gateway.py")]
        self.env = env or plain_env(Path(root))
        r = subprocess.run(self.prefix + ["--gateway-home", str(self.home), "init", "--port", str(self.port),
                                          "--owner-label", "Synthetic owner"],
                           capture_output=True, text=True, env=self.env, timeout=60)
        assert r.returncode == 0, r.stdout + r.stderr
        self.init_output = json.loads(r.stdout)
        if limits:
            cfg = json.loads((self.home / "gateway.json").read_text())
            cfg["limits"] = limits
            (self.home / "gateway.json").write_text(json.dumps(cfg))
        self.owner_token = (self.home / "owner.token").read_text().strip()
        self.url = "http://127.0.0.1:%d" % self.port
        self.service = None

    def start(self):
        self.service = Service(self.prefix + ["--gateway-home", str(self.home), "serve"], self.env,
                               self.home.parent / ("%s.log" % self.home.name)).start()
        assert self.service.url == self.url, (self.service.url, self.url)
        return self

    def stop(self):
        if self.service:
            self.service.stop()

    def kill(self):
        if self.service:
            self.service.kill()

    def restart(self, hard=False):
        self.kill() if hard else self.stop()
        return self.start()

    def call(self, method, path, body=None, token=None):
        return C.api_call(self.url, method, path, body, token)

    def ok(self, method, path, body=None, token=None):
        status, obj = self.call(method, path, body, token)
        assert status in (200, 201, 202) and obj.get("ok") is True, (method, path, status, obj)
        return obj

    # ---- small builders over the real HTTP API
    def bot(self, handle, publish=True, policy=None, owner=None):
        body = {"handle": handle, "display_name": "Synthetic %s" % handle}
        if policy is not None:
            body["policy"] = policy
        bot = self.ok("POST", "/v1/owner/bots", body, owner or self.owner_token)
        if publish:
            self.ok("POST", "/v1/owner/bots/%s/publish" % bot["bot_id"], {}, owner or self.owner_token)
        return bot["bot_id"]

    def actor(self, name="Synthetic caller"):
        obj = self.ok("POST", "/v1/actors", {"display_name": name})
        return obj["actor_id"], obj["token"]

    def conversation(self, bot_ref, token, op_key=None):
        return self.ok("POST", "/v1/bots/%s/conversations" % bot_ref,
                       {"op_key": op_key or C.new_token("ticket")[4:20]}, token)["conversation_id"]

    def send(self, cid, token, kind="faq", text="hello", op_key=None, **extra):
        body = dict({"op_key": op_key or C.new_token("ticket")[4:24], "kind": kind}, **extra)
        if kind != "slots" or text is not None:
            if text is not None:
                body["text"] = text
        return self.call("POST", "/v1/conversations/%s/messages" % cid, body, token)

    def contact(self, bot_id, actor_id, action, caps=None, owner=None):
        body = {"action": action}
        if caps is not None:
            body["capabilities"] = caps
        return self.call("POST", "/v1/owner/bots/%s/contacts/%s" % (bot_id, actor_id), body, owner or self.owner_token)

    def worker(self, bot_id, replace=False, owner=None):
        return self.ok("POST", "/v1/owner/bots/%s/workers" % bot_id, {"label": "synthetic", "replace": replace},
                       owner or self.owner_token)["token"]

    def claim(self, wtoken, lease_seconds=30, limit=1):
        return self.ok("POST", "/v1/worker/claim", {"lease_seconds": lease_seconds, "max": limit}, wtoken)["items"]

    def start_work(self, wtoken, item):
        lease = item["lease"]
        return self.call("POST", "/v1/worker/messages/%s/start" % item["message_id"],
                         {"lease_id": lease["lease_id"], "fence": lease["fence"]}, wtoken)

    def finish(self, wtoken, item, outcome="completed", reply=None, code=None):
        lease = item["lease"]
        body = {"lease_id": lease["lease_id"], "fence": lease["fence"], "outcome": outcome,
                "reply": reply if reply is not None else {"text": "synthetic answer"}}
        if code:
            body["code"] = code
        return self.call("POST", "/v1/worker/messages/%s/finish" % item["message_id"], body, wtoken)

    def owner_view(self, cid):
        return self.ok("GET", "/v1/owner/conversations/%s" % cid, None, self.owner_token)


def calendar_policy(**overrides):
    policy = {"timezone": "UTC", "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
              "hours": [["09:00", "17:00"]], "durations": [60], "horizon_days": 14, "min_notice_minutes": 0,
              "step_minutes": 60, "buffer_minutes": 0, "max_slots": 3, "levels": ["friend"],
              "per_contact_per_day": 3, "per_bot_per_day": 50, "answer_ttl_minutes": 15}
    policy.update(overrides)
    return policy


def write_calendar_dir(directory, gateway_url, bot_id, policy, events, verifier_token):
    """A private capability service directory written the way `calendar-service init` would."""
    directory = Path(directory)
    C.ensure_private_dir(directory)
    access = C.new_token("access")
    C.create_private_file(directory / "access.token", access + "\n")
    C.create_private_file(directory / "verifier.token", verifier_token + "\n")
    C.write_private_json(directory / "events.json", {"events": events})
    C.write_private_json(directory / "service.json", {
        "v": 1, "gateway": gateway_url, "bot_id": bot_id, "events_file": "events.json",
        "verifier_token_file": "verifier.token", "access_token_hash": C.token_hash(access), "policy": policy})
    return access


def calendar_service(directory, env, log_path, prefix=None):
    prefix = prefix or [sys.executable, str(PUBLIC / "bot_calendar.py")]
    return Service(prefix + ["serve", "--dir", str(directory)], env, log_path).start()


def wait_until(fn, timeout=10.0, step=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(step)
    return fn()


class FriendsGateway(Gateway):
    """Gateway plus Friends-level builders over the real HTTP API (source bots, follows, native jobs, reviews)."""

    def enroll_bot(self, handle, name=None, expect=201):
        owner, source = C.new_token("owner"), C.new_token("source")
        status, obj = self.call("POST", "/v1/enroll", {"display_name": name or "Synthetic %s" % handle,
                                                        "handle": handle,
                                                        "source_credential_sha256": C.token_hash(source)}, owner)
        assert status == expect, (status, obj)
        return obj.get("bot_id"), owner, source

    def source_for(self, bot_id, owner=None):
        token = C.new_token("source")
        self.ok("POST", "/v1/owner/bots/%s/sources" % bot_id, {"credential_sha256": C.token_hash(token)},
                owner or self.owner_token)
        return token

    def owned_bot(self, handle, policy=None):
        """An admin-owner bot, published, with a source credential: -> (bot_id, source token)."""
        bot_id = self.bot(handle, policy=policy)
        return bot_id, self.source_for(bot_id)

    def follow(self, source, ref):
        return self.call("POST", "/v1/bots/%s/follow" % ref, {}, source)

    def unfollow(self, source, ref):
        return self.call("POST", "/v1/bots/%s/unfollow" % ref, {}, source)

    def relation(self, token, ref):
        return self.ok("GET", "/v1/bots/%s/contact" % ref, None, token)

    def policy(self, bot_id, owner=None, **policy):
        return self.ok("PATCH", "/v1/owner/bots/%s" % bot_id, {"policy": policy}, owner or self.owner_token)

    def native(self, bot_id, replace=False, owner=None):
        obj = self.ok("POST", "/v1/owner/bots/%s/workers" % bot_id, {"adapter": "native", "label": "synthetic host",
                                                                     "replace": replace}, owner or self.owner_token)
        return obj["token"], obj["worker_id"]

    def chat(self, cid, token, text, op_key=None):
        status, obj = self.send(cid, token, kind="chat", text=text, op_key=op_key)
        assert status == 202, (status, obj)
        return obj["message_id"]

    def reviews(self, bot_id, owner=None):
        return self.ok("GET", "/v1/owner/bots/%s/reviews/full" % bot_id, None, owner or self.owner_token)

    def admit(self, bot_id, mid, owner=None):
        match = [x for x in self.reviews(bot_id, owner)["inbound"] if x["job_id"] == mid]
        assert match, ("not waiting for inbound review", mid)
        return self.call("POST", "/v1/owner/messages/%s/review" % mid,
                         {"action": "approve_inbound", "request_hash": match[0]["request_hash"]},
                         owner or self.owner_token)

    def claim_job(self, wtoken, mid, lease_seconds=120):
        return self.call("POST", "/v1/worker/messages/%s/claim" % mid, {"lease_seconds": lease_seconds}, wtoken)

    def reply_job(self, wtoken, job, text, outcome="completed", code=None):
        body = {"lease_id": job["lease"]["lease_id"], "fence": job["lease"]["fence"], "outcome": outcome,
                "reply": {"text": text} if outcome == "completed" else {}}
        if code:
            body["code"] = code
        return self.call("POST", "/v1/worker/messages/%s/finish" % job["job_id"], body, wtoken)

    def draft_hash(self, bot_id, mid, owner=None):
        match = [x for x in self.reviews(bot_id, owner)["replies"] if x["job_id"] == mid]
        return match[0]["draft_hash"] if match else None

    def approve_reply(self, bot_id, mid, owner=None, draft_hash=None):
        return self.call("POST", "/v1/owner/messages/%s/review" % mid,
                         {"action": "approve_reply", "draft_hash": draft_hash or self.draft_hash(bot_id, mid, owner)},
                         owner or self.owner_token)

    def answer(self, bot_id, wtoken, mid, text, owner=None):
        """Owner-admitted, host-claimed, host-answered, owner-approved: one complete supervised native turn."""
        if [x for x in self.reviews(bot_id, owner)["inbound"] if x["job_id"] == mid]:
            assert self.admit(bot_id, mid, owner)[0] == 200
        status, job = self.claim_job(wtoken, mid)
        assert status == 200, job
        assert self.reply_job(wtoken, job, text)[0] == 200
        assert self.approve_reply(bot_id, mid, owner)[0] == 200
        return job

    def resolve(self, mid, action, owner=None):
        return self.call("POST", "/v1/owner/messages/%s/resolve" % mid, {"action": action}, owner or self.owner_token)

    def raw(self, method, path, body=b"", headers=None):
        """One raw HTTP exchange: -> (status, headers dict, body bytes)."""
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
        finally:
            conn.close()

    def mcp(self, token, message, version="2025-11-25", origin=None, extra=None, raw_body=None):
        """POST one JSON-RPC message to /mcp as an MCP client would. -> (status, headers, parsed body or None)."""
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if token:
            headers["Authorization"] = "Bearer " + token
        if version:
            headers["MCP-Protocol-Version"] = version
        if origin:
            headers["Origin"] = origin
        headers.update(extra or {})
        body = raw_body if raw_body is not None else json.dumps(message).encode("utf-8")
        status, hdrs, data = self.raw("POST", "/mcp", body, headers)
        return status, hdrs, (json.loads(data) if data else None)

    def tool(self, token, name, arguments, rid=1):
        """tools/call -> the MCP tool result (content, structuredContent, isError); asserts a JSON-RPC result."""
        status, _, obj = self.mcp(token, {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                                          "params": {"name": name, "arguments": arguments}})
        assert status == 200 and "result" in obj, (status, obj)
        return obj["result"]

    def db(self):
        import sqlite3
        db = sqlite3.connect(str(self.home / "gateway.sqlite3"), timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

    def sql(self, statement, args=()):
        db = self.db()
        try:
            rows = db.execute(statement, args).fetchall()
            return [dict(r) for r in rows]
        finally:
            db.close()
