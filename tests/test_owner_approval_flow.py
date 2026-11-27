"""`yobiru approval` end to end over a real gateway's approval drops, with a software passkey playing the part of
yobiru.com/approve (the browser test of the page itself lives in the yobiru.com repository).

The gateway runs as a subprocess on loopback; nothing here touches Tailscale: this machine's identity and a peer's
/info are stubbed where the command would ask Tailscale for them."""
import contextlib
import importlib.util
import io
import json
import os
import tempfile
import types
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from support.pkg import REPO
from support.public_harness import C, Gateway
from support.softpasskey import SoftPasskey, b64u

SCRIPTS = REPO / "plugins" / "yobiru" / "scripts"
spec = importlib.util.spec_from_file_location("approval_flow_core", REPO / "tailmsg.py")
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)
spec = importlib.util.spec_from_file_location("yobiru_approval_under_test", SCRIPTS / "yobiru_approval.py")
approval = importlib.util.module_from_spec(spec)
spec.loader.exec_module(approval)
LAUNCHER = types.SimpleNamespace(core=lambda: core)

HOST, SENDER = "studio.example.ts.net", "grok-bot-1.example.ts.net"
HOST_NODE = core.stable_peer_id("nStudio1CNTRL")
SENDER_NODE = core.stable_peer_id("nGrok1CNTRL")


class Page(object):
    """What yobiru.com/approve does with a link, done with a software passkey."""

    def __init__(self, link):
        frag = urllib.parse.parse_qs(urllib.parse.urlsplit(link).fragment)
        relay = frag["r"][0]
        self.relay = relay if "://" in relay else "https://" + relay
        self.did, self.key = approval.drop_parts(core.unb64u(frag["s"][0]))
        status, obj = C.api_call(self.relay, "GET", "/v1/approval-drops/" + self.did)
        assert status == 200, (status, obj)
        self.ask = approval.unseal(self.key, self.did, "ask", obj["ask"])

    def intent(self):
        return json.loads(core.unb64u(self.ask["intent"], core.DROP_SLOT_MAX).decode("utf-8"))

    def answer(self, obj, key=None):
        sealed = approval.seal(key or self.key, self.did, "answer", obj)
        return C.post_raw(self.relay, "/v1/approval-drops/%s/answer" % self.did, sealed.encode("ascii"),
                          {"Content-Type": "text/plain"})[0]

    def approve(self, passkey):
        return self.answer({"v": 1, "approval": passkey.approve(core.unb64u(self.ask["intent"], core.DROP_SLOT_MAX))})

    def register(self, new, old=None):
        new.label = self.ask.get("label") or new.label              # the page names the passkey as asked
        if old is None:
            return self.answer({"v": 1, "key": new.key()})
        to = {"host": self.ask["host"]}
        if self.ask.get("node"):
            to["node"] = self.ask["node"]
        now = core.now_utc()
        intent = {"type": core.KEY_ADD_TYPE, "v": 1, "to": to, "nonce": b64u(os.urandom(16)),
                  "issued_at": core.iso(now), "expires_at": core.iso(now + core.dt.timedelta(hours=1)),
                  "key": new.key()}
        return self.answer({"v": 1, "key": new.key(), "approval": old.approve(intent)})


class ApprovalFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="yobiru approval test ")
        cls.gw = Gateway(Path(cls._tmp.name)).start()

    @classmethod
    def tearDownClass(cls):
        cls.gw.stop()
        cls._tmp.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"
        self.home.mkdir()
        patches = [mock.patch.dict(os.environ, {approval.RELAY_ENV: self.gw.url}),
                   mock.patch.object(approval, "self_identity", lambda home: (HOST, HOST_NODE)),
                   mock.patch.object(approval, "POLL_SECONDS", 0.2)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.phone = SoftPasskey(label="phone")

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = approval.main(self.home, list(argv), LAUNCHER)
        return code, json.loads(out.getvalue())

    def setup_phone(self):
        code, started = self.run_cli("setup", "--label", "studio")
        self.assertEqual((code, started["mode"], started["state"]), (0, "register", "waiting_for_owner"))
        self.assertTrue(started["link"].startswith(approval.APPROVE_PAGE + "#r="))
        page = Page(started["link"])
        self.assertEqual((page.ask["mode"], page.ask["host"], page.ask["node"]), ("register", HOST, HOST_NODE))
        self.assertEqual(page.register(self.phone), 201)
        code, done = self.run_cli("wait", started["ref"], "--timeout", "5")
        self.assertEqual((code, done["state"]), (0, "key_added"))
        return done

    def peer(self, keys):
        info = {"node": HOST, "node_id": HOST_NODE, "capabilities": [core.OWNER_APPROVAL_CAPABILITY],
                "caller": {"node": SENDER, "id": SENDER_NODE},
                "owner_keys": [{"credential_id": k["credential_id"], "fingerprint": k["fingerprint"],
                                "label": k["label"], "rp_id": k["rp_id"]} for k in keys]}
        return mock.patch.object(approval, "peer_info", lambda home, peer: (HOST, "claude", info))

    def test_setup_then_request_sign_and_verify(self):
        done = self.setup_phone()
        self.assertEqual(done["key"]["label"], "studio")
        keys = core.load_owner_keys(self.home)
        self.assertEqual(len(keys), 1)
        plan = Path(self.temp.name) / "plan.md"
        plan.write_text("the plan", "utf-8")
        with self.peer(keys):
            code, asked = self.run_cli("request", "studio/claude", "Run the tests and open a draft PR",
                                       "--file", str(plan), "--expires", "30m", "--uses", "2")
        self.assertEqual(code, 0, asked)
        page = Page(asked["link"])
        intent = page.intent()
        self.assertEqual(intent["to"], {"host": HOST, "agent": "claude", "node": HOST_NODE})
        self.assertEqual(intent["from"], {"host": SENDER, "node": SENDER_NODE})
        self.assertEqual((intent["uses"], intent["files"][0]["name"]), (2, "plan.md"))
        self.assertEqual(page.ask["credentials"], [keys[0]["credential_id"]])
        code, waiting = self.run_cli("get", asked["ref"])
        self.assertEqual((code, waiting["state"]), (3, "waiting_for_owner"))
        self.assertEqual(page.approve(self.phone), 201)
        self.assertEqual(page.approve(self.phone), 409)            # write-once: no second answer
        code, signed = self.run_cli("wait", asked["link"], "--timeout", "5")
        self.assertEqual((code, signed["state"]), (0, "signed"), signed)
        blob, loaded, _ = core.load_approval_arg(self.home, signed["approval_id"])
        self.assertEqual(loaded["instruction"], "Run the tests and open a draft PR")
        self.assertEqual(core.load_approval_arg(self.home, core.approval_text(blob))[1], loaded)
        code, checked = self.run_cli("verify", signed["approval_id"])
        self.assertEqual((code, checked["verified"]), (0, True), checked)
        self.assertFalse((self.home / "approvals" / "pending" / (asked["ref"] + ".json")).exists())

    def test_the_longest_instruction_goes_through_the_gateway(self):
        """4,000 characters of Chinese and emoji: about 24 KB canonical, well past the old 12 KB drop slot."""
        self.setup_phone()
        instruction = "部署网站并检查日志" * 443 + "部署网" + "\U0001F600" * 10
        self.assertEqual(len(instruction), 4000)
        with self.peer(core.load_owner_keys(self.home)):
            code, asked = self.run_cli("request", "studio/claude", instruction)
        self.assertEqual(code, 0, asked)
        page = Page(asked["link"])
        self.assertEqual(page.intent()["instruction"], instruction)
        self.assertEqual(page.approve(self.phone), 201)
        code, signed = self.run_cli("wait", asked["ref"], "--timeout", "5")
        self.assertEqual((code, signed["state"]), (0, "signed"), signed)
        blob, loaded, raw = core.load_approval_arg(self.home, signed["approval_id"])
        self.assertEqual(loaded["instruction"], instruction)
        self.assertLessEqual(len(raw), core.INTENT_MAX_BYTES)
        self.assertEqual(core.load_approval_arg(self.home, core.approval_text(blob))[1], loaded)
        code, checked = self.run_cli("verify", signed["approval_id"])
        self.assertEqual((code, checked["verified"]), (0, True), checked)
        # Past the intent limit (4,000 emoji are 48 KB), the command says so before anything is sent
        with self.peer(core.load_owner_keys(self.home)):
            code, refused = self.run_cli("request", "studio/claude", "\U0001F600" * 4000)
        self.assertEqual(code, 2)
        self.assertIn("bytes once encoded", refused["error"])

    def test_drop_routes_refuse_what_they_cannot_hold(self):
        import http.client
        did = approval.drop_parts(os.urandom(32))[0]
        post = lambda slot, body: C.post_raw(self.gw.url, "/v1/approval-drops/%s/%s" % (did, slot), body,
                                             {"Content-Type": "text/plain"})[0]
        self.assertEqual(post("answer", b"AAAA"), 404)                          # no ask to answer
        self.assertEqual(post("ask", b"A" * (core.DROP_SLOT_MAX + 1)), 400)     # one slot holds at most this
        self.assertEqual(post("ask", b"not base64url!"), 400)
        self.assertEqual(post("ask", b"A" * core.DROP_SLOT_MAX), 201)           # the largest slot fits
        self.assertEqual(post("ask", b"A" * core.DROP_SLOT_MAX), 200)           # the same write again
        self.assertEqual(post("ask", b"B" * 8), 409)
        port = int(self.gw.url.rsplit(":", 1)[1])
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("OPTIONS", "/v1/approval-drops/%s/answer" % did)
        resp = conn.getresponse()
        resp.read()
        self.assertEqual((resp.status, resp.getheader("Access-Control-Allow-Origin")), (204, "*"))
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/v1/approval-drops/%s" % did)
        resp = conn.getresponse()
        body = json.loads(resp.read())
        self.assertEqual((resp.status, resp.getheader("Access-Control-Allow-Origin")), (200, "*"))
        self.assertEqual((len(body["ask"]), body["answer"]), (core.DROP_SLOT_MAX, None))

    def test_decline_tamper_and_gone(self):
        self.setup_phone()
        with self.peer(core.load_owner_keys(self.home)):
            _, asked = self.run_cli("request", "studio/claude", "Delete the release branch")
        page = Page(asked["link"])
        self.assertEqual(page.answer({"v": 1, "declined": True}), 201)
        code, declined = self.run_cli("wait", asked["ref"], "--timeout", "2")
        self.assertEqual((code, declined["state"]), (0, "declined"))

        with self.peer(core.load_owner_keys(self.home)):
            _, asked = self.run_cli("request", "studio/claude", "Something")
        page = Page(asked["link"])
        self.assertEqual(page.answer({"v": 1, "declined": True}, key=b"\x01" * 32), 201)   # not sealed with the link
        code, broken = self.run_cli("wait", asked["ref"], "--timeout", "2")
        self.assertEqual(code, 2)
        self.assertIn("does not open", broken["error"])

        with self.peer(core.load_owner_keys(self.home)):
            _, asked = self.run_cli("request", "studio/claude", "Something else")
        other = SoftPasskey()
        page = Page(asked["link"])
        intent = page.intent()
        intent["instruction"] = "Something worse"
        page.answer({"v": 1, "approval": self.phone.approve(intent)})
        code, mismatch = self.run_cli("wait", asked["ref"], "--timeout", "2")
        self.assertEqual(code, 2)
        self.assertIn("different content", mismatch["error"])
        del other

        code, gone = self.run_cli("wait", approval.link_for(self.gw.url, os.urandom(32)), "--timeout", "0")
        self.assertEqual(code, 2)
        self.assertIn("gone", gone["error"])

    def test_add_import_and_remove_keys(self):
        self.setup_phone()
        laptop = SoftPasskey(label="laptop")
        code, started = self.run_cli("setup", "--label", "laptop")
        self.assertEqual(started["mode"], "add-key")
        page = Page(started["link"])
        self.assertEqual(page.ask["credentials"], [core.load_owner_keys(self.home)[0]["credential_id"]])
        page.register(laptop, old=SoftPasskey())                    # confirmed by a key this machine does not trust
        code, refused = self.run_cli("wait", started["ref"], "--timeout", "2")
        self.assertEqual(code, 2)
        self.assertIn("does not trust", refused["error"])

        _, started = self.run_cli("setup", "--label", "laptop")
        Page(started["link"]).register(laptop, old=self.phone)
        code, added = self.run_cli("wait", started["ref"], "--timeout", "2")
        self.assertEqual((code, added["state"], added["keys"]), (0, "key_added", 2), added)

        # A passkey from another machine: its share text, confirmed with a trusted key.
        tablet = SoftPasskey(label="tablet")
        share = core.owner_key_text(core.owner_key_record(tablet.key()))
        code, started = self.run_cli("setup", "--key", share)
        self.assertEqual(started["approval"]["type"], core.KEY_ADD_TYPE)
        page = Page(started["link"])
        self.assertEqual(page.intent()["key"]["public_key"], tablet.key()["public_key"])
        page.approve(laptop)
        code, imported = self.run_cli("wait", started["ref"], "--timeout", "2")
        self.assertEqual((code, imported["state"], imported["keys"]), (0, "key_added", 3), imported)

        code, listed = self.run_cli("keys")
        self.assertEqual([k["label"] for k in listed["keys"]], ["studio", "laptop", "tablet"])
        target = listed["keys"][0]["fingerprint"]
        code, started = self.run_cli("remove", target)
        Page(started["link"]).approve(self.phone)
        code, removed = self.run_cli("wait", started["ref"], "--timeout", "2")
        self.assertEqual((code, removed["state"]), (0, "key_removed"), removed)
        self.assertEqual([k["label"] for k in core.load_owner_keys(self.home)], ["laptop", "tablet"])

    def test_import_on_a_new_machine_and_errors(self):
        share = core.owner_key_text(core.owner_key_record(self.phone.key()))
        code, done = self.run_cli("setup", "--key", share, "--label", "phone")
        self.assertEqual((code, done["state"]), (0, "key_added"))
        code, again = self.run_cli("setup", "--key", share)
        self.assertEqual((code, again["state"]), (0, "key_already_trusted"))
        code, bad = self.run_cli("setup", "--key", "yobiru-owner-key:AAAA")
        self.assertEqual(code, 2)
        code, bad = self.run_cli("request", "studio/claude", "x", "--expires", "9d")
        self.assertEqual((code, bad["ok"]), (2, False))
        self.assertIn("7 days", bad["error"])

    def test_approval_for_a_received_request(self):
        self.setup_phone()
        ctx = core.Ctx(self.home)
        env = {"v": 1, "id": "1122334455667788", "from": SENDER, "to": HOST, "ts": core.iso(core.now_utc()),
               "kind": "request", "text": "Restart the build runner", "agent": "claude",
               "request": {"timeout_seconds": 900, "reply_port": 7766}}
        rec = {"envelope": env, "received": {"at": env["ts"], "peer": {"node": SENDER, "stable_id": "nGrok1CNTRL",
                                                                       "dns_name": SENDER},
                                             "node": HOST, "agent": "claude", "agent_status": "known"}}
        (ctx.inbox / (env["id"] + ".json")).write_text(json.dumps(rec), "utf-8")
        code, asked = self.run_cli("for", env["id"])
        self.assertEqual(code, 0, asked)
        self.assertIn(asked["link"], asked["blocked_text"])
        page = Page(asked["link"])
        self.assertEqual(page.intent()["instruction"], "Restart the build runner")
        self.assertEqual(page.intent()["from"], {"host": SENDER, "node": SENDER_NODE})
        page.approve(self.phone)
        code, signed = self.run_cli("wait", asked["ref"], "--timeout", "2")
        blob, _, _ = core.load_approval_arg(self.home, signed["approval_id"])
        resent = dict(env, id="1122334455667799", owner_approval=blob)
        verdict = core.check_incoming_approval(self.home, resent, rec["received"]["peer"], HOST, HOST_NODE,
                                               "claude", "known")
        self.assertTrue(verdict["verified"], verdict)


class SenderTests(unittest.TestCase):
    """`request send --approval` refuses, before anything is sent, an approval that cannot authorize the request."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ctx = core.Ctx(Path(self.temp.name) / "a")
        self.phone = SoftPasskey()
        key = core.owner_key_record(self.phone.key())
        self.info = {"node": HOST, "node_id": HOST_NODE, "capabilities": [core.OWNER_APPROVAL_CAPABILITY],
                     "agents": [{"agent": "claude", "default": True}],
                     "owner_keys": [{"credential_id": key["credential_id"]}]}
        self.me = {"node": SENDER, "stable_id": "nGrok1CNTRL"}

    def approval(self, **over):
        now = core.now_utc()
        intent = {"type": core.APPROVAL_TYPE, "v": 1, "to": {"host": HOST, "agent": "claude", "node": HOST_NODE},
                  "from": {"host": SENDER, "node": SENDER_NODE}, "instruction": "Run the tests", "files": [],
                  "uses": 1, "nonce": b64u(os.urandom(16)), "issued_at": core.iso(now),
                  "expires_at": core.iso(now + core.dt.timedelta(hours=1))}
        intent.update(over)
        return core.approval_text(self.phone.approve(intent))

    def attach(self, value, env=None, info=None):
        env = env or {"agent": "claude"}
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                core.attach_owner_approval(self.ctx, value, env, info or self.info, HOST, self.me)
            except SystemExit:
                return None, err.getvalue()
        return env, err.getvalue()

    def test_attaches_only_a_fitting_approval(self):
        env, _ = self.attach(self.approval())
        self.assertEqual(env["owner_approval"]["v"], 1)
        env, _ = self.attach(self.approval(), env={})               # default agent named by /info
        self.assertIn("owner_approval", env)
        cases = {
            "not this machine": dict(value=self.approval(to={"host": "other.example.ts.net"})),
            "agent claude": dict(value=self.approval(), env={"agent": "codex"}),
            "another machine": dict(value=self.approval(**{"from": {"node": core.stable_peer_id("nOther")}})),
            "approved ones": dict(value=self.approval(), env={"agent": "claude", "fetch": [
                {"name": "x", "url": "http://a/x", "sha256": "ab" * 32, "size": 1}]}),
            "expired": dict(value=self.approval(issued_at="2026-01-01T00:00:00Z", expires_at="2026-01-01T01:00:00Z")),
            "does not trust": dict(value=self.approval(), info=dict(self.info, owner_keys=[])),
            "0.7.30": dict(value=self.approval(), info=dict(self.info, capabilities=[])),
            "--approval:": dict(value="nonsense"),
        }
        for reason, kw in cases.items():
            with self.subTest(reason=reason):
                env, err = self.attach(**kw)
                self.assertIsNone(env)
                self.assertIn(reason, err)
                self.assertIn("no request created", err)


if __name__ == "__main__":
    unittest.main()
