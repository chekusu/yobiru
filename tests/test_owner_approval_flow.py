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
from support.public_harness import C, FriendsGateway, Gateway, cli
from support.softpasskey import SoftPasskey, b64u

SCRIPTS = REPO / "plugins" / "yobiru" / "scripts"
spec = importlib.util.spec_from_file_location("approval_flow_core", REPO / "tailmsg.py")
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)
spec = importlib.util.spec_from_file_location("yobiru_approval_under_test", SCRIPTS / "yobiru_approval.py")
approval = importlib.util.module_from_spec(spec)
spec.loader.exec_module(approval)
LAUNCHER = types.SimpleNamespace(core=lambda: core)
RECEIVER_KEYS = approval.receiver_keys       # the real one; the tests stub the owner's other machines

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
        if "bot" in self.ask:
            to = dict(self.ask["bot"])
        else:
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
        self.receivers = {}            # fqdn -> ([owner key records], problem): the owner's other machines
        patches = [mock.patch.dict(os.environ, {approval.RELAY_ENV: self.gw.url}),
                   mock.patch.object(approval, "self_identity", lambda home: (HOST, HOST_NODE)),
                   mock.patch.object(approval, "POLL_SECONDS", 0.2),
                   mock.patch.object(approval, "tailnet_receivers",
                                     lambda ctx: [(fq, "100.64.0.%d" % (i + 2)) for i, fq in enumerate(self.receivers)]),
                   mock.patch.object(approval, "receiver_keys",
                                     lambda ctx, fqdn, ip, port: self.receivers.get(fqdn, ([], "unreachable")))]
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
        code, again = self.run_cli("setup", "--label", "laptop")
        self.assertEqual((code, again["state"], len(again["keys"])), (0, "already_set_up", 1), again)
        laptop = SoftPasskey(label="laptop")
        code, started = self.run_cli("setup", "--label", "laptop", "--new")
        self.assertEqual(started["mode"], "add-key")
        page = Page(started["link"])
        self.assertEqual(page.ask["credentials"], [core.load_owner_keys(self.home)[0]["credential_id"]])
        page.register(laptop, old=SoftPasskey())                    # confirmed by a key this machine does not trust
        code, refused = self.run_cli("wait", started["ref"], "--timeout", "2")
        self.assertEqual(code, 2)
        self.assertIn("does not trust", refused["error"])

        _, started = self.run_cli("setup", "--label", "laptop", "--new")
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

    def test_adopt_the_passkey_the_other_machines_trust(self):
        """One passkey for every machine: setup finds the key the owner's other receivers trust, the owner signs
        once with that same key, and no passkey is created."""
        phone = core.owner_key_record(self.phone.key())
        stranger = core.owner_key_record(SoftPasskey(label="shared-in").key())
        self.receivers = {"mac.example.ts.net": ([phone], None), "laptop.example.ts.net": ([phone], None),
                          "odd.example.ts.net": ([stranger], None),
                          "old.example.ts.net": ([], "runs Yobiru 0.7.33, which does not share its owner keys yet "
                                                     "(0.7.35 or later does)"),
                          "printer.example.ts.net": ([], "unreachable (ConnectionRefusedError)")}
        code, started = self.run_cli("setup")
        self.assertEqual((code, started["state"], started["mode"]), (0, "waiting_for_owner", "adopt"), started)
        self.assertEqual(started["key"], {"fingerprint": phone["fingerprint"], "label": "phone",
                                          "trusted_by": ["mac.example.ts.net", "laptop.example.ts.net"]})
        self.assertEqual([k["fingerprint"] for k in started["other_keys"]], [stranger["fingerprint"]])
        self.assertEqual(started["machines_asked"], 5)
        self.assertEqual(len(started["notes"]), 1)                  # the receiver to upgrade, not the printer
        self.assertIn("old.example.ts.net", started["notes"][0])
        page = Page(started["link"])
        intent = page.intent()
        self.assertEqual(page.ask["mode"], "sign")
        self.assertEqual(page.ask["credentials"], [phone["credential_id"]], "the page offers only that passkey")
        self.assertEqual((intent["type"], intent["to"]), (core.KEY_ADD_TYPE, {"host": HOST, "node": HOST_NODE}))
        self.assertEqual(intent["key"], {f: phone[f] for f in core.OWNER_KEY_FIELDS})
        self.assertEqual(page.approve(self.phone), 201)
        code, done = self.run_cli("wait", started["ref"], "--timeout", "5")
        self.assertEqual((code, done["state"], done["keys"]), (0, "key_added", 1), done)
        self.assertEqual((done["key"]["added_via"], done["key"]["fingerprint"]), ("adopted", phone["fingerprint"]))
        code, again = self.run_cli("setup")
        self.assertEqual((code, again["state"]), (0, "already_set_up"))
        # and the adopted key works like any other
        with self.peer(core.load_owner_keys(self.home)):
            code, asked = self.run_cli("request", "studio/claude", "Deploy the site")
        Page(asked["link"]).approve(self.phone)
        code, signed = self.run_cli("wait", asked["ref"], "--timeout", "5")
        code, checked = self.run_cli("verify", signed["approval_id"])
        self.assertEqual((code, checked["verified"]), (0, True), checked)

    def test_a_key_the_owner_does_not_hold_is_never_adopted(self):
        stranger = SoftPasskey(label="sandbank-tyo")
        self.receivers = {"odd.example.ts.net": ([core.owner_key_record(stranger.key())], None)}
        code, started = self.run_cli("setup")
        self.assertEqual(started["mode"], "adopt")
        page = Page(started["link"])
        # The owner's device has no such passkey; whatever another passkey signs, nothing is pinned.
        self.assertEqual(page.approve(self.phone), 201)
        code, refused = self.run_cli("wait", started["ref"], "--timeout", "5")
        self.assertEqual(code, 2)
        self.assertIn("does not trust", refused["error"])
        self.assertEqual(core.load_owner_keys(self.home), [])
        # A key addressed elsewhere, signed by the key itself, is refused too.
        code, started = self.run_cli("setup")
        page = Page(started["link"])
        now = core.now_utc()
        moved = dict(page.intent(), to={"host": "other.example.ts.net"}, nonce=b64u(os.urandom(16)),
                     issued_at=core.iso(now))
        signed = stranger.approve(moved)
        self.assertEqual(page.answer({"v": 1, "approval": signed}), 201)
        code, refused = self.run_cli("wait", started["ref"], "--timeout", "5")
        self.assertEqual(code, 2)
        self.assertIn("different content", refused["error"])
        self.assertEqual(core.load_owner_keys(self.home), [])

    def test_no_shared_passkey_then_a_new_one_and_the_flags(self):
        self.receivers = {"old.example.ts.net": ([], "runs Yobiru 0.7.33, which does not share its owner keys yet "
                                                     "(0.7.35 or later does)")}
        code, started = self.run_cli("setup", "--label", "studio")
        self.assertEqual((code, started["mode"]), (0, "register"), started)
        self.assertEqual(started["searched"]["machines_asked"], 1)
        self.assertIn("old.example.ts.net", started["searched"]["notes"][0])
        # --new never asks the tailnet
        self.receivers = {"mac.example.ts.net": ([core.owner_key_record(self.phone.key())], None)}
        with mock.patch.object(approval, "tailnet_receivers", side_effect=AssertionError("asked the tailnet")):
            code, fresh = self.run_cli("setup", "--new")
        self.assertEqual((code, fresh["mode"]), (0, "register"), fresh)
        self.assertNotIn("searched", fresh)
        # --from asks only the machines it names, without listing the tailnet
        resolved = []

        def resolve(ctx, host):
            resolved.append(host)
            return host + ".example.ts.net", "100.64.0.9"
        with mock.patch.object(approval, "tailnet_receivers", side_effect=AssertionError("listed the tailnet")), \
                mock.patch.object(core.Ctx, "resolve", resolve):
            code, started = self.run_cli("setup", "--from", "mac")
        self.assertEqual((code, started["mode"], started["key"]["trusted_by"]), (0, "adopt", ["mac.example.ts.net"]))
        self.assertEqual(resolved, ["mac"])
        with mock.patch.object(core.Ctx, "resolve", resolve):
            code, none = self.run_cli("setup", "--from", "printer")
        self.assertEqual((code, none["mode"]), (0, "register"))
        self.assertEqual(none["searched"]["notes"], ["printer.example.ts.net: unreachable"])

    def test_receiver_keys_reads_a_real_info_answer(self):
        """receiver_keys against an HTTP server answering /info as a 0.7.35 receiver, and as an older one."""
        import http.server
        import threading
        phone = core.owner_key_record(self.phone.key())
        answers = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps(answers["info"]).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        approval._tm = core
        ctx = core.Ctx(self.home)
        shared = dict({f: phone[f] for f in core.OWNER_KEY_FIELDS}, fingerprint=phone["fingerprint"])
        answers["info"] = {"node": "mac.example.ts.net", "version": "0.7.35",
                           "owner_keys": [shared, {"credential_id": "x", "public_key": "not a key"}]}
        recs, problem = RECEIVER_KEYS(ctx, "mac.example.ts.net", "127.0.0.1", port)
        self.assertEqual(([r["id"] for r in recs], problem), ([phone["id"]], None))
        answers["info"]["node"] = "someone-else.example.ts.net"     # not answering as itself
        self.assertEqual(RECEIVER_KEYS(ctx, "mac.example.ts.net", "127.0.0.1", port)[0], [])
        answers["info"] = {"node": "mac.example.ts.net", "version": "0.7.33",
                           "owner_keys": [{"credential_id": phone["credential_id"], "fingerprint": phone["fingerprint"],
                                           "label": "phone", "rp_id": "yobiru.com"}]}
        recs, problem = RECEIVER_KEYS(ctx, "mac.example.ts.net", "127.0.0.1", port)
        self.assertEqual(recs, [])
        self.assertIn("0.7.33", problem)

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


class FriendsApprovalTests(unittest.TestCase):
    """A Friends bot's own agent asks its owner to approve one message the bot received: over a real gateway, with
    the bots, the native binding and the end-to-end keys made by the client, and no Tailscale at all."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="yobiru friends approval ")
        cls.root = Path(cls._tmp.name)
        cls.gw = FriendsGateway(cls.root).start()
        cls.homes = {r: cls.root / r for r in ("owner", "agent", "visitor")}
        y = cls.y_cls
        y("owner", "owner", "login", cls.gw.url, "--token-file", str(cls.gw.home / "owner.token"))
        cls.claude = y("owner", "bot", "create", "--handle", "claude", "--name", "Claude at home")["bot_id"]
        cls.grok = y("owner", "bot", "create", "--handle", "grok", "--name", "Grok")["bot_id"]
        for handle in ("claude", "grok"):
            y("owner", "bot", "publish", handle)
        y("owner", "bot", "edit", "claude", "--offered", "faq,chat,files", "--visitor-capabilities", "faq,chat,files")
        token = cls.root / "native.token"
        y("owner", "worker", "bind", "claude", "--adapter", "native", "--token-file", str(token))
        y("agent", "worker", "login", cls.gw.url, "--token-file", str(token))
        y("owner", "add", cls.gw.url + "/@claude", "--alias", "claude-home", "--as", "@grok")

    @classmethod
    def tearDownClass(cls):
        cls.gw.stop()
        cls._tmp.cleanup()

    @classmethod
    def y_cls(cls, role, *args):
        code, obj, err = cli(cls.homes[role], *args)
        assert code == 0, (role, args, obj, err)
        return obj

    def setUp(self):
        patches = [mock.patch.dict(os.environ, {approval.RELAY_ENV: self.gw.url}),
                   mock.patch.object(approval, "POLL_SECONDS", 0.2),
                   mock.patch.object(approval, "self_identity", self.no_tailscale)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.phone = SoftPasskey(label="phone")
        self.home = self.homes["agent"]
        if not core.load_owner_keys(self.home):
            self.setup_phone()

    @staticmethod
    def no_tailscale(home):
        raise approval.Fail("this machine's Tailscale identity is unavailable (not installed)")

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = approval.main(self.home, list(argv), LAUNCHER)
        return code, json.loads(out.getvalue())

    def setup_phone(self):
        type(self).phone_key = self.phone
        code, started = self.run_cli("setup")
        self.assertEqual((code, started["mode"]), (0, "register"), started)
        page = Page(started["link"])
        self.assertEqual(page.ask["bot"], {"gateway": self.gw.url, "bot": self.claude, "handle": "claude"},
                         "without Tailscale, the only bot this home answers for")
        self.assertNotIn("host", page.ask)
        self.assertEqual(page.register(self.phone), 201)
        code, done = self.run_cli("wait", started["ref"], "--timeout", "5")
        self.assertEqual((code, done["state"]), (0, "key_added"), done)

    def message(self, text, role="owner", alias="claude-home", attach=None, **kw):
        args = ["chat", alias, text] + (["--as", "@grok"] if role == "owner" else [])
        for path in attach or []:
            args += ["--attach", str(path)]
        sent = self.y_cls(role, *args)
        mid = sent["message_id"]
        if role == "owner":
            self.y_cls("owner", "review", "accept", "claude", mid)
        else:
            self.y_cls("owner", "review", "approve-inbound", "claude", mid)
        return mid, self.y_cls("agent", "job", "claim", self.claude, mid)

    def test_the_owners_other_bot_asks_and_the_owner_signs_that_message(self):
        mid, job = self.message("部署网站，然后把日志发给我。Deploy the site from main.")
        self.assertTrue(job["from"]["same_owner"], "the gateway says the sender is another bot of this owner")
        self.assertTrue(job["untrusted_content"]["e2e"]["opened"])
        code, asked = self.run_cli("for", mid)
        self.assertEqual(code, 0, asked)
        self.assertIn(asked["link"], asked["reply_text"], "the link goes back to the owner's own bot")
        page = Page(asked["link"])
        intent = page.intent()
        self.assertEqual(intent["to"], {"gateway": self.gw.url, "bot": self.claude, "handle": "claude"})
        self.assertEqual(intent["from"], {"principal": self.grok, "handle": "grok"})
        self.assertEqual((intent["message"], intent["uses"], intent["files"]), (mid, 1, []))
        self.assertEqual(intent["instruction"], "部署网站，然后把日志发给我。Deploy the site from main.")
        self.assertIn(self.phone_key.key()["credential_id"], page.ask["credentials"])
        code, waiting = self.run_cli("get", mid)
        self.assertEqual((code, waiting["state"]), (3, "waiting_for_owner"))
        self.assertEqual(page.approve(self.phone_key), 201)
        code, signed = self.run_cli("wait", asked["ref"], "--timeout", "5")
        self.assertEqual((code, signed["state"], signed["verified"]), (0, "signed", True), signed)
        verdict = signed["owner_approval"]
        self.assertEqual((verdict["message"], verdict["instruction"]), (mid, intent["instruction"]))
        self.assertIn("this computer", verdict["checked_by"])
        # the agent answers the message; later (a new session) it asks by message id
        self.y_cls("agent", "job", "reply", self.claude, mid, "--text", "Waiting for your signature")
        code, later = self.run_cli("get", mid)
        self.assertEqual((code, later["state"], later["verified"]), (0, "signed", True), later)
        code, listed = self.run_cli("list")
        self.assertIn({"message": mid, "verified": True, "reason": None, "expires_at": intent["expires_at"]},
                      listed["friends_messages"])
        code, checked = self.run_cli("verify", signed["approval_id"])
        self.assertEqual((code, checked["verified"]), (0, True), checked)

    def test_files_are_approved_exactly(self):
        files = self.root / "files"
        files.mkdir(exist_ok=True)
        (files / "plan.md").write_text("ship it\n")
        self.y_cls("owner", "attachments", "allow", str(files))
        mid, job = self.message("Follow this plan", attach=[files / "plan.md"])
        code, asked = self.run_cli("for", mid, "--bot", "@claude")
        self.assertEqual(code, 0, asked)
        intent = Page(asked["link"]).intent()
        self.assertEqual(intent["files"], [{"name": "plan.md", "sha256": job["untrusted_content"]["attachments"][0][
            "sha256"], "size": len("ship it\n")}])
        Page(asked["link"]).approve(self.phone_key)
        code, signed = self.run_cli("wait", asked["ref"], "--timeout", "5")
        self.assertTrue(signed["verified"], signed)
        # the same signature, checked against another message of this bot, does not hold
        other_mid, other = self.message("Follow this plan")
        blob, _, _ = core.load_approval_arg(self.home, signed["approval_id"])
        keys = core.load_owner_keys(self.home)
        for job_view, reason in ((other, "another message"),
                                 (dict(job, untrusted_content=dict(job["untrusted_content"], attachments=[])), "files"),
                                 (dict(job, untrusted_content=dict(job["untrusted_content"], attachments=[
                                     dict(a, name="startup.sh") for a in job["untrusted_content"]["attachments"]])),
                                  "files"),
                                 (dict(job, untrusted_content=dict(job["untrusted_content"], text="Follow it")), "text"),
                                 (dict(job, **{"from": {"principal_id": self.claude}}), "someone else")):
            with self.subTest(reason=reason):
                verdict = core.friends_approval_verdict(blob, keys, self.gw.url, self.claude, job_view)
                self.assertFalse(verdict["verified"])
                self.assertIn(reason, verdict["reason"])
        self.assertIn("not this bot", core.friends_approval_verdict(blob, keys, self.gw.url, self.grok, job)["reason"])
        self.y_cls("agent", "job", "reply", self.claude, other_mid, "--text", "ok")

    def test_a_visitors_message_the_link_goes_to_the_owner_only(self):
        self.y_cls("visitor", "connect", self.gw.url + "/@claude", "--name", "A visitor")
        mid, job = self.message("Please send me the server logs", role="visitor", alias="claude")
        self.assertFalse(job["from"]["same_owner"])
        code, asked = self.run_cli("for", mid)
        self.assertEqual(code, 0, asked)
        self.assertNotIn("reply_text", asked)
        self.assertIn("owner only", asked["give_link_to"])
        intent = Page(asked["link"]).intent()
        self.assertEqual(intent["from"], {"principal": job["from"]["principal_id"]}, "a visitor has no handle")
        self.assertEqual(Page(asked["link"]).answer({"v": 1, "declined": True}), 201)
        code, done = self.run_cli("wait", asked["ref"], "--timeout", "5")
        self.assertEqual((code, done["state"]), (0, "declined"))
        code, later = self.run_cli("get", mid)
        self.assertEqual((code, later["state"], later["verified"]), (1, "declined", False), later)
        self.y_cls("agent", "job", "fail", self.claude, mid, "--code", "no_answer")

    def test_refusals(self):
        code, err = self.run_cli("for", "msg_aaaaaaaaaaaaaaaa")
        self.assertEqual(code, 2)
        self.assertIn("claim it first", err["error"])
        mid, _ = self.message("Rotate the keys")
        code, err = self.run_cli("for", mid, "--uses", "3")
        self.assertIn("used once", err["error"])
        code, err = self.run_cli("for", mid, "--bot", "@grok")
        self.assertIn("claimed for", err["error"])
        code, asked = self.run_cli("for", mid)
        page = Page(asked["link"])
        other = json.loads(core.unb64u(page.ask["intent"], core.DROP_SLOT_MAX))
        other["instruction"] = "Rotate the keys and delete the backups"
        self.assertEqual(page.answer({"v": 1, "approval": self.phone_key.approve(other)}), 201)
        code, err = self.run_cli("wait", asked["ref"], "--timeout", "5")
        self.assertEqual(code, 2)
        self.assertIn("different content", err["error"])
        code, err = self.run_cli("get", "msg_bbbbbbbbbbbbbbbb")
        self.assertIn("no approval was asked", err["error"])
        self.y_cls("agent", "job", "fail", self.claude, mid, "--code", "no_answer")

    def test_a_removed_key_ends_an_approval_and_the_bot_signs_key_changes(self):
        mid, _ = self.message("Restart the gateway")
        code, asked = self.run_cli("for", mid)
        Page(asked["link"]).approve(self.phone_key)
        self.assertEqual(self.run_cli("wait", asked["ref"], "--timeout", "5")[1]["verified"], True)
        laptop = SoftPasskey(label="laptop")
        code, added = self.run_cli("setup", "--key", core.owner_key_text(core.owner_key_record(laptop.key())))
        page = Page(added["link"])
        self.assertEqual(page.intent()["to"], {"gateway": self.gw.url, "bot": self.claude, "handle": "claude"})
        page.approve(self.phone_key)
        self.assertEqual(self.run_cli("wait", added["ref"], "--timeout", "5")[1]["state"], "key_added")
        old = core.owner_key_record(self.phone_key.key())
        code, removing = self.run_cli("remove", old["fingerprint"])
        Page(removing["link"]).approve(laptop)
        self.assertEqual(self.run_cli("wait", removing["ref"], "--timeout", "5")[1]["state"], "key_removed")
        code, later = self.run_cli("get", mid)
        self.assertEqual((code, later["verified"]), (1, False))
        self.assertIn("does not trust", later["owner_approval"]["reason"])
        # put the phone back for the other tests, signed by the laptop
        code, back = self.run_cli("setup", "--key", core.owner_key_text(old))
        Page(back["link"]).approve(laptop)
        self.assertEqual(self.run_cli("wait", back["ref"], "--timeout", "5")[1]["state"], "key_added")
        self.y_cls("agent", "job", "reply", self.claude, mid, "--text", "ok")

    def test_a_bot_approval_never_authorizes_a_tailnet_request(self):
        now = core.now_utc()
        intent = {"type": core.APPROVAL_TYPE, "v": 1, "to": {"gateway": self.gw.url, "bot": self.claude},
                  "from": {"principal": self.grok}, "message": "msg_aaaaaaaaaaaaaaaa", "instruction": "Run it",
                  "files": [], "uses": 1, "nonce": b64u(os.urandom(16)), "issued_at": core.iso(now),
                  "expires_at": core.iso(now + core.dt.timedelta(hours=1))}
        env = {"owner_approval": self.phone_key.approve(intent), "request": {}}
        verdict = core.check_incoming_approval(self.home, env, {"node": SENDER}, HOST, HOST_NODE, None, "default")
        self.assertFalse(verdict["verified"])
        self.assertIn("does not answer for", verdict["reason"])


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
