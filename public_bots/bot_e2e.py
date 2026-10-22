#!/usr/bin/env python3
"""End-to-end encryption of Friends message bodies (Python 3.8+, stdlib only).

A sealed body is a small JSON envelope that the gateway stores and forwards but cannot open:

    {"v": 1, "alg": "x25519-chacha20poly1305", "from": KID, "n": NONCE, "ct": CIPHERTEXT,
     "to": [{"kid": KID, "epk": EPHEMERAL_PUBLIC, "k": WRAPPED_KEY}, ...]}

A fresh 32-byte content key encrypts the body with ChaCha20-Poly1305 (RFC 8439). The content key is wrapped for
each recipient's X25519 key (RFC 7748) under HKDF-SHA256 of two Diffie-Hellman results: one with a fresh ephemeral
key (forward secrecy for the sender's side) and one with the sender's static key (only the holder of the sender's
key could have produced it, so a recipient that knows the sender's key also authenticates the sender). The
associated data binds the header and a caller-chosen context (gateway, bot, conversation, field), so the gateway
cannot move a sealed body to another conversation or field without the recipient noticing.

The primitives are written out here because the project has no third-party dependencies (when the `cryptography`
package happens to be installed, its ChaCha20-Poly1305 is used instead: same algorithm, same bytes, faster). They are
checked against the RFC test vectors in tests/test_friends_e2e.py. They are not constant time; they run only on the devices that
hold the keys (never on the gateway), where a remote timing attack has nothing to measure.
"""
import base64
import hashlib
import hmac
import json
import os
import struct

ALG = "x25519-chacha20poly1305"
VERSION = 1
KID_HEX = 32
MAX_RECIPIENTS = 4
INFO_WRAP = b"yobiru-e2e-v1 wrap"
INFO_SEED = b"yobiru-e2e-v1 seed"


class E2EError(ValueError):
    """A sealed body could not be opened or does not match what was expected."""


# ---------------------------------------------------------------- encoding
def b64e(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64d(text, size=None):
    if not isinstance(text, str) or len(text) > 1 << 22 or not all(c.isalnum() or c in "-_" for c in text):
        raise E2EError("not base64url")
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (ValueError, TypeError):
        raise E2EError("not base64url")
    if b64e(raw) != text or (size is not None and len(raw) != size):
        raise E2EError("bad length or encoding")
    return raw


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


# ---------------------------------------------------------------- X25519 (RFC 7748)
_P = 2 ** 255 - 19
_A24 = 121665


def _clamp(k):
    k = bytearray(k)
    k[0] &= 248
    k[31] &= 127
    k[31] |= 64
    return int.from_bytes(bytes(k), "little")


def x25519(scalar, u):
    if len(scalar) != 32 or len(u) != 32:
        raise E2EError("X25519 keys are 32 bytes")
    k = _clamp(scalar)
    x1 = int.from_bytes(u, "little") & ((1 << 255) - 1)
    x2, z2, x3, z3, swap = 1, 0, x1, 1, 0
    for t in range(254, -1, -1):
        bit = (k >> t) & 1
        swap ^= bit
        if swap:
            x2, x3, z2, z3 = x3, x2, z3, z2
        swap = bit
        a, b = x2 + z2, x2 - z2
        aa, bb = a * a % _P, b * b % _P
        e = aa - bb
        c, d = x3 + z3, x3 - z3
        da, cb = d * a % _P, c * b % _P
        x3 = (da + cb) ** 2 % _P
        z3 = x1 * (da - cb) ** 2 % _P
        x2 = aa * bb % _P
        z2 = e * (aa + _A24 * e) % _P
    if swap:
        x2, z2 = x3, z3
    return (x2 * pow(z2, _P - 2, _P) % _P).to_bytes(32, "little")


_BASE = (9).to_bytes(32, "little")


def public_key(secret):
    return x25519(secret, _BASE)


def _dh(secret, public):
    shared = x25519(secret, public)
    if not any(shared):
        raise E2EError("low-order public key")
    return shared


# ---------------------------------------------------------------- ChaCha20-Poly1305 (RFC 8439)
_M32 = 0xFFFFFFFF


def _quarter(s, a, b, c, d):
    s[a] = (s[a] + s[b]) & _M32
    s[d] ^= s[a]
    s[d] = ((s[d] << 16) | (s[d] >> 16)) & _M32
    s[c] = (s[c] + s[d]) & _M32
    s[b] ^= s[c]
    s[b] = ((s[b] << 12) | (s[b] >> 20)) & _M32
    s[a] = (s[a] + s[b]) & _M32
    s[d] ^= s[a]
    s[d] = ((s[d] << 8) | (s[d] >> 24)) & _M32
    s[c] = (s[c] + s[d]) & _M32
    s[b] ^= s[c]
    s[b] = ((s[b] << 7) | (s[b] >> 25)) & _M32


def chacha20_block(key, counter, nonce):
    state = [0x61707865, 0x3320646E, 0x79622D32, 0x6B206574] + list(struct.unpack("<8L", key)) + \
        [counter & _M32] + list(struct.unpack("<3L", nonce))
    s = list(state)
    for _ in range(10):
        _quarter(s, 0, 4, 8, 12)
        _quarter(s, 1, 5, 9, 13)
        _quarter(s, 2, 6, 10, 14)
        _quarter(s, 3, 7, 11, 15)
        _quarter(s, 0, 5, 10, 15)
        _quarter(s, 1, 6, 11, 12)
        _quarter(s, 2, 7, 8, 13)
        _quarter(s, 3, 4, 9, 14)
    return struct.pack("<16L", *[(x + y) & _M32 for x, y in zip(s, state)])


def chacha20(key, counter, nonce, data):
    out = bytearray()
    for i in range(0, len(data), 64):
        block = chacha20_block(key, counter + i // 64, nonce)
        chunk = data[i:i + 64]
        out += (int.from_bytes(chunk, "little") ^ int.from_bytes(block[:len(chunk)], "little")).to_bytes(
            len(chunk), "little")
    return bytes(out)


def poly1305(key, msg):
    r = int.from_bytes(key[:16], "little") & 0x0FFFFFFC0FFFFFFC0FFFFFFC0FFFFFFF
    s = int.from_bytes(key[16:], "little")
    p, acc = (1 << 130) - 5, 0
    for i in range(0, len(msg), 16):
        chunk = msg[i:i + 16]
        acc = (acc + int.from_bytes(chunk + b"\x01", "little")) * r % p
    return ((acc + s) & ((1 << 128) - 1)).to_bytes(16, "little")


def _pad16(n):
    return b"\x00" * (-n % 16)


def _tag(key, nonce, aad, ct):
    otk = chacha20_block(key, 0, nonce)[:32]
    mac = aad + _pad16(len(aad)) + ct + _pad16(len(ct)) + struct.pack("<QQ", len(aad), len(ct))
    return poly1305(otk, mac)


def py_aead_encrypt(key, nonce, plaintext, aad=b""):
    ct = chacha20(key, 1, nonce, plaintext)
    return ct + _tag(key, nonce, aad, ct)


def py_aead_decrypt(key, nonce, data, aad=b""):
    if len(data) < 16:
        raise E2EError("ciphertext too short")
    ct, tag = data[:-16], data[-16:]
    if not hmac.compare_digest(_tag(key, nonce, aad, ct), tag):
        raise E2EError("authentication failed")
    return chacha20(key, 1, nonce, ct)


try:        # optional: the same algorithm from the `cryptography` package, much faster for large bodies
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305 as _Fast
    from cryptography.exceptions import InvalidTag as _InvalidTag
except Exception:       # noqa: BLE001 - any import problem means the stdlib implementation is used
    _Fast = None


def aead_encrypt(key, nonce, plaintext, aad=b""):
    if _Fast is not None:
        return _Fast(key).encrypt(nonce, plaintext, aad)
    return py_aead_encrypt(key, nonce, plaintext, aad)


def aead_decrypt(key, nonce, data, aad=b""):
    if _Fast is not None:
        if len(data) < 16:
            raise E2EError("ciphertext too short")
        try:
            return _Fast(key).decrypt(nonce, data, aad)
        except _InvalidTag:
            raise E2EError("authentication failed")
    return py_aead_decrypt(key, nonce, data, aad)


# ---------------------------------------------------------------- HKDF-SHA256 (RFC 5869)
def hkdf(ikm, salt, info, length=32):
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    out, block, i = b"", b"", 1
    while len(out) < length:
        block = hmac.new(prk, block + info + bytes([i]), hashlib.sha256).digest()
        out += block
        i += 1
    return out[:length]


# ---------------------------------------------------------------- keys
def generate():
    """-> (secret, public) as base64url strings."""
    secret = os.urandom(32)
    return b64e(secret), b64e(public_key(secret))


def kid(public):
    """A key's id: the first 128 bits of a hash of its public key, in hex. Also what people compare."""
    raw = b64d(public, 32) if isinstance(public, str) else public
    return hashlib.sha256(b"yobiru-e2e-v1 key" + raw).hexdigest()[:KID_HEX]


def fingerprint(public):
    """A kid grouped for reading aloud or comparing: 8 groups of 4 hex digits."""
    k = kid(public)
    return " ".join(k[i:i + 4] for i in range(0, len(k), 4))


def key_error(public):
    """None for a usable X25519 public key (base64url, 32 bytes, not low order), else why not."""
    try:
        raw = b64d(public, 32)
    except E2EError as e:
        return str(e)
    if not any(x25519(b"\x01" + b"\x00" * 31, raw)) or not any(raw):
        return "low-order key"
    return None


# ---------------------------------------------------------------- envelopes
def _header(sender_kid, kids, context):
    return canonical({"v": VERSION, "alg": ALG, "from": sender_kid, "to": kids, "ctx": context})


def seal(plaintext, sender_secret, recipients, context, seed=None):
    """Encrypt `plaintext` (bytes) for each X25519 public key in `recipients` (base64url), authenticated as the
    holder of `sender_secret`. With `seed` (bytes) the envelope is deterministic: the same seed, body, keys and
    context give the identical envelope, so a retried request carries the same bytes."""
    if not 1 <= len(recipients) <= MAX_RECIPIENTS:
        raise E2EError("1-%d recipients" % MAX_RECIPIENTS)
    sk = b64d(sender_secret, 32)
    spk = public_key(sk)
    rnd = (lambda label, n: hkdf(seed, b"", INFO_SEED + b" " + label, n)) if seed is not None else \
        (lambda label, n: os.urandom(n))
    keys, seen = [], set()
    for pub in recipients:
        raw = b64d(pub, 32)
        if raw not in seen:
            seen.add(raw)
            keys.append(raw)
    kids = [kid(r) for r in keys]
    aad = _header(kid(spk), kids, context)
    cek, nonce = rnd(b"cek", 32), rnd(b"nonce", 12)
    wraps = []
    for i, rpk in enumerate(keys):
        esk = rnd(b"eph %d" % i, 32)
        epk = public_key(esk)
        kek = hkdf(_dh(esk, rpk) + _dh(sk, rpk), epk + rpk + spk, INFO_WRAP)
        wraps.append({"kid": kids[i], "epk": b64e(epk), "k": b64e(aead_encrypt(kek, b"\x00" * 12, cek, aad))})
    return {"v": VERSION, "alg": ALG, "from": kid(spk), "n": b64e(nonce), "ct": b64e(aead_encrypt(cek, nonce,
                                                                                                    plaintext, aad)),
            "to": wraps}


def envelope_error(env, max_ct_bytes):
    """Structural check (no keys needed), for the gateway: None or the reason the envelope is refused."""
    if not isinstance(env, dict) or set(env) != {"v", "alg", "from", "n", "ct", "to"}:
        return "a sealed body is exactly {v, alg, from, n, ct, to}"
    if env["v"] != VERSION or env["alg"] != ALG:
        return "unsupported sealed body version or algorithm"
    if not isinstance(env["from"], str) or len(env["from"]) != KID_HEX or \
            any(c not in "0123456789abcdef" for c in env["from"]):
        return "from is a key id"
    if not isinstance(env["to"], list) or not 1 <= len(env["to"]) <= MAX_RECIPIENTS:
        return "to lists 1-%d recipients" % MAX_RECIPIENTS
    try:
        b64d(env["n"], 12)
        if not isinstance(env["ct"], str) or len(env["ct"]) > (max_ct_bytes + 16) * 4 // 3 + 4:
            return "sealed body too large"
        if len(b64d(env["ct"])) < 16:
            return "ciphertext too short"
        kids = set()
        for w in env["to"]:
            if not isinstance(w, dict) or set(w) != {"kid", "epk", "k"} or not isinstance(w["kid"], str) or \
                    len(w["kid"]) != KID_HEX or w["kid"] in kids:
                return "each recipient is {kid, epk, k} with a distinct key id"
            kids.add(w["kid"])
            b64d(w["epk"], 32)
            b64d(w["k"], 48)
    except E2EError as e:
        return "sealed body: %s" % e
    return None


def ciphertext_bytes(env):
    return len(b64d(env["ct"])) - 16


def recipients_of(env):
    return [w["kid"] for w in env.get("to") or []] if isinstance(env, dict) else []


def open_sealed(env, my_secret, sender_public, context):
    """Decrypt an envelope addressed to `my_secret`, written by the holder of `sender_public`, for `context`.
    Raises E2EError when it is not addressed to this key, was not written by that sender, or was changed."""
    err = envelope_error(env, 1 << 22)
    if err:
        raise E2EError(err)
    sk = b64d(my_secret, 32)
    mpk = public_key(sk)
    spk = b64d(sender_public, 32)
    if env["from"] != kid(spk):
        raise E2EError("sealed by another key than the expected sender's")
    mine = [w for w in env["to"] if w["kid"] == kid(mpk)]
    if not mine:
        raise E2EError("not sealed for this key")
    aad = _header(env["from"], [w["kid"] for w in env["to"]], context)
    epk = b64d(mine[0]["epk"], 32)
    kek = hkdf(_dh(sk, epk) + _dh(sk, spk), epk + mpk + spk, INFO_WRAP)
    cek = aead_decrypt(kek, b"\x00" * 12, b64d(mine[0]["k"], 48), aad)
    return aead_decrypt(cek, b64d(env["n"], 12), b64d(env["ct"]), aad)


def seed_for(sender_secret, *parts):
    """A deterministic-envelope seed only the sender can compute, from the sender's key and the request's identity
    (op key, payload hash): retries are byte-identical, and nobody else can predict the ephemeral keys."""
    return hmac.new(b64d(sender_secret, 32), canonical(list(parts)), hashlib.sha256).digest()
