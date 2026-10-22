"""Blob store for large message bodies on a Yobiru gateway (stdlib only).

A gateway keeps every message body in SQLite. With a blob store configured (`<home>/blobs.json`), a body at or
over `limits.blob_spill_bytes` (pictures, PDFs, sealed envelopes) is written gzip-compressed to the store instead,
and the database column holds only a reference `blob:<key>:<sha256>`. The reference is not JSON, so a client can
never forge one: the gateway writes every body itself, as canonical JSON. Readers resolve references
transparently; a body whose bytes do not hash to the reference is refused.

Stores:
  local  a directory on the gateway host (default `<home>/blobs`)
  s3     any S3-compatible object store, such as Cloudflare R2, signed with AWS Signature Version 4

blobs.json for Cloudflare R2:
  {"kind": "s3", "endpoint": "https://<ACCOUNT_ID>.r2.cloudflarestorage.com", "bucket": "yobiru-gateway-files",
   "access_key_id": "...", "secret_access_key_file": "r2.secret"}
"""

import collections
import datetime
import gzip
import hashlib
import hmac
import io
import os
import re
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import bot_common as C

PREFIX = "blob:"
REF = re.compile(r"^blob:([a-z0-9]{8,64}):([0-9a-f]{64})$")
KEY = re.compile(r"^[a-z0-9]{8,64}$")
CONFIG_KEYS = {"kind", "dir", "endpoint", "bucket", "region", "access_key_id", "secret_access_key_file", "prefix",
               "timeout_seconds"}
MAX_UNPACKED = 64 * 1024 * 1024          # far above any body the gateway accepts
CACHE_BYTES = 64 * 1024 * 1024


class BlobError(Exception):
    """The store could not write or return a body (unreachable, refused, missing or altered)."""


def new_key():
    return secrets.token_hex(16)


def pack(text):
    return gzip.compress(text.encode("utf-8"), compresslevel=1, mtime=0)


def unpack(data):
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as f:
            raw = f.read(MAX_UNPACKED + 1)
    except (OSError, EOFError):
        raise BlobError("blob is not a stored body")
    if len(raw) > MAX_UNPACKED:
        raise BlobError("blob too large")
    return raw.decode("utf-8")


def make_ref(key, text):
    return "%s%s:%s" % (PREFIX, key, hashlib.sha256(text.encode("utf-8")).hexdigest())


def parse_ref(value):
    """(key, sha256) when a column value is a blob reference, else None."""
    if not isinstance(value, str) or not value.startswith(PREFIX):
        return None
    m = REF.match(value)
    return (m.group(1), m.group(2)) if m else None


class LocalStore(object):
    kind = "local"

    def __init__(self, directory):
        self.dir = Path(directory)
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    def _path(self, key):
        if not KEY.match(key):
            raise BlobError("bad key")
        return self.dir / key[:2] / key

    def put(self, key, data):
        path = self._path(key)
        path.parent.mkdir(mode=0o700, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(path))

    def get(self, key):
        try:
            return self._path(key).read_bytes()
        except FileNotFoundError:
            raise BlobError("missing")

    def delete(self, key):
        try:
            self._path(key).unlink()
        except FileNotFoundError:
            pass


class S3Store(object):
    """Path-style S3 requests signed with AWS Signature Version 4 (header signing, signed payload hash)."""
    kind = "s3"

    def __init__(self, endpoint, bucket, access_key_id, secret_access_key, region="auto", prefix="gw/",
                 timeout=30, opener=None):
        parts = urllib.parse.urlsplit(endpoint)
        if parts.scheme not in ("https", "http") or not parts.netloc or parts.path not in ("", "/"):
            raise ValueError("blobs.json: endpoint must be scheme://host[:port]")
        self.scheme, self.host = parts.scheme, parts.netloc
        self.bucket, self.region, self.prefix, self.timeout = bucket, region, prefix, timeout
        self.access_key_id, self.secret = access_key_id, secret_access_key
        self.opener = opener or urllib.request.build_opener()

    def _url_path(self, key):
        if not KEY.match(key):
            raise BlobError("bad key")
        return "/%s/%s" % (urllib.parse.quote(self.bucket, safe=""),
                           urllib.parse.quote(self.prefix + key, safe="/-_.~"))

    def sign(self, method, path, body, now=None, query=""):
        now = now or datetime.datetime.now(datetime.timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        day = amz_date[:8]
        payload_hash = hashlib.sha256(body).hexdigest()
        signed = "host;x-amz-content-sha256;x-amz-date"
        canonical_request = "\n".join([method, path, query, "host:%s\nx-amz-content-sha256:%s\nx-amz-date:%s\n"
                                       % (self.host, payload_hash, amz_date), signed, payload_hash])
        scope = "%s/%s/s3/aws4_request" % (day, self.region)
        to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope,
                             hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()])
        k = ("AWS4" + self.secret).encode("utf-8")
        for part in (day, self.region, "s3", "aws4_request"):
            k = hmac.new(k, part.encode("utf-8"), hashlib.sha256).digest()
        signature = hmac.new(k, to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        return {"x-amz-date": amz_date, "x-amz-content-sha256": payload_hash,
                "Authorization": "AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s"
                                 % (self.access_key_id, scope, signed, signature)}

    def _request(self, method, key, body=b""):
        path = self._url_path(key)
        req = urllib.request.Request("%s://%s%s" % (self.scheme, self.host, path), data=body if method == "PUT"
                                     else None, method=method, headers=self.sign(method, path, body))
        if method == "PUT":
            req.add_header("Content-Length", str(len(body)))
            req.add_header("Content-Type", "application/octet-stream")
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                return resp.status, resp.read() if method == "GET" else b""
        except urllib.error.HTTPError as e:
            return e.code, b""
        except (OSError, ValueError) as e:
            raise BlobError("store unreachable (%s)" % type(e).__name__)

    def put(self, key, data):
        status, _ = self._request("PUT", key, data)
        if status not in (200, 201):
            raise BlobError("store refused the write (%d)" % status)

    def get(self, key):
        status, data = self._request("GET", key)
        if status == 404:
            raise BlobError("missing")
        if status != 200:
            raise BlobError("store refused the read (%d)" % status)
        return data

    def delete(self, key):
        status, _ = self._request("DELETE", key)
        if status not in (200, 204, 404):
            raise BlobError("store refused the delete (%d)" % status)


class _Cache(object):
    """Bodies are immutable under their key: a small byte-bounded LRU spares repeated reads of one message."""

    def __init__(self, limit=CACHE_BYTES):
        self.limit, self.size, self.items, self.lock = limit, 0, collections.OrderedDict(), threading.Lock()

    def get(self, key):
        with self.lock:
            text = self.items.get(key)
            if text is not None:
                self.items.move_to_end(key)
            return text

    def put(self, key, text):
        n = len(text)
        if n > self.limit // 4:
            return
        with self.lock:
            if key in self.items:
                return
            self.items[key] = text
            self.size += n
            while self.size > self.limit:
                _, old = self.items.popitem(last=False)
                self.size -= len(old)

    def drop(self, key):
        with self.lock:
            text = self.items.pop(key, None)
            if text is not None:
                self.size -= len(text)


class Blobs(object):
    """The gateway's view of its store: write a body, read a reference back, forget a key."""

    def __init__(self, store):
        self.store, self.cache = store, _Cache()

    @property
    def kind(self):
        return self.store.kind

    def write(self, text):
        """(key, reference, stored bytes) of a new body."""
        key, data = new_key(), pack(text)
        self.store.put(key, data)
        self.cache.put(key, text)
        return key, make_ref(key, text), len(data)

    def read(self, value):
        parsed = parse_ref(value)
        if parsed is None:
            return value
        key, digest = parsed
        text = self.cache.get(key)
        if text is None:
            text = unpack(self.store.get(key))
            if not hmac.compare_digest(hashlib.sha256(text.encode("utf-8")).hexdigest(), digest):
                raise BlobError("blob does not match its reference")
            self.cache.put(key, text)
        return text

    def delete(self, key):
        self.cache.drop(key)
        self.store.delete(key)


def load(home, path=None):
    """Blobs from `<home>/blobs.json`, or None when the file is absent (bodies stay in the database)."""
    home = Path(home)
    path = Path(path) if path else home / "blobs.json"
    if not path.is_file():
        return None
    cfg = C.read_json_file(path, private=True, what="blob store config")
    if not isinstance(cfg, dict) or set(cfg) - CONFIG_KEYS or cfg.get("kind") not in ("local", "s3"):
        raise ValueError("blobs.json: kind local or s3, fields %s" % ", ".join(sorted(CONFIG_KEYS)))
    if cfg["kind"] == "local":
        directory = Path(cfg.get("dir") or "blobs").expanduser()
        return Blobs(LocalStore(directory if directory.is_absolute() else home / directory))
    for field in ("endpoint", "bucket", "access_key_id", "secret_access_key_file"):
        if not isinstance(cfg.get(field), str) or not cfg[field]:
            raise ValueError("blobs.json: %s is required for kind s3" % field)
    timeout = cfg.get("timeout_seconds", 30)
    if not C.int_in(timeout, 1, 600):
        raise ValueError("blobs.json: timeout_seconds must be 1-600")
    prefix = cfg.get("prefix", "gw/")
    if not isinstance(prefix, str) or len(prefix) > 64 or not re.match(r"^[A-Za-z0-9/_.-]*$", prefix):
        raise ValueError("blobs.json: prefix may use letters, digits and / _ . -")
    secret_path = Path(cfg["secret_access_key_file"]).expanduser()
    secret_path = secret_path if secret_path.is_absolute() else home / secret_path
    try:
        if os.stat(str(secret_path)).st_mode & 0o077:
            raise ValueError("blobs.json: secret_access_key_file must not be readable by group or others (chmod 600)")
        secret = secret_path.read_text(encoding="utf-8").strip()
    except OSError as e:
        raise ValueError("blobs.json: cannot read secret_access_key_file (%s)" % type(e).__name__)
    return Blobs(S3Store(cfg["endpoint"], cfg["bucket"], cfg["access_key_id"], secret,
                         region=cfg.get("region", "auto"), prefix=prefix, timeout=timeout))
