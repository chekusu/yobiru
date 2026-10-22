#!/usr/bin/env python3
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
MAX_RESPONSE_BYTES = 24 * 1024 * 1024      # a job or message may carry 8 MiB of media (sealed: about 15 MB)
ID_KINDS = ("bot", "act", "own", "wrk", "ver", "cnv", "msg", "chk", "dec", "eml")
ID_RE = re.compile(r"^(?:bot|act|own|wrk|ver|cnv|msg|chk|dec|eml)_[a-z2-7]{16}\Z")
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
# Attachments (opt-in `files` capability): plain names with a known extension or none at all. Text files travel as
# UTF-8 `content`; images, PDFs, audio and video travel as base64 `data` and must start with the file signature of
# their extension. Names a viewer could run or open as something else (scripts, executables, HTML, SVG) are refused.
ATTACHMENT_EXTENSIONS = ("txt", "md", "markdown", "json", "csv", "tsv", "yaml", "yml", "toml", "log", "diff", "patch",
                         "rst")
ATTACHMENT_NAME_CHARS = 100
ATTACHMENT_NAME_BYTES = 200
# extension -> media type of binary (media) attachments; each must match its signature (media_signature_ok)
MEDIA_TYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif",
               "webp": "image/webp", "heic": "image/heic", "heif": "image/heif", "avif": "image/avif",
               "pdf": "application/pdf", "mp3": "audio/mpeg", "m4a": "audio/mp4", "wav": "audio/wav",
               "ogg": "audio/ogg", "opus": "audio/ogg", "mp4": "video/mp4", "mov": "video/quicktime",
               "webm": "video/webm"}
MEDIA_MAX_BYTES = 4 * 1024 * 1024           # one media file, raw bytes (the default; a gateway may set less)
NAME_RESERVED_CHARS = set('/\\:*?"<>|')
WINDOWS_DEVICE_RE = re.compile("^(?:con|prn|aux|nul|clock\\$|conin\\$|conout\\$|com[0-9\u00b9\u00b2\u00b3]|"
                               "lpt[0-9\u00b9\u00b2\u00b3])\\Z", re.I)


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


# ---------------------------------------------------------------- attachments (text and media)
def attachment_ext(name):
    return name.rsplit(".", 1)[1].lower() if "." in name else ""


def is_media_name(name):
    return attachment_ext(name) in MEDIA_TYPES


def attachment_name_error(name):
    """None if `name` is a safe plain file name for an attachment; a short reason otherwise. Plain means: no
    separators or traversal, no leading dot or dash, no trailing dot or space, nothing invisible or reserved on
    Windows, and an extension from ATTACHMENT_EXTENSIONS or MEDIA_TYPES (or none). Never modifies input."""
    if not isinstance(name, str):
        return "must be a string"
    if not 1 <= len(name) <= ATTACHMENT_NAME_CHARS or len(name.encode("utf-8", "surrogatepass")) > ATTACHMENT_NAME_BYTES:
        return "must be 1-%d characters (%d bytes)" % (ATTACHMENT_NAME_CHARS, ATTACHMENT_NAME_BYTES)
    for c in name:
        cat = unicodedata.category(c)
        if c in NAME_RESERVED_CHARS or c in BIDI or cat[0] == "C" or (cat[0] == "Z" and c != " "):
            return "contains a separator, a reserved or an invisible character"
    if unicodedata.normalize("NFC", name) != name:
        return "must be in Unicode normal form NFC"
    if name[0] in ".- " or name[-1] in ". ":
        return "must not start with a dot, dash or space, nor end with a dot or space"
    if WINDOWS_DEVICE_RE.match(name.split(".", 1)[0].rstrip(" ")):
        return "is a reserved device name on Windows"
    ext = attachment_ext(name)
    if ext and ext not in ATTACHMENT_EXTENSIONS and ext not in MEDIA_TYPES:
        return "must be a text file (.%s), an image, PDF, audio or video file (.%s) or have no extension" % (
            ", .".join(ATTACHMENT_EXTENSIONS), ", .".join(sorted(MEDIA_TYPES)))
    return None


def attachment_text_error(content, max_bytes):
    """None if `content` is UTF-8 text within `max_bytes`: no NUL or other control characters except newline,
    carriage return and tab, no unpaired surrogates or bidirectional controls."""
    if not isinstance(content, str):
        return "must be a string"
    try:
        size = len(content.encode("utf-8"))
    except UnicodeEncodeError:
        return "is not valid UTF-8 text"
    if size > max_bytes:
        return "is larger than %d bytes" % max_bytes
    for c in content:
        if c in BIDI:
            return "contains bidirectional control characters"
        if unicodedata.category(c) == "Cc" and c not in "\n\r\t":
            return "contains NUL or other control characters"
    return None


ISO_BMFF = {"heic": (b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"mif1", b"msf1"),
            "heif": (b"mif1", b"msf1", b"heic", b"heix", b"heif"), "avif": (b"avif", b"avis")}


def media_signature_ok(ext, raw):
    """Whether `raw` starts like a file of type `ext`: a renamed program, page or archive is refused."""
    if ext == "png":
        return raw.startswith(b"\x89PNG\r\n\x1a\n")
    if ext in ("jpg", "jpeg"):
        return raw.startswith(b"\xff\xd8\xff")
    if ext == "gif":
        return raw[:6] in (b"GIF87a", b"GIF89a")
    if ext == "webp":
        return raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"
    if ext == "wav":
        return raw[:4] == b"RIFF" and raw[8:12] == b"WAVE"
    if ext in ISO_BMFF:
        return raw[4:8] == b"ftyp" and (raw[8:12] in ISO_BMFF[ext] or any(
            raw[i:i + 4] in ISO_BMFF[ext] for i in range(16, min(len(raw), int.from_bytes(raw[:4], "big"), 64), 4)))
    if ext in ("m4a", "mp4", "mov"):
        return raw[4:8] in (b"ftyp", b"moov", b"mdat", b"wide", b"free")
    if ext == "pdf":
        return raw.startswith(b"%PDF-")
    if ext == "mp3":
        return raw.startswith(b"ID3") or (len(raw) > 1 and raw[0] == 0xFF and raw[1] & 0xE0 == 0xE0)
    if ext in ("ogg", "opus"):
        return raw.startswith(b"OggS")
    if ext == "webm":
        return raw.startswith(b"\x1a\x45\xdf\xa3")
    return False


def decode_media(data, max_bytes):
    """Canonical standard base64 (padded, no whitespace) -> (bytes, None) or (None, reason)."""
    if not isinstance(data, str):
        return None, "must be a base64 string"
    if len(data) > 4 * ((max_bytes + 2) // 3):
        return None, "is larger than %d bytes" % max_bytes
    try:
        raw = base64.b64decode(data.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError):
        return None, "is not valid base64"
    if base64.b64encode(raw).decode("ascii") != data:
        return None, "is not canonical base64 (standard alphabet, padded, no whitespace)"
    if not raw:
        return None, "is empty"
    if len(raw) > max_bytes:
        return None, "is larger than %d bytes" % max_bytes
    return raw, None


def media_raw_size(data):
    """Raw bytes of canonical base64 `data` without decoding it."""
    return len(data) // 4 * 3 - (len(data) - len(data.rstrip("=")))


def attachments_error(items, max_files, max_bytes, total_bytes, media_max_bytes=MEDIA_MAX_BYTES):
    """Validate a list of attachments: text {name, content} (UTF-8, within `max_bytes`) or media {name, data}
    (base64 of an image, PDF, audio or video file within `media_max_bytes` whose signature matches its extension).
    -> (clean list, total raw bytes, None) or (None, 0, reason). Names must be distinct within one message, also
    after case folding."""
    if not isinstance(items, list) or not items:
        return None, 0, "attachments must be a non-empty list"
    if len(items) > max_files:
        return None, 0, "at most %d attachments" % max_files
    clean, seen, total = [], set(), 0
    for item in items:
        if not isinstance(item, dict) or set(item) not in ({"name", "content"}, {"name", "data"}):
            return None, 0, "each attachment is exactly {name, content} (text) or {name, data} (base64 media)"
        err = attachment_name_error(item["name"])
        if err:
            return None, 0, "attachment name %s" % err
        folded = unicodedata.normalize("NFC", item["name"]).casefold()
        if folded in seen:
            return None, 0, "attachment names must be distinct"
        seen.add(folded)
        ext = attachment_ext(item["name"])
        if "data" in item:
            if ext not in MEDIA_TYPES:
                return None, 0, "attachment %s: base64 data is only for image, PDF, audio or video files (.%s)" % (
                    item["name"], ", .".join(sorted(MEDIA_TYPES)))
            raw, err = decode_media(item["data"], media_max_bytes)
            if err:
                return None, 0, "attachment %s %s" % (item["name"], err)
            if not media_signature_ok(ext, raw):
                return None, 0, "attachment %s is not a .%s file (its content does not match the extension)" % (
                    item["name"], ext)
            size = len(raw)
            clean.append({"name": item["name"], "data": item["data"]})
        else:
            if ext in MEDIA_TYPES:
                return None, 0, "attachment %s is a media file: send it as base64 data, not content" % item["name"]
            err = attachment_text_error(item["content"], max_bytes)
            if err:
                return None, 0, "attachment %s %s" % (item["name"], err)
            size = len(item["content"].encode("utf-8"))
            clean.append({"name": item["name"], "content": item["content"]})
        total += size
        if total > total_bytes:
            return None, 0, "attachments are larger than %d bytes together" % total_bytes
    return clean, total, None


def attachment_raw(item):
    """The bytes of one validated attachment."""
    if "data" in item:
        return base64.b64decode(item["data"])
    return item["content"].encode("utf-8")


def attachment_size(item):
    return media_raw_size(item["data"]) if "data" in item else len(item["content"].encode("utf-8"))


def attachment_bytes(items):
    return sum(attachment_size(a) for a in items or [])


def attachment_view(item, content=True):
    """What a reader sees of one attachment: name, raw bytes and sha256, plus the media type of a media file, and
    with `content` its text or base64 data. A text view stays exactly {name, bytes, sha256[, content]}."""
    raw = attachment_raw(item)
    v = {"name": item["name"], "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    if "data" in item:
        v["media_type"] = MEDIA_TYPES[attachment_ext(item["name"])]
        if content:
            v["data"] = item["data"]
    elif content:
        v["content"] = item["content"]
    return v


def omit_media_data(value, hint):
    """A copy of `value` in which every media attachment view keeps its name, type, size and hash but not its base64
    `data` (megabytes an agent should save as a file, not read as text): `data_omitted` says how to save it."""
    if isinstance(value, list):
        return [omit_media_data(v, hint) for v in value]
    if isinstance(value, dict):
        if isinstance(value.get("data"), str) and "media_type" in value and "sha256" in value:
            return dict({k: v for k, v in value.items() if k != "data"}, data_omitted=hint)
        return {k: omit_media_data(v, hint) for k, v in value.items()}
    return value


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
    a bare '@handle' or bot id -> (None, ref). The last path segment must start with '@'; a '.json' suffix, query or
    fragment on it (the human page's links) is ignored."""
    if not isinstance(text, str) or not text:
        raise ValueError("invalid bot address")
    if "://" not in text:
        return None, parse_bot_ref(text)
    head, sep, tail = text.rpartition("/")
    tail = tail.split("?", 1)[0].split("#", 1)[0]
    if tail.endswith(".json"):              # the card's own URL, as the human page links it
        tail = tail[:-5]
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

    def extend(self, seconds):
        """Allow a large body (attachments) more time: restart the total limit at `seconds` from now."""
        with self.lock:
            if self.expired:
                return
            self.timer.cancel()
            self.timer = threading.Timer(max(0.05, seconds), self._expire)
            self.timer.daemon = True
            self.timer.start()

    def __enter__(self):
        self.started = time.monotonic()
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


LARGE_BODY_BYTES = 256 * 1024
TRANSFER_BYTES_PER_SECOND = 64 * 1024       # the slowest rate a large body is given time for
MAX_TRANSFER_SECONDS = 300.0


def transfer_seconds(timeout, nbytes):
    """The total time limit for an exchange that moves `nbytes`: small bodies keep `timeout`, a large one (media
    attachments) also gets the time it takes at TRANSFER_BYTES_PER_SECOND, up to MAX_TRANSFER_SECONDS."""
    if nbytes <= LARGE_BODY_BYTES:
        return timeout
    return min(MAX_TRANSFER_SECONDS, timeout + nbytes / TRANSFER_BYTES_PER_SECOND)


def read_bounded(resp, d, timeout):
    """Read a response body of at most MAX_RESPONSE_BYTES (+1 to detect more). Bytes that keep arriving earn their
    own transfer time, so a large answer gets through while a peer that trickles stays held to `timeout`."""
    length = resp.getheader("Content-Length")
    if length and length.isdigit() and int(length) > MAX_RESPONSE_BYTES:
        return b"x" * (MAX_RESPONSE_BYTES + 1)
    chunks, total = [], 0
    while total <= MAX_RESPONSE_BYTES:
        chunk = resp.read1(65536)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > LARGE_BODY_BYTES:
            d.extend(d.started + transfer_seconds(timeout, total) - time.monotonic())
    return b"".join(chunks)


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
    with _Deadline(conn, transfer_seconds(timeout, len(data or b""))) as d:
        try:
            d.connect()
            conn.request(method, u.path + path, body=data, headers=headers)
            resp = conn.getresponse()
            raw = read_bounded(resp, d, timeout)
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


def post_raw(base_url, path, data, headers, timeout=60.0):
    """POST `data` (bytes) to `base_url + path` and return (status, content type, body bytes): for the local MCP
    proxy, which forwards JSON-RPC as it is. Same rules as api_call: the normalized `base_url` is the only place a
    credential in `headers` is ever sent, redirects are refused, TLS is verified, the exchange has a total deadline
    and the answer is bounded."""
    base = normalize_base_url(base_url)
    u = urllib.parse.urlsplit(base)
    if u.scheme == "https":
        conn = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=timeout,
                                           context=ssl.create_default_context())
    else:
        conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
    hdrs = {"User-Agent": "yobiru-public/%d" % API_VERSION}
    hdrs.update(headers)
    with _Deadline(conn, transfer_seconds(timeout, len(data))) as d:
        try:
            d.connect()
            conn.request("POST", u.path + path, body=data, headers=hdrs)
            resp = conn.getresponse()
            raw = read_bounded(resp, d, timeout)
            d.check()
        except (OSError, http.client.HTTPException) as e:
            raise ApiError(None, "unreachable", "%s%s" % (type(e).__name__, " (deadline)" if d.expired else ""))
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ApiError(resp.status, "response_too_large")
    if 300 <= resp.status < 400:
        raise ApiError(resp.status, "redirect_refused", "credentials are never forwarded to another location")
    return resp.status, (resp.getheader("Content-Type") or "").split(";")[0].strip().lower(), raw


def get_raw(base_url, path, max_bytes, timeout=15.0):
    """GET `base_url + path` without a credential and return (status, content type, body bytes) for a small public
    file such as a bot's avatar. Same rules as api_call: redirects refused, TLS verified, a total deadline; a body
    larger than `max_bytes` is refused instead of returned."""
    base = normalize_base_url(base_url)
    u = urllib.parse.urlsplit(base)
    if u.scheme == "https":
        conn = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=timeout,
                                           context=ssl.create_default_context())
    else:
        conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
    with _Deadline(conn, timeout) as d:
        try:
            d.connect()
            conn.request("GET", u.path + path, headers={"User-Agent": "yobiru-public/%d" % API_VERSION,
                                                        "Accept": "image/png, image/jpeg, image/webp"})
            resp = conn.getresponse()
            raw = resp.read(max_bytes + 1)
            d.check()
        except (OSError, http.client.HTTPException) as e:
            raise ApiError(None, "unreachable", "%s%s" % (type(e).__name__, " (deadline)" if d.expired else ""))
    if 300 <= resp.status < 400:
        raise ApiError(resp.status, "redirect_refused")
    if len(raw) > max_bytes:
        raise ApiError(resp.status, "response_too_large")
    return resp.status, (resp.getheader("Content-Type") or "").split(";")[0].strip().lower(), raw


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
