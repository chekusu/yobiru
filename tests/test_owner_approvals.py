"""Owner approvals (0.7.30): passkey signatures over exact instructions, verified by the receiver's own code.

Covers the P-256 verifier against RFC 6979 vectors, a real Chromium WebAuthn assertion, the strict intent
schema, every WebAuthn binding (challenge, origin, rp id, user presence and verification), audience, requester
and file binding, the owner key store, and the receiver's ledger (uses, authorization). Isolated homes only.
"""
import argparse
import contextlib
import datetime as dt
import io
import importlib.util
import json
import os
import secrets
import tempfile
import unittest
from pathlib import Path

from support.pkg import REPO
from support.softpasskey import SoftPasskey, b64u, canonical, der

spec = importlib.util.spec_from_file_location("approval_core", REPO / "tailmsg.py")
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)

HOST, SENDER = "studio.example.ts.net", "grok-bot-1.example.ts.net"
HOST_NODE = core.stable_peer_id("nStudio1CNTRL")
SENDER_NODE = core.stable_peer_id("nGrok1CNTRL")
MID = "8899aabbccddeeff"


def intent(**over):
    now = core.now_utc()
    value = {"type": core.APPROVAL_TYPE, "v": 1, "to": {"host": HOST, "agent": "claude", "node": HOST_NODE},
             "from": {"host": SENDER, "node": SENDER_NODE}, "instruction": "Run the yobiru tests and open a draft PR",
             "files": [], "uses": 1, "nonce": b64u(secrets.token_bytes(16)), "issued_at": core.iso(now),
             "expires_at": core.iso(now + dt.timedelta(hours=1))}
    value.update(over)
    return value


class P256Tests(unittest.TestCase):
    # RFC 6979 A.2.5, P-256 with SHA-256
    Q = (0x60FED4BA255A9D31C961EB74C6356D68C049B8923B61FA6CE669622E60F29FB6,
         0x7903FE1008B8BC99A41AE9E95628BC64F2F1B20C2D7E9F5177A3C294D4462299)
    VECTORS = ((b"sample", 0xEFD48B2AACB6A8FD1140DD9CD45E81D69D2C877B56AAF991C34D0EA84EAF3716,
                0xF7CB1C942D657C41D436C7A1B6E29F65F3E900DBB9AFF4064DC4AB2F843ACDA8),
               (b"test", 0xF1ABB023518351CD71D881567B1EA663ED3EFCF6C5132B354F28D3B0B7D38367,
                0x019F4113742A2B14BD25926B49C649155F267E60D3814B4C0CC84250E46F0083))

    def test_rfc6979_vectors(self):
        for msg, r, s in self.VECTORS:
            self.assertTrue(core.p256_verify(self.Q, msg, der(r, s)))
            self.assertFalse(core.p256_verify(self.Q, msg + b"!", der(r, s)))
            self.assertFalse(core.p256_verify(self.Q, msg, der(r, s ^ 1)))
            self.assertFalse(core.p256_verify(self.Q, msg, der(r, core._P256_N - s + 1)))

    def test_signature_encoding_is_strict_der(self):
        msg, r, s = self.VECTORS[0]
        good = der(r, s)
        for bad in (good + b"\x00", good[:-1], b"\x31" + good[1:], good[:2] + b"\x02\x22\x00" + good[4:],
                    b"\x30\x06\x02\x01\x00\x02\x01\x01", b""):
            with self.subTest(bad=bad.hex()):
                with self.assertRaises(core.ApprovalError):
                    if not core.p256_verify(self.Q, msg, bad):
                        raise core.ApprovalError("rejected")
        for value in (0, core._P256_N):
            self.assertFalse(core.p256_verify(self.Q, msg, der(value, s)))

    def test_soft_passkey_and_point_checks(self):
        pk = SoftPasskey()
        point = core.p256_point(pk.spki)
        self.assertTrue(core.p256_verify(point, b"hello", pk.sign(b"hello")))
        off_curve = bytearray(pk.spki)
        off_curve[-1] ^= 1
        with self.assertRaises(core.ApprovalError):
            core.p256_point(bytes(off_curve))
        with self.assertRaises(core.ApprovalError):
            core.p256_point(pk.spki[:-1])


class ChromiumFixtureTests(unittest.TestCase):
    """A real assertion from Chromium's virtual authenticator, as yobiru.com/approve makes them."""

    def setUp(self):
        fixture = json.loads((REPO / "tests" / "fixtures" / "owner_approval_chromium.json").read_text("utf-8"))
        self.key, self.approval = core.owner_key_record(fixture["key"]), fixture["approval"]
        _, self.intent, _ = core.parse_approval(self.approval)
        self.now = core.parse_iso(self.intent["issued_at"]) + dt.timedelta(minutes=1)

    def test_verifies_in_time_only(self):
        check = core.verify_approval(self.approval, [self.key], now=self.now)
        self.assertEqual(check["key"]["id"], self.key["id"])
        self.assertIn("测试", check["intent"]["instruction"])
        with self.assertRaisesRegex(core.ApprovalError, "expired"):
            core.verify_approval(self.approval, [self.key], now=self.now + dt.timedelta(hours=2))
        with self.assertRaisesRegex(core.ApprovalError, "future"):
            core.verify_approval(self.approval, [self.key], now=self.now - dt.timedelta(minutes=30))
        text = core.approval_text(self.approval)
        self.assertTrue(text.startswith("yobiru-approval:"))
        self.assertEqual(core.verify_approval(text, [self.key], now=self.now)["digest"], check["digest"])

    def test_any_change_breaks_it(self):
        raw = json.loads(core.unb64u(self.approval["intent"]).decode("utf-8"))
        raw["instruction"] += "，然后删掉所有分支"
        changed = dict(self.approval, intent=b64u(canonical(raw).encode("utf-8")))
        with self.assertRaisesRegex(core.ApprovalError, "different content"):
            core.verify_approval(changed, [self.key], now=self.now)
        sig = bytearray(core.unb64u(self.approval["signature"]))
        sig[-1] ^= 1
        with self.assertRaises(core.ApprovalError):
            core.verify_approval(dict(self.approval, signature=b64u(bytes(sig))), [self.key], now=self.now)
        with self.assertRaisesRegex(core.ApprovalError, "does not trust"):
            core.verify_approval(self.approval, [core.owner_key_record(SoftPasskey().key())], now=self.now)
        with self.assertRaisesRegex(core.ApprovalError, "no owner key"):
            core.verify_approval(self.approval, [], now=self.now)


class IntentTests(unittest.TestCase):
    def setUp(self):
        self.pk = SoftPasskey()
        self.keys = [core.owner_key_record(self.pk.key())]

    def verify(self, value, **kw):
        return core.verify_approval(self.pk.approve(value, **kw), self.keys)

    def test_good_approval(self):
        check = self.verify(intent())
        self.assertEqual(check["intent"]["to"]["agent"], "claude")
        self.assertEqual(len(check["digest"]), 64)

    def test_schema_is_strict(self):
        cases = {
            "unknown fields": intent(extra=1),
            "missing fields": {k: v for k, v in intent().items() if k != "uses"},
            "uses": intent(uses=0),
            "7 days": intent(expires_at=core.iso(core.now_utc() + dt.timedelta(days=8))),
            "bidirectional": intent(instruction="pay ‮redro‬"),
            "1 to": intent(instruction=" "),
            "recipient": intent(to={"host": HOST, "agent": "claude", "port": 1}),
            "requester": intent(**{"from": {"user": "x"}}),
            "approved file": intent(files=[{"name": "../a", "sha256": "0" * 64, "size": 1}]),
            "nonce": intent(nonce="short"),
            "version": intent(v=2),
            "approval type": intent(type="yobiru.something-else"),
        }
        for reason, value in cases.items():
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(core.ApprovalError, reason):
                    self.verify(value)

    def test_a_friends_bot_and_one_of_its_messages(self):
        bot = {"gateway": "https://bots.example.com", "bot": "bot_aaaaaaaaaaaaaaaa", "handle": "claude"}
        good = intent(to=bot, message="msg_bbbbbbbbbbbbbbbb",
                      **{"from": {"principal": "bot_cccccccccccccccc", "handle": "grok"}})
        self.assertEqual(self.verify(good)["intent"]["to"], bot)
        self.assertEqual(self.verify(dict(good, **{"from": {"principal": "act_dddddddddddddddd"}}))["intent"]["uses"], 1)
        self.assertEqual(self.verify(dict(good, to=dict(bot, gateway="http://127.0.0.1:7780/gw")))["intent"]["message"],
                         "msg_bbbbbbbbbbbbbbbb")
        cases = {
            "names the message": {k: v for k, v in good.items() if k != "message"},
            "requester": dict(good, **{"from": {"host": SENDER}}),
            "requester handle": dict(good, **{"from": {"principal": "act_dddddddddddddddd", "handle": "x-man"}}),
            "approved message": dict(good, message="msg_short"),
            "used once": dict(good, uses=2),
            "recipient bot": dict(good, to=dict(bot, bot="act_aaaaaaaaaaaaaaaa")),
            "recipient handle": dict(good, to=dict(bot, handle="@claude")),
            "approval recipient": dict(good, to=dict(bot, agent="claude")),
        }
        for gateway in ("http://bots.example.com", "https://bots.example.com/", "https://Bots.example.com",
                        "https://bots.example.com:443", "https://u@bots.example.com", "https://bots.example.com/?q",
                        "https://bots.example.com/a/../b", "bots.example.com", "https://bots.example.com/a b"):
            cases["gateway " + gateway] = dict(good, to=dict(bot, gateway=gateway))
        for reason, value in cases.items():
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(core.ApprovalError, reason.split(" ")[0] if reason.startswith("gateway")
                                            else reason):
                    self.verify(value)
        with self.assertRaisesRegex(core.ApprovalError, "only an approval for a bot"):
            self.verify(intent(message="msg_bbbbbbbbbbbbbbbb"))
        self.assertEqual(core.recipient_label(bot), "@claude on https://bots.example.com")

    def test_bytes_must_be_canonical(self):
        raw = json.dumps(intent(), sort_keys=False).encode("utf-8")
        with self.assertRaisesRegex(core.ApprovalError, "canonical"):
            self.verify(raw)
        with self.assertRaisesRegex(core.ApprovalError, "JSON"):
            self.verify(b'{"type": NaN}')

    def test_webauthn_bindings(self):
        cases = {
            "the device did not check its owner": {"flags": 0x01},
            "did not confirm presence": {"flags": 0x04},
            "different site than https://yobiru.com": {"origin": "https://yobiru.com.evil.example"},
            "different site than yobiru.com": {"rp_id": "evil.example"},
            "not a WebAuthn signature": {"kind": "webauthn.create"},
            "another site's frame": {"cross_origin": True},
        }
        for reason, kw in cases.items():
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(core.ApprovalError, reason):
                    self.verify(intent(), **kw)
        other = SoftPasskey()
        blob = self.pk.approve(intent())
        forged = dict(blob, signature=other.approve(core.unb64u(blob["intent"]))["signature"])
        with self.assertRaisesRegex(core.ApprovalError, "does not verify"):
            core.verify_approval(forged, self.keys)

    def test_only_listed_kinds(self):
        blob = self.pk.approve({"type": core.KEY_REMOVE_TYPE, "v": 1, "to": {"host": HOST}, "key_id": "a" * 64,
                                "nonce": b64u(secrets.token_bytes(16)), "issued_at": intent()["issued_at"],
                                "expires_at": intent()["expires_at"]})
        with self.assertRaisesRegex(core.ApprovalError, "something else"):
            core.verify_approval(blob, self.keys)
        core.verify_approval(blob, self.keys, kinds=(core.KEY_REMOVE_TYPE,))

    def test_audience_requester_and_files(self):
        value = core.validate_intent(intent())
        self.assertIsNone(core.approval_audience_error(value, HOST, HOST_NODE, "claude", "known"))
        self.assertIsNone(core.approval_audience_error(value, HOST.upper() + ".", HOST_NODE, "claude", "default"))
        self.assertIn("not this machine", core.approval_audience_error(value, "other.example.ts.net", HOST_NODE,
                                                                       "claude", "known"))
        self.assertIn("another machine", core.approval_audience_error(value, HOST, SENDER_NODE, "claude", "known"))
        self.assertIn("agent claude", core.approval_audience_error(value, HOST, HOST_NODE, "codex", "known"))
        self.assertIn("agent claude", core.approval_audience_error(value, HOST, HOST_NODE, "claude", "unknown"))
        short = core.validate_intent(intent(to={"host": "studio"}))
        self.assertIsNone(core.approval_audience_error(short, HOST, None, "claude", "default"))
        self.assertIn("default agent", core.approval_audience_error(short, HOST, None, "codex", "known"))

        sender = {"node": SENDER, "stable_id": "nGrok1CNTRL"}
        self.assertIsNone(core.approval_requester_error(value, sender))
        self.assertIn("another machine", core.approval_requester_error(value, dict(sender, stable_id="nOther")))
        self.assertIn("from " + SENDER, core.approval_requester_error(
            value, {"node": "evil.example.ts.net", "stable_id": "nGrok1CNTRL"}))
        anyone = core.validate_intent({k: v for k, v in intent().items() if k != "from"})
        self.assertIsNone(core.approval_requester_error(anyone, {}))

        files = [{"name": "plan.md", "sha256": "ab" * 32, "size": 12}]
        with_files = core.validate_intent(intent(files=files))
        push = {"fetch": [{"name": "plan.md", "url": "http://x/plan.md", "sha256": "ab" * 32, "size": 12}]}
        pull = {"request": {"transport": "pull", "uploads": [{"name": "renamed.md", "sha256": "ab" * 32, "size": 12}]}}
        self.assertIsNone(core.approval_files_error(with_files, push))
        self.assertIsNone(core.approval_files_error(with_files, pull))
        self.assertIsNotNone(core.approval_files_error(with_files, {}))
        self.assertIsNotNone(core.approval_files_error(value, push))
        two = dict(push, fetch=push["fetch"] * 2)
        self.assertIsNotNone(core.approval_files_error(with_files, two))


class KeyStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.first, self.second = SoftPasskey(label="phone"), SoftPasskey(label="laptop")

    def key_intent(self, kind, extra, host=HOST):
        value = {"type": kind, "v": 1, "to": {"host": host, "node": HOST_NODE}, "nonce": b64u(secrets.token_bytes(16)),
                 "issued_at": core.iso(core.now_utc()), "expires_at": core.iso(core.now_utc() + dt.timedelta(hours=1))}
        value.update(extra)
        return value

    def test_first_key_then_signed_changes_only(self):
        self.assertEqual(core.load_owner_keys(self.home), [])
        added = core.owner_key_add(self.home, self.first.key(), None, HOST, HOST_NODE)
        self.assertTrue(added["added"])
        self.assertEqual(added["key"]["added_via"], "first")
        if os.name == "posix":
            self.assertEqual(core.owner_keys_path(self.home).stat().st_mode & 0o777, 0o600)
        self.assertFalse(core.owner_key_add(self.home, self.first.key())["added"])
        with self.assertRaisesRegex(core.ApprovalError, "already trusts"):
            core.owner_key_add(self.home, self.second.key(), None, HOST, HOST_NODE)
        # Signed by the new key itself: not a key this machine trusts.
        selfsigned = self.second.approve(self.key_intent(core.KEY_ADD_TYPE, {"key": self.second.key()}))
        with self.assertRaisesRegex(core.ApprovalError, "does not trust"):
            core.owner_key_add(self.home, self.second.key(), selfsigned, HOST, HOST_NODE)
        elsewhere = self.first.approve(self.key_intent(core.KEY_ADD_TYPE, {"key": self.second.key()},
                                                       host="other.example.ts.net"))
        with self.assertRaisesRegex(core.ApprovalError, "not this machine"):
            core.owner_key_add(self.home, self.second.key(), elsewhere, HOST, HOST_NODE)
        third = SoftPasskey()
        wrong = self.first.approve(self.key_intent(core.KEY_ADD_TYPE, {"key": third.key()}))
        with self.assertRaisesRegex(core.ApprovalError, "different key"):
            core.owner_key_add(self.home, self.second.key(), wrong, HOST, HOST_NODE)
        good = self.first.approve(self.key_intent(core.KEY_ADD_TYPE, {"key": self.second.key()}))
        added = core.owner_key_add(self.home, self.second.key(), good, HOST, HOST_NODE)
        self.assertEqual((added["key"]["added_via"], added["keys"]), ("signed", 2))

        target = core.owner_key_record(self.first.key())["id"]
        with self.assertRaises(core.ApprovalError):
            core.owner_key_remove(self.home, target, None, HOST, HOST_NODE)
        removal = self.second.approve(self.key_intent(core.KEY_REMOVE_TYPE, {"key_id": target}))
        removed = core.owner_key_remove(self.home, target, removal, HOST, HOST_NODE)
        self.assertEqual(removed["keys"], 1)
        self.assertEqual([k["label"] for k in core.load_owner_keys(self.home)], ["laptop"])
        events = json.loads(core.owner_keys_path(self.home).read_text("utf-8"))["events"]
        self.assertEqual([e["event"] for e in events], ["added", "added", "removed"])

    def test_damaged_file_fails_closed_and_text_round_trips(self):
        core.owner_key_add(self.home, self.first.key())
        rec = core.load_owner_keys(self.home)[0]
        again = core.owner_key_from_text(core.owner_key_text(rec))
        self.assertEqual((again["id"], again["label"]), (rec["id"], "phone"))
        core.owner_keys_path(self.home).write_text("{not json", "utf-8")
        with self.assertRaisesRegex(core.ApprovalError, "damaged"):
            core.load_owner_keys(self.home)
        info = core.info_payload(self.home, {"default_agent": None}, {}, HOST, lambda b: None)
        self.assertEqual((info["owner_keys"], info["owner_keys_error"]), ([], "damaged"))
        self.assertIn(core.OWNER_APPROVAL_CAPABILITY, info["capabilities"])

    def test_key_records_are_checked(self):
        bad = dict(self.first.key(), origin="https://evil.example")
        with self.assertRaisesRegex(core.ApprovalError, "origin"):
            core.owner_key_record(bad)
        with self.assertRaisesRegex(core.ApprovalError, "unknown fields"):
            core.owner_key_record(dict(self.first.key(), id="x"))
        with self.assertRaisesRegex(core.ApprovalError, "ES256"):
            core.owner_key_record(dict(self.first.key(), alg="RS256"))


class ReceiverTests(unittest.TestCase):
    """The receiver's own verdict, its ledger record and how bridges read it."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ctx = core.Ctx(Path(self.temp.name) / "b")
        self.pk = SoftPasskey()
        core.owner_key_add(self.ctx.home, self.pk.key())
        self.ident = {"node": SENDER, "stable_id": "nGrok1CNTRL"}

    def env(self, approval, mid=MID, **over):
        value = {"v": 1, "id": mid, "from": SENDER, "to": HOST, "ts": core.iso(core.now_utc()), "kind": "request",
                 "text": "Please run the tests", "agent": "claude", "owner_approval": approval,
                 "request": {"timeout_seconds": 900, "reply_port": 7766}}
        value.update(over)
        return value

    def receive(self, env, agent=("claude", "known")):
        self.assertIsNone(core.validate_env(env))
        verdict = core.check_incoming_approval(self.ctx.home, env, self.ident, HOST, HOST_NODE, *agent)
        rec = {"envelope": env, "received": {"at": env["ts"], "peer": self.ident, "node": HOST, "agent": agent[0],
                                             "agent_status": agent[1], "owner_approval": verdict}}
        core.accept_request(self.ctx, rec)
        return rec

    def test_verified_request_is_authorized_once(self):
        approval = self.pk.approve(intent(uses=1))
        rec = self.receive(self.env(approval))
        verdict = rec["received"]["owner_approval"]
        self.assertTrue(verdict["verified"], verdict)
        self.assertEqual(verdict["uses"], {"allowed": 1, "used": 1})
        status = core.owner_approval_status(self.ctx.home, rec)
        self.assertEqual(status["authorization_status"], "active")
        view = core.authorization_snapshot(self.ctx.home, verdict["authorization_id"])
        self.assertEqual((view["issuer_kind"], view["issuer_ref"]), ("owner_signature", "key:" + verdict["key"]["id"]))
        task = core.request_snapshot(self.ctx.home, MID)
        self.assertEqual(task["state"], "received")

        again = self.receive(self.env(approval, mid="8899aabbccddee00"))
        self.assertFalse(again["received"]["owner_approval"]["verified"])
        self.assertIn("already used 1", again["received"]["owner_approval"]["reason"])
        self.assertIsNone(core.owner_approval_status(self.ctx.home, {"received": {}}))
        self.assertTrue(core.owner_approved(self.ctx.home, rec))
        self.assertFalse(core.owner_approved(self.ctx.home, again))
        self.assertFalse(core.owner_approved(self.ctx.home, {"received": {}}))
        # `show` and `inbox --json` report the verdict with its authorization's state now, not as stored
        for record in (rec, again):
            core.atomic_write(self.ctx.inbox / (record["envelope"]["id"] + ".json"),
                              json.dumps(record).encode("utf-8"))
        shown = io.StringIO()
        with contextlib.redirect_stdout(shown):
            core.cmd_show(self.ctx, argparse.Namespace(id=MID))
        self.assertEqual(json.loads(shown.getvalue())["received"]["owner_approval"]["authorization_status"], "active")
        listed = io.StringIO()
        with contextlib.redirect_stdout(listed):
            core.cmd_inbox(self.ctx, argparse.Namespace(pending=False, agent=None, json=True))
        self.assertEqual({row["id"]: row["owner_approved"] for row in json.loads(listed.getvalue())},
                         {MID: True, "8899aabbccddee00": False})

    def test_wrong_audience_or_unknown_agent_never_authorizes(self):
        rec = self.receive(self.env(self.pk.approve(intent())), agent=("codex", "known"))
        self.assertFalse(rec["received"]["owner_approval"]["verified"])
        self.assertIn("agent claude", rec["received"]["owner_approval"]["reason"])
        rec = self.receive(self.env(self.pk.approve(intent(to={"host": HOST, "agent": "ghost"})),
                                    mid="0000000000000001", agent="ghost"), agent=("ghost", "unknown"))
        self.assertFalse(rec["received"]["owner_approval"]["verified"])
        forged = dict(self.pk.approve(intent()), signature=b64u(der(1, 1)))
        rec = self.receive(self.env(forged, mid="0000000000000002"))
        self.assertEqual(rec["received"]["owner_approval"], {"verified": False, "reason": "the signature does not verify"})

    def test_envelope_rules(self):
        approval = self.pk.approve(intent())
        self.assertIn("owner_approval", core.validate_env({k: v for k, v in self.env(approval).items()
                                                           if k != "request"}))
        self.assertIn("owner_approval", core.validate_env(self.env(dict(approval, extra="x"))))
        # the largest approval a receiver verifies also fits the envelope (4,000 characters of CJK and 20 files)
        files = [{"name": "f%02d.txt" % i, "sha256": "%064x" % i, "size": i} for i in range(20)]
        largest = self.pk.approve(intent(instruction="部" * 4000, files=files))
        self.assertGreater(len(json.dumps(largest)), 32768)
        self.assertIsNone(core.validate_env(self.env(largest)))
        self.assertIn("owner_approval", core.validate_env(self.env(dict(largest, intent=largest["intent"] + "A" * 20000))))
        self.assertEqual(core.task_fingerprint(self.env(approval)), core.task_fingerprint(
            {k: v for k, v in self.env(approval).items() if k != "owner_approval"}))


if __name__ == "__main__":
    unittest.main()
