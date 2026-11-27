"""A software stand-in for a passkey in tests: P-256 keys and WebAuthn assertions, written apart from tailmsg.py
(affine arithmetic, its own encoding), so a shared mistake cannot make a bad signature look good."""
import base64
import hashlib
import json
import secrets

P = 0xffffffff00000001000000000000000000000000ffffffffffffffffffffffff
N = 0xffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551
G = (0x6b17d1f2e12c4247f8bce6e563a440f277037d812deb33a0f4a13945d898c296,
     0x4fe342e2fe1a7f9b8ee7eb4a7c0f9e162bce33576b315ececbb6406837bf51f5)
SPKI_PREFIX = bytes.fromhex("3059301306072a8648ce3d020106082a8648ce3d030107034200")
DOMAIN = b"yobiru-owner-approval-v1\n"


def b64u(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def _add(a, b):
    if a is None:
        return b
    if b is None:
        return a
    if a[0] == b[0] and (a[1] + b[1]) % P == 0:
        return None
    if a == b:
        lam = 3 * (a[0] * a[0] - 1) * pow(2 * a[1], P - 2, P) % P
    else:
        lam = (b[1] - a[1]) * pow(b[0] - a[0], P - 2, P) % P
    x = (lam * lam - a[0] - b[0]) % P
    return x, (lam * (a[0] - x) - a[1]) % P


def _mul(k, pt):
    out = None
    while k:
        if k & 1:
            out = _add(out, pt)
        pt = _add(pt, pt)
        k >>= 1
    return out


def _der_int(v):
    raw = v.to_bytes((v.bit_length() + 8) // 8 or 1, "big")
    return b"\x02" + bytes([len(raw)]) + raw


def der(r, s):
    body = _der_int(r) + _der_int(s)
    return b"\x30" + bytes([len(body)]) + body


class SoftPasskey(object):
    def __init__(self, rp_id="yobiru.com", label="test passkey", secret=None):
        self.d = secret or (secrets.randbelow(N - 1) + 1)
        self.q = _mul(self.d, G)
        self.rp_id, self.origin, self.label = rp_id, "https://" + rp_id, label
        self.credential = secrets.token_bytes(32)

    @property
    def spki(self):
        return SPKI_PREFIX + b"\x04" + self.q[0].to_bytes(32, "big") + self.q[1].to_bytes(32, "big")

    def key(self):
        """The public half as `navigator.credentials.create` + yobiru.com/approve report it."""
        return {"alg": "ES256", "public_key": b64u(self.spki), "credential_id": b64u(self.credential),
                "rp_id": self.rp_id, "origin": self.origin, "label": self.label}

    def sign(self, message):
        z = int.from_bytes(hashlib.sha256(message).digest(), "big")
        while True:
            k = secrets.randbelow(N - 1) + 1
            r = _mul(k, G)[0] % N
            s = pow(k, N - 2, N) * (z + r * self.d) % N
            if r and s:
                return der(r, s)

    def assertion(self, challenge, flags=0x05, origin=None, kind="webauthn.get", rp_id=None, cross_origin=None):
        client = {"type": kind, "challenge": b64u(challenge), "origin": origin or self.origin}
        if cross_origin is not None:
            client["crossOrigin"] = cross_origin
        client_data = json.dumps(client).encode("utf-8")
        auth = hashlib.sha256((rp_id or self.rp_id).encode("ascii")).digest() + bytes([flags]) + b"\x00\x00\x00\x07"
        return {"credential_id": b64u(self.credential), "authenticator_data": b64u(auth),
                "client_data_json": b64u(client_data),
                "signature": b64u(self.sign(auth + hashlib.sha256(client_data).digest()))}

    def approve(self, intent, **kw):
        """An owner approval over `intent` (a dict, or exact bytes)."""
        raw = intent if isinstance(intent, bytes) else canonical(intent).encode("utf-8")
        blob = {"v": 1, "intent": b64u(raw)}
        blob.update(self.assertion(hashlib.sha256(DOMAIN + raw).digest(), **kw))
        return blob
