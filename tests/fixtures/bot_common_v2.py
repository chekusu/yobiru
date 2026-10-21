#!/usr/bin/env python3
# Fixture: its bot_common.py exactly as committed at 961f065 (Friends and dispatch reliability, before schema 3),
# kept to build real schema-2 databases for the schema 2 -> 3 migration tests. Do not edit.
"""Shared helpers for Yobiru public bot contacts (Python 3.8+, stdlib only).

Used by bot_gateway.py (the public mailbox), bot_client.py (callers, owners and the constrained
worker) and bot_calendar.py (the private calendar capability service). Nothing here touches the
tailmsg data home, Tailscale or the private receiver on port 7766.
"""
import base64
import datetime as dt
import hashlib
import http.client
import ipaddress
import json
import os
import re
import secrets
import socket
import ssl
import stat
import threading
import time
import unicodedata
import urllib.parse
from pathlib import Path

API_VERSION = 1
SERVICE = "yobiru-bot-gateway"
MAX_RESPONSE_BYTES = 1024 * 1024
ID_KINDS = ("bot", "act", "own", "wrk", "ver", "cnv", "msg", "chk")
ID_RE = re.compile(r"^(?:bot|act|own|wrk|ver|cnv|msg|chk)_[a-z2-7]{16}\Z")
# source: speak and follow as one bot (minted only through that bot's owner); connector: wake one native
# binding's host, never read message bodies.
TOKEN_PREFIX = {"owner": "ybo", "actor": "yba", "worker": "ybw", "verifier": "ybv", "ticket": "ybt",
                "access": "ybc", "source": "ybs", "connector": "ybn"}
TOKEN_RE = re.compile(r"^(ybo|yba|ybw|ybv|ybt|ybc|ybs|ybn)_[A-Za-z0-9_-]{43}\Z")
HASH_RE = re.compile(r"^[0-9a-f]{64}\Z")
RESOURCE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}\Z")
HANDLE_RE = re.compile(r"^[a-z](?:[a-z0-9]|-(?=[a-z0-9])){2,31}\Z")
OP_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}\Z")
DATE_RE = re.compile(r"^\d{4}-\d\d-\d\d\Z")
ISO_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\Z")
# Bidirectional overrides/isolates and marks: they can make displayed text differ from stored text.
BIDI = set("\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")


class StrictJSONError(ValueError):
    pass


class ApiError(Exception):
    """A gateway or capability service answered with an error, or could not be reached."""

    def __init__(self, status, code, detail=""):
        Exception.__init__(self, "%s (%s)" % (code, status) + (": " + detail if detail else ""))
        self.status, self.code, self.detail = status, code, detail


# ---------------------------------------------------------------- ids, tokens and time
def new_id(kind):
    if kind not in ID_KINDS:
        raise ValueError("unknown id kind")
    return "%s_%s" % (kind, base64.b32encode(secrets.token_bytes(10)).decode("ascii").lower())


def new_token(kind):
    return "%s_%s" % (TOKEN_PREFIX[kind], secrets.token_urlsafe(32))


def token_kind(token):
    """The credential kind encoded in a token's prefix, or None for anything malformed."""
    if not isinstance(token, str) or not TOKEN_RE.match(token):
        return None
    prefix = token.split("_", 1)[0]
    return next(k for k, v in TOKEN_PREFIX.items() if v == prefix)


def token_hash(token):
    """Credentials are random 256-bit values, so a plain SHA-256 is a sufficient one-way index."""
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def now_ts():
    return dt.datetime.now(dt.timezone.utc).timestamp()


def iso(ts):
    return dt.datetime.fromtimestamp(int(ts), dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text):
    if not isinstance(text, str) or not ISO_RE.match(text):
        raise ValueError("expected YYYY-MM-DDTHH:MM:SSZ")
    return dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).timestamp()


def parse_date(text):
    if not isinstance(text, str) or not DATE_RE.match(text):
        raise ValueError("expected YYYY-MM-DD")
    return dt.datetime.strptime(text, "%Y-%m-%d").date()


# ---------------------------------------------------------------- strict input
def strict_loads(raw):
    """Parse a JSON request body: UTF-8, one object, no duplicate keys, no NaN/Infinity."""
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise StrictJSONError("duplicate key")
            out[key] = value
        return out

    def constant(_name):
        raise StrictJSONError("non-finite number")
    try:
        text = raw.decode("utf-8")
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except StrictJSONError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise StrictJSONError("invalid JSON")
    return value


def text_error(value, limit, multiline=False, required=True):
    """None if `value` is acceptable display text; a short reason otherwise. Never modifies input."""
    if value is None and not required:
        return None
    if not isinstance(value, str):
        return "must be a string"
    if required and not value.strip():
        return "must not be empty"
    if len(value) > limit:
        return "longer than %d characters" % limit
    for c in value:
        cat = unicodedata.category(c)
        if cat == "Cs" or c in BIDI:
            return "contains unsupported characters"
        if cat == "Cc" and not (multiline and c in "\n\t"):
            return "contains control characters"
        if not multiline and cat in ("Zl", "Zp"):
            return "contains line separators"
    return None


def clean_label(value, limit=80):
    """NFC single-line label with surrounding whitespace removed (call text_error first)."""
    return " ".join(unicodedata.normalize("NFC", value).split())[:limit]


def int_in(value, low, high):
    return type(value) is int and low <= value <= high


# ---------------------------------------------------------------- gateway URLs and addresses
def is_loopback_host(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def normalize_base_url(url):
    """Canonical gateway base URL. Credentials are bound to exactly this string, so a token is never
    sent to another scheme, host, port or path prefix. Plain http is accepted only for loopback."""
    if not isinstance(url, str) or not url or len(url) > 2048 or \
            any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url):
        raise ValueError("invalid gateway URL")
    try:
        u = urllib.parse.urlsplit(url)
        port = u.port
    except ValueError:
        raise ValueError("invalid gateway URL")
    scheme = u.scheme.lower()
    if scheme not in ("https", "http"):
        raise ValueError("gateway URL must start with https:// (http:// only for a loopback test gateway)")
    if u.username is not None or u.password is not None or "@" in u.netloc:
        raise ValueError("gateway URL must not contain credentials")
    if u.query or u.fragment:
        raise ValueError("gateway URL must not contain a query or fragment")
    host = (u.hostname or "").lower()
    if not host or (port is not None and not 1 <= port <= 65535):
        raise ValueError("invalid gateway URL")
    if scheme == "http" and not is_loopback_host(host):
        raise ValueError("refusing plain http:// for a non-loopback gateway; use https://")
    path = u.path.rstrip("/")
    if path and (not re.match(r"^(?:/[A-Za-z0-9._~-]+)+\Z", path) or
                 any(seg in (".", "..") for seg in path.split("/")[1:])):
        raise ValueError("invalid gateway URL path")
    netloc = "[%s]" % host if ":" in host else host
    if port is not None and port != {"https": 443, "http": 80}[scheme]:
        netloc += ":%d" % port
    return "%s://%s%s" % (scheme, netloc, path)


def parse_bot_ref(text):
    """'@handle', 'handle' or 'bot_...' -> canonical reference used in gateway paths."""
    if not isinstance(text, str):
        raise ValueError("invalid bot reference")
    if ID_RE.match(text) and text.startswith("bot_"):
        return text
    handle = text[1:] if text.startswith("@") else text
    handle = handle.lower()
    if not HANDLE_RE.match(handle):
        raise ValueError("invalid bot handle %r (3-32 of a-z, 0-9 and single hyphens, starting with a letter)"
                         % text[:40])
    return "@" + handle


def parse_address(text):
    """'https://gw.example/@handle' or '.../@bot_...' (a share link) -> (base_url, '@handle' or bot id);
    a bare '@handle' or bot id -> (None, ref). The last path segment must start with '@'."""
    if not isinstance(text, str) or not text:
        raise ValueError("invalid bot address")
    if "://" not in text:
        return None, parse_bot_ref(text)
    head, sep, tail = text.rpartition("/")
    if not sep or not tail.startswith("@"):
        raise ValueError("a share link ends with /@handle or /@bot_...")
    ref = tail[1:] if tail[1:].startswith("bot_") else tail
    return normalize_base_url(head), parse_bot_ref(ref)


def share_path(ref):
    """The gateway path of a share link for a canonical reference ('@handle' or a bot id)."""
    return "/" + ref if ref.startswith("@") else "/@" + ref


# ---------------------------------------------------------------- private files
def ensure_private_dir(path):
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            os.chmod(str(path), 0o700)
    return path


def write_private_json(path, obj):
    """Atomic replace through an exclusive 0600 temporary file."""
    path = Path(path)
    tmp = path.with_name(".%s.%s.tmp" % (path.name, secrets.token_hex(4)))
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write((json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))
            out.flush()
            os.fsync(out.fileno())
        os.replace(str(tmp), str(path))
    except BaseException:
        if os.path.lexists(str(tmp)):
            os.unlink(str(tmp))
        raise


def create_private_file(path, text):
    """Create a new 0600 file; never overwrites an existing file or follows a symlink."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(text)
        out.flush()
        os.fsync(out.fileno())


def read_private_text(path, limit=64 * 1024, what="file"):
    """Read a credential or private config file: regular file, bounded, and (POSIX) not accessible to
    group or others, like tailmsg's secret_file rule."""
    path = Path(path)
    try:
        st = os.lstat(str(path))
    except OSError:
        raise ValueError("%s %s does not exist or is not readable" % (what, path))
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise ValueError("%s %s must be a regular file, not a symlink" % (what, path))
    if os.name == "posix" and st.st_mode & 0o077:
        raise ValueError("%s %s must be mode 0600 (chmod 600)" % (what, path))
    if st.st_size > limit:
        raise ValueError("%s %s is too large" % (what, path))
    return path.read_text(encoding="utf-8")


class LockBusy(Exception):
    pass


class FileLock(object):
    """Exclusive kernel advisory lock on a lock file (flock on POSIX, msvcrt on Windows). Closing the
    descriptor releases it, also when the process dies."""

    def __init__(self, path, blocking=True, timeout=30.0):
        self.path, self.blocking, self.timeout, self.fd = Path(path), blocking, timeout, None

    def _try(self, fd):
        try:
            import fcntl
        except ImportError:
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def __enter__(self):
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self._try(fd)
                break
            except OSError:
                if not self.blocking or time.monotonic() >= deadline:
                    os.close(fd)
                    raise LockBusy(str(self.path))
                time.sleep(0.05)
        self.fd = fd
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def read_token_file(path, kind):
    token = read_private_text(path, 4096, "credential file").strip()
    if token_kind(token) != kind:
        raise ValueError("credential file %s does not contain a %s credential" % (path, kind))
    return token


def read_json_file(path, limit=1024 * 1024, private=False, what="file"):
    if private:
        text = read_private_text(path, limit, what)
    else:
        path = Path(path)
        if path.is_symlink() or not path.is_file():
            raise ValueError("%s %s must be a regular file" % (what, path))
        if path.stat().st_size > limit:
            raise ValueError("%s %s is too large" % (what, path))
        text = path.read_text(encoding="utf-8")
    try:
        return strict_loads(text.encode("utf-8"))
    except StrictJSONError as e:
        raise ValueError("%s %s: %s" % (what, path, e))


# ---------------------------------------------------------------- HTTP client
class _Deadline(object):
    """Total time limit for one exchange: when it passes the socket is shut down, so a server that
    trickles bytes cannot hold the client longer than `seconds`.

    The transport socket is kept from connect(): for HTTP/1.0 or `Connection: close` responses,
    getresponse() detaches conn.sock while the response body is still read from that same socket,
    so shutting down conn.sock alone would not stop a slow close-delimited body."""

    def __init__(self, conn, seconds):
        self.conn, self.expired, self.sock, self.lock = conn, False, None, threading.Lock()
        self.timer = threading.Timer(max(0.05, seconds), self._expire)
        self.timer.daemon = True

    def _expire(self):
        with self.lock:
            self.expired = True
            sock = self.sock or self.conn.sock
        self._abort(sock)

    @staticmethod
    def _abort(sock):
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def connect(self):
        self.conn.connect()
        with self.lock:
            self.sock = self.conn.sock
            late = self.expired
        if late:
            self._abort(self.sock)
            raise socket.timeout("deadline exceeded")

    def check(self):
        if self.expired:
            raise socket.timeout("deadline exceeded")

    def __enter__(self):
        self.timer.start()
        return self

    def __exit__(self, *exc):
        self.timer.cancel()
        self.conn.close()
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass


def api_call(base_url, method, path, body=None, token=None, timeout=15.0):
    """One JSON request to `base_url + path`. -> (status, object). Redirects are never followed (a 3xx is
    returned as an error), TLS certificates are verified, responses are bounded. The bearer token, if
    any, is only ever sent to the normalized `base_url` it belongs to."""
    base = normalize_base_url(base_url)
    u = urllib.parse.urlsplit(base)
    host, port = u.hostname, u.port
    if u.scheme == "https":
        conn = http.client.HTTPSConnection(host, port or 443, timeout=timeout,
                                           context=ssl.create_default_context())
    else:
        conn = http.client.HTTPConnection(host, port or 80, timeout=timeout)
    headers = {"Accept": "application/json", "User-Agent": "yobiru-public/%d" % API_VERSION}
    if token is not None:
        if token_kind(token) is None:
            raise ValueError("refusing to send a malformed credential")
        headers["Authorization"] = "Bearer " + token
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    with _Deadline(conn, timeout) as d:
        try:
            d.connect()
            conn.request(method, u.path + path, body=data, headers=headers)
            resp = conn.getresponse()
            raw = resp.read(MAX_RESPONSE_BYTES + 1)
            d.check()            # a body that completed only because the deadline cut it short is not a success
        except (OSError, http.client.HTTPException) as e:
            raise ApiError(None, "unreachable", "%s%s" % (type(e).__name__, " (deadline)" if d.expired else ""))
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ApiError(resp.status, "response_too_large")
    if 300 <= resp.status < 400:
        raise ApiError(resp.status, "redirect_refused", "credentials are never forwarded to another location")
    try:
        obj = strict_loads(raw) if raw else {}
    except StrictJSONError:
        raise ApiError(resp.status, "invalid_response")
    if not isinstance(obj, dict):
        raise ApiError(resp.status, "invalid_response")
    return resp.status, obj


def api_ok(base_url, method, path, body=None, token=None, timeout=15.0, accept=(200, 201, 202)):
    status, obj = api_call(base_url, method, path, body, token, timeout)
    if status not in accept or obj.get("ok") is not True:
        code = obj.get("error") if isinstance(obj.get("error"), str) else "http_%s" % status
        detail = obj.get("detail") if isinstance(obj.get("detail"), str) else ""
        raise ApiError(status, code[:64], detail[:300])
    return status, obj


# ---------------------------------------------------------------- wake transport (connector -> owner's host)
def wake_url(url):
    """An owner-configured wake endpoint: https (http only for loopback), no credentials or fragment. Unlike a
    gateway URL it may carry a path and query, because hosts issue such webhook URLs."""
    if not isinstance(url, str) or not url or len(url) > 4096 or \
            any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url):
        raise ValueError("invalid wake URL")
    try:
        u = urllib.parse.urlsplit(url)
        port = u.port
    except ValueError:
        raise ValueError("invalid wake URL")
    host = (u.hostname or "").lower()
    if u.scheme not in ("https", "http") or not host or u.fragment or u.username is not None or \
            u.password is not None or (port is not None and not 1 <= port <= 65535):
        raise ValueError("wake URL must be https://host/path (no credentials or fragment)")
    if u.scheme == "http" and not is_loopback_host(host):
        raise ValueError("refusing plain http:// for a non-loopback wake URL")
    return u


def post_wake(url, headers, body, timeout=20.0):
    """POST one wake notification. -> (outcome, detail). Redirects are not followed and the answer body is not
    interpreted: a 2xx means the host accepted the wake (not that any work happened).
      accepted   2xx answer
      refused    no connection could be made: the request never left, so nothing was delivered
      uncertain  anything else once the request may have left: any non-2xx answer (a 5xx or a 429 can follow
                 work the host already queued), a timeout or a reset. It is never treated as not delivered."""
    u = wake_url(url)
    if u.scheme == "https":
        conn = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=timeout,
                                           context=ssl.create_default_context())
    else:
        conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
    data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    hdrs = {"Content-Type": "application/json", "User-Agent": "yobiru-connector/%d" % API_VERSION}
    hdrs.update(headers)
    target = (u.path or "/") + ("?" + u.query if u.query else "")
    with _Deadline(conn, timeout) as d:
        try:
            d.connect()
        except (OSError, http.client.HTTPException) as e:
            return "refused", "connect: %s" % type(e).__name__
        try:
            conn.request("POST", target, body=data, headers=hdrs)
            resp = conn.getresponse()
            resp.read(64 * 1024)
            d.check()
        except (OSError, http.client.HTTPException) as e:
            return "uncertain", "%s%s" % (type(e).__name__, " (deadline)" if d.expired else "")
    if 200 <= resp.status < 300:
        return "accepted", "http %d" % resp.status
    return "uncertain", "http %d" % resp.status
