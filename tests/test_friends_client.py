"""Friends through the client CLI: share-link onboarding, self-enrolled source bots and follows, the supervised
native two-turn flow driven by a connector and a synthetic host process, owner-approved resource exports, wake
transport classification, and client retry/identity safety (review findings C1-C3).

Real gateway and resource-service subprocesses on loopback, a separate synthetic home per role. The native host is
tests/support/synthetic_host.py run by the connector's exec route: a protocol fixture, never an agent.
"""
import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler
from support.numeric_http import HTTPServer
from pathlib import Path

from support.public_harness import C, PUBLIC, FriendsGateway, Service, plain_env

import bot_client as P  # noqa: E402

SUPPORT = Path(__file__).resolve().parent / "support"
TOKEN_PATTERN = re.compile(r"yb[oawvtcsn]_[A-Za-z0-9_-]{43}")
LIMITS = {"min_deadline_seconds": 1, "read_timeout_seconds": 5, "wake_retry_seconds": 1,
          "messages_per_minute_per_actor": 1000, "messages_per_hour_per_actor": 5000, "pending_per_contact": 100,
          "new_conversations_per_hour_per_bot": 1000, "new_contacts_per_hour_per_bot": 1000,
          "bot_enroll_per_hour_per_ip": 1000, "bot_enroll_per_hour": 1000, "enroll_per_hour_per_ip": 1000}


def cli(home, *args):
    """bot_client in this process: -> (exit code, parsed JSON stdout or text, stderr)."""
    out, err, code = io.StringIO(), io.StringIO(), 0
    with redirect_stdout(out), redirect_stderr(err):
        try:
            P.main(["--home", str(home)] + list(args))
        except SystemExit as e:
            code = e.code or 0
    text = out.getvalue()
    try:
        return code, json.loads(text), err.getvalue()
    except ValueError:
        return code, text, err.getvalue()


class FriendsClientCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="yobiru friends client ")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.gw = FriendsGateway(self.root, limits=LIMITS).start()
        self.addCleanup(self.gw.stop)
        self.homes = {r: self.root / r for r in ("owner", "caller", "host", "connector", "other")}
        self.outputs = []

    def y(self, role, *args, expect=0):
        code, obj, err = cli(self.homes[role], *args)
        self.outputs.append(json.dumps(obj) + err)
        self.assertEqual(expect, code, "%s %s\n%s\n%s" % (role, args, obj, err))
        return obj

    def owner_bot(self, handle="alice"):
        self.y("owner", "owner", "login", self.gw.url, "--token-file", str(self.gw.home / "owner.token"))
        bot_id = self.y("owner", "bot", "create", "--handle", handle, "--name", "Synthetic %s" % handle)["bot_id"]
        self.y("owner", "bot", "publish", handle)
        return bot_id

    def native(self, handle, bot_id):
        token_file = self.root / "native.token"
        bound = self.y("owner", "worker", "bind", handle, "--adapter", "native", "--label", "synthetic host",
                       "--token-file", str(token_file))
        self.y("host", "worker", "login", self.gw.url, "--token-file", str(token_file))
        return bound["worker_id"]

    def connector(self, handle, bot_id, worker_id):
        token_file = self.root / "connector.token"
        self.y("owner", "connector", "bind", handle, worker_id, "--token-file", str(token_file))
        self.y("connector", "connector", "login", self.gw.url, "--token-file", str(token_file))
        self.y("connector", "connector", "config", bot_id, "--exec", sys.executable, str(SUPPORT / "synthetic_host.py"),
               str(PUBLIC / "bot_client.py"), str(self.homes["host"]), bot_id, "{job_id}", "{phase}", "{wake_id}",
               "{check_id}")

    def wakes(self):
        log = self.homes["host"] / "wakes.log"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def assert_no_credentials_printed(self):
        for text in self.outputs:
            self.assertIsNone(TOKEN_PATTERN.search(text), text[:300])


class OnboardingTests(FriendsClientCase):
    def test_an_agent_with_only_the_share_link_can_connect_and_chat(self):
        self.owner_bot("alice")
        share = self.y("owner", "bot", "share", "alice")
        self.assertEqual(self.gw.url + "/@alice", share["share_link"])
        card = self.y("caller", "lookup", share["share_link"])
        self.assertEqual(("@alice", self.gw.url), (card["handle"], card["issuer"]))
        connected = self.y("caller", "connect", share["share_link"], "--name", "Synthetic visitor agent")
        self.assertEqual(("alice", "visitor", True), (connected["alias"], connected["as_kind"],
                                                      connected["enrolled_visitor"]))
        self.assertEqual(["faq", "chat"], connected["relationship"]["capabilities"])
        sent = self.y("caller", "chat", "alice", "Hello from a synthetic visitor")
        self.assertEqual(("queued", False, None), (sent["state"], sent["reply_required"], sent["deadline"]))
        again = self.y("caller", "connect", share["share_link"])
        self.assertFalse(again["enrolled_visitor"], "the saved identity is reused, never a second one")
        ids = self.y("caller", "identities")["identities"]
        self.assertEqual(1, len(ids))
        self.assertIsNotNone(ids[0]["visitor"])
        self.assert_no_credentials_printed()
        creds = (self.homes["caller"] / "public" / "credentials.json")
        self.assertEqual(0o600, creds.stat().st_mode & 0o777)

    def test_source_bots_follow_and_become_friends_through_the_cli(self):
        alice = self.owner_bot("alice")
        bob = self.y("caller", "enroll-bot", self.gw.url, "--handle", "bob", "--name", "Synthetic Bob")
        self.assertEqual(("@bob", "draft"), (bob["handle"], bob["state"]))
        followed = self.y("caller", "connect", self.gw.url + "/@alice", "--follow")
        self.assertEqual(("bot", "follower"), (followed["as_kind"], followed["follow"]["relationship"]))
        followers = self.y("owner", "owner", "followers", "alice")["followers"]
        self.assertEqual([(bob["bot_id"], "follower")], [(f["bot_id"], f["relationship"]) for f in followers])
        back = self.y("owner", "follow", bob["bot_id"], "--as", "@alice", "--gateway", self.gw.url)
        self.assertEqual("friend", back["relationship"])
        self.assertEqual("friend", self.y("caller", "relationship", "alice")["relationship"])
        self.assertEqual([alice], [f["bot_id"] for f in self.y("caller", "following", "--gateway", self.gw.url)["following"]])
        self.y("caller", "unfollow", "alice")
        self.assertEqual("visitor", self.y("caller", "relationship", "alice")["relationship"])
        code, _, err = cli(self.homes["other"], "follow", self.gw.url + "/@alice")
        self.assertEqual(2, code)
        self.assertIn("no caller identity", err, "an agent without any identity cannot follow")
        self.assert_no_credentials_printed()


class NativeFlowTests(FriendsClientCase):
    def test_message_requests_and_the_inbox_from_the_command_line(self):
        alice = self.owner_bot("alice")
        self.native("alice", alice)
        self.y("caller", "connect", self.gw.url + "/@alice", "--name", "Visitor")
        first = self.y("caller", "chat", "alice", "REQ-CLI hello")["message_id"]
        listed = self.y("owner", "review", "list", "alice")
        self.assertEqual(("REQ-CLI hello", first), (listed["inbound"][0]["message"]["text"],
                                                    listed["inbound"][0]["job_id"]))
        self.assertEqual("ignored", self.y("owner", "review", "ignore", "alice", first)["inbound"])
        self.assertEqual([], self.y("owner", "review", "list", "alice")["inbound"])
        second = self.y("caller", "chat", "alice", "hello again")["message_id"]
        self.assertTrue(self.y("owner", "review", "accept", "alice", second)["inbound_preapproved"])
        third = self.y("caller", "chat", "alice", "and a third")["message_id"]
        claimed = self.y("host", "job", "claim", alice, second)
        self.assertIn(third, claimed["more_from_this_sender"])
        self.y("host", "job", "claim", alice, third)
        self.assertEqual("read", self.y("host", "job", "read", alice, second)["code"])
        self.assertEqual("completed", self.y("host", "job", "reply", alice, third, "--text", "hi!")["state"])
        # block straight from a request
        self.y("other", "connect", self.gw.url + "/@alice", "--name", "Pest")
        pest = self.y("other", "chat", "alice", "buy my stuff")["message_id"]
        self.assertEqual("revoked", self.y("owner", "review", "block", "alice", pest)["status"])
        refused = self.y("other", "chat", "alice", "again?", expect=2)
        self.assertIn(refused.get("error"), ("blocked", "contact_revoked", "not_found", "forbidden"), refused)

    def test_two_supervised_turns_through_connector_and_synthetic_host(self):
        alice = self.owner_bot("alice")
        wid = self.native("alice", alice)
        self.connector("alice", alice, wid)
        bob = self.y("caller", "enroll-bot", self.gw.url, "--handle", "bob", "--name", "Synthetic Bob")["bot_id"]
        self.y("caller", "connect", self.gw.url + "/@alice", "--follow")
        # bob follows alice one way, so his chat is reviewed (friends would talk to her agent directly)
        first = self.y("caller", "chat", "alice", "turn one: name a robot")["message_id"]
        woke = self.y("connector", "connector", "run", alice, "--once")
        self.assertEqual([(first, "review", "accepted")], [(w["ref"], w["phase"], w["outcome"]) for w in woke["woken"]])
        listed = self.y("owner", "review", "list", "alice")
        self.assertEqual([first], [x["job_id"] for x in listed["inbound"]])
        self.assertIn("name a robot", json.dumps(listed), "the owner reads a message request in full")
        self.y("owner", "review", "approve-inbound", "alice", first)
        self.y("connector", "connector", "run", alice, "--once")
        reply = self.y("owner", "review", "list", "alice")["replies"][0]
        self.assertIn("SYNTHETIC-HOST reply to 'turn one: name a robot'; earlier bot turns: 0", reply["draft"]["text"])
        self.assertEqual("review", self.y("caller", "status", first)["state"])
        self.y("owner", "review", "approve-reply", "alice", first, "--draft-hash", reply["draft_hash"])
        self.assertIn("earlier bot turns: 0", self.y("caller", "status", first)["result"]["text"])
        # second turn: the owner accepts this sender (trust), so bob is in the inbox: the job is ready at once,
        # carries turn one, and its reply goes back without review
        self.y("owner", "owner", "contact", "alice", bob, "trust")
        second = self.y("caller", "chat", "alice", "turn two: continue")["message_id"]
        self.y("connector", "connector", "run", alice, "--once")
        self.assertEqual([], self.y("owner", "review", "list", "alice")["replies"])
        self.assertIn("earlier bot turns: 1; last: SYNTHETIC-HOST reply to 'turn one: name a robot'",
                      self.y("caller", "status", second)["result"]["text"])
        history = self.y("caller", "history", "alice")
        self.assertEqual(["completed", "completed"], [m["state"] for m in history["messages"]])
        self.assertIn("earlier bot turns: 1", history["messages"][1]["result"]["text"])
        idle = self.y("connector", "connector", "run", alice, "--once")
        self.assertEqual([], idle["woken"], "nothing is woken twice")
        self.assertEqual([(first, "review"), (first, "ready"), (second, "ready")],
                         [(w["job"], w["phase"]) for w in self.wakes()])
        evidence = self.y("owner", "binding", "show", "alice")["bindings"][0]["evidence"]
        self.assertTrue(evidence["reply_tested_at"] and evidence["multiturn_tested_at"])
        self.assertIsNone(evidence["wake_tested_at"])
        chk = self.y("owner", "binding", "check", "alice", wid)["check_id"]
        self.y("connector", "connector", "run", alice, "--once")
        self.assertEqual([chk], [w["check"] for w in self.wakes() if w["check"]])
        evidence = self.y("owner", "binding", "show", "alice")["bindings"][0]["evidence"]
        self.assertTrue(evidence["wake_tested_at"], "a recorded wake followed by the host's acknowledgement")
        self.assert_no_credentials_printed()

    def test_the_connector_recovers_its_ledger_without_repeating_a_wake(self):
        alice = self.owner_bot("alice")
        wid = self.native("alice", alice)
        self.connector("alice", alice, wid)
        actor = self.y("caller", "connect", self.gw.url + "/@alice", "--name", "Visitor")["as"]
        self.y("owner", "owner", "contact", "alice", actor, "trust")
        crashed = self.y("caller", "chat", "alice", "crashed during the wake")["message_id"]
        token = json.loads((self.homes["connector"] / "public" / "credentials.json").read_text())[
            "gateways"][self.gw.url]["connectors"][alice]["token"]
        self.assertEqual(200, self.gw.call("POST", "/v1/connector/messages/%s/wake" % crashed,
                                           {"step": "intent", "wake_id": "lost-wake-01", "phase": "ready"}, token)[0])
        ledger = P.State(self.homes["connector"])
        ledger.save("connector-ledger.json", {"v": 1, "wakes": {"%s:%s" % (alice, crashed): {
            "kind": "job", "ref": crashed, "phase": "ready", "wake_id": "lost-wake-01", "step": "intended"}}})
        report = self.y("connector", "connector", "run", alice, "--once")
        self.assertEqual([{"ref": crashed, "outcome": "uncertain"}], report["recovered"])
        self.assertEqual([], report["woken"])
        self.assertEqual([], self.wakes(), "a wake that may have happened is reported, never repeated")
        self.assertEqual("uncertain", self.gw.sql("SELECT wake_state FROM messages WHERE id=?", (crashed,))[0]["wake_state"])
        # a planned wake whose intent never reached the gateway was never performed: recorded and refused, then
        # retried once after its backoff
        planned = self.y("caller", "chat", "alice", "planned before a crash", "--new-conversation")["message_id"]
        ledger.save("connector-ledger.json", {"v": 1, "wakes": {"%s:%s" % (alice, planned): {
            "kind": "job", "ref": planned, "phase": "ready", "wake_id": "never-sent-01", "step": "planned"}}})
        report = self.y("connector", "connector", "run", alice, "--once")
        self.assertEqual([], [w for w in self.wakes() if w["job"] == planned])
        self.assertEqual([{"ref": planned, "outcome": "refused"}], report["recovered"])
        time.sleep(1.2)
        self.y("connector", "connector", "run", alice, "--once")
        self.assertEqual(1, len([w for w in self.wakes() if w["job"] == planned]))
        self.assertEqual({}, json.loads((self.homes["connector"] / "public" / "connector-ledger.json").read_text())["wakes"])


class TransportTests(unittest.TestCase):
    def serve(self, status=None, drop=False):
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if drop:
                    self.connection.close()
                    return
                self.send_response(status)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")
        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return "http://127.0.0.1:%d/hook?x=1" % srv.server_port

    def test_only_a_presend_failure_is_refused(self):
        self.assertEqual("accepted", C.post_wake(self.serve(200), {}, {"a": 1}, 5)[0])
        for status in (500, 503, 429, 400, 404, 302, 409):
            self.assertEqual("uncertain", C.post_wake(self.serve(status), {}, {"a": 1}, 5)[0], status)
        self.assertEqual("uncertain", C.post_wake(self.serve(drop=True), {}, {"a": 1}, 5)[0])
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            closed = "http://127.0.0.1:%d/hook" % s.getsockname()[1]
        self.assertEqual("refused", C.post_wake(closed, {}, {"a": 1}, 5)[0])
        for bad in ("http://example.com/hook", "https://user:pw@example.com/", "https://example.com/#x", "ftp://x/"):
            with self.assertRaises(ValueError):
                C.wake_url(bad)

    def test_an_exec_route_is_uncertain_once_the_command_ran(self):
        body = {"type": "yobiru.job", "bot_id": "bot_aaaaaaaaaaaaaaaa", "handle": "@x", "wake_id": "w" * 12,
                "gateway": "http://127.0.0.1:1", "job_id": "msg_aaaaaaaaaaaaaaaa", "phase": "ready"}
        for code, expected in ((0, "accepted"), (1, "uncertain"), (75, "uncertain"), (2, "uncertain")):
            route = {"type": "exec", "argv": [sys.executable, "-c", "import sys; sys.exit(%d)" % code]}
            self.assertEqual(expected, P.perform_wake(route, body)[0], code)
        self.assertEqual("refused", P.perform_wake({"type": "exec", "argv": ["/nonexistent/yobiru-wake"]}, body)[0])
        with tempfile.TemporaryDirectory(prefix="yobiru exec ") as tmp:
            target = os.path.join(tmp, "argv.json")
            route = {"type": "exec", "argv": [sys.executable, "-c", "import sys, json; open(sys.argv[1], 'w').write("
                                              "json.dumps(sys.argv[2:]))", target, "{job_id}", "{handle}", "{phase}"]}
            self.assertEqual("accepted", P.perform_wake(route, body)[0])
            with open(target) as f:
                self.assertEqual(["msg_aaaaaaaaaaaaaaaa", "@x", "ready"], json.load(f), "only opaque values")


class ResourceTests(FriendsClientCase):
    def test_exports_are_released_only_to_the_caller_they_were_granted_to(self):
        alice = self.owner_bot("alice")
        self.native("alice", alice)
        rdir = self.root / "resources"
        run = lambda *args: subprocess.run([sys.executable, str(PUBLIC / "bot_resources.py")] + list(args),  # noqa
                                           capture_output=True, text=True, env=plain_env(self.root), timeout=60)
        self.assertEqual(0, run("init", "--dir", str(rdir), "--gateway", self.gw.url, "--bot", alice).returncode)
        menu = self.root / "menu.txt"
        menu.write_text("SYNTHETIC-MENU: soup, bread\n")
        self.assertEqual(0, run("add", "--dir", str(rdir), "menu", "--file", str(menu)).returncode)
        secret = self.root / "secret.txt"
        secret.write_text("SYNTHETIC-SECRET\n")
        self.assertEqual(0, run("add", "--dir", str(rdir), "secret", "--file", str(secret)).returncode)
        link = self.root / "link.txt"
        os.symlink(str(secret), str(link))
        for args in (("../escape", "--file", str(menu)), ("Menu", "--file", str(menu)), ("ok", "--file", str(link)),
                     ("ok", "--file", "/nonexistent")):
            self.assertNotEqual(0, run("add", "--dir", str(rdir), *args).returncode, args)
        self.y("owner", "verifier", "bind", "alice", "--token-file", str(rdir / "verifier.token"), "--label", "resources")
        svc = Service([sys.executable, str(PUBLIC / "bot_resources.py"), "serve", "--dir", str(rdir)],
                      plain_env(self.root), self.root / "resources.log").start()
        self.addCleanup(svc.stop)
        self.y("owner", "bot", "edit", "alice", "--offered", "faq,calendar_slots,chat,resource:menu,"
                                                             "resource:secret")
        caller = self.y("caller", "connect", self.gw.url + "/@alice", "--name", "Granted visitor")["as"]
        self.y("other", "connect", self.gw.url + "/@alice", "--name", "Other visitor")
        self.y("owner", "owner", "contact", "alice", caller, "grant", "--capabilities", "resource:menu")
        self.y("owner", "owner", "contact", "alice", caller, "trust")
        self.y("host", "job", "setup", alice, "--resource-url", svc.url, "--resource-token-file",
               str(rdir / "access.token"))
        mid = self.y("caller", "chat", "alice", "what is on the menu?")["message_id"]
        job = self.y("host", "job", "claim", alice, mid)
        self.assertEqual(["menu"], job["allowed"]["resources"])
        got = self.y("host", "job", "resource", alice, mid, "menu")
        self.assertEqual("SYNTHETIC-MENU: soup, bread\n", got["content"])
        refused = self.y("host", "job", "resource", alice, mid, "secret", expect=2)
        self.assertEqual("not_authorized", refused["error"])
        self.y("host", "job", "resource", alice, mid, "../secret", expect=2)
        # the other visitor's job carries no resource ticket at all
        self.y("owner", "owner", "contact", "alice", self.y("other", "identities")["identities"][0]["visitor"][
            "actor_id"], "trust")
        other_mid = self.y("other", "chat", "alice", "and for me?")["message_id"]
        # an accepted (trusted) caller is in the inbox: the reply goes out without review
        self.assertEqual("completed", self.y("host", "job", "reply", alice, mid, "--text", "Soup and bread.")["state"])
        other_job = self.y("host", "job", "claim", alice, other_mid)
        self.assertEqual([], other_job["allowed"]["resources"])
        self.y("host", "job", "resource", alice, other_mid, "menu", expect=2)
        # revoking the grant fences a job that already holds a ticket
        third = self.y("caller", "chat", "alice", "the menu again?")["message_id"]
        self.y("host", "job", "claim", alice, third)
        self.y("owner", "owner", "contact", "alice", caller, "grant", "--capabilities", "none")
        self.y("host", "job", "resource", alice, third, "menu", expect=2)
        # the service itself: no access token, malformed ids, unknown fields
        for body, token in (({"ticket": C.new_token("ticket"), "resource": "menu"}, None),
                            ({"ticket": C.new_token("ticket"), "resource": "../secret"},
                             (rdir / "access.token").read_text().strip()),
                            ({"ticket": C.new_token("ticket"), "resource": "menu", "path": "/etc/passwd"},
                             (rdir / "access.token").read_text().strip())):
            status, obj = C.api_call(svc.url, "POST", "/v1/resource", body, token)
            self.assertIn(status, (400, 401, 403), body)
            self.assertNotIn("SYNTHETIC", json.dumps(obj))
        self.assertNotIn("SYNTHETIC-SECRET", "".join(self.outputs))
        self.assertNotIn("SYNTHETIC-SECRET", (self.gw.home / "gateway.sqlite3").read_bytes().decode("latin-1"))


class ExportReleaseOrderTests(unittest.TestCase):
    """Resource review R1: an export removed or replaced before the final check is never released. The gateway is
    stubbed here so the owner's change can land exactly between the two authorizations; the HTTP path itself is
    covered by ResourceTests."""

    def service(self, through_link=False):
        import bot_resources as R
        tmp = tempfile.TemporaryDirectory(prefix="yobiru exports ")
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name) / "svc"
        if through_link:            # like macOS, where /var and /tmp are links to /private/...
            (Path(tmp.name) / "real").mkdir()
            os.symlink(str(Path(tmp.name) / "real"), str(Path(tmp.name) / "link"))
            d = Path(tmp.name) / "link" / "svc"
        with redirect_stdout(io.StringIO()):
            R.main(["init", "--dir", str(d), "--gateway", "http://127.0.0.1:9", "--bot", "bot_aaaaaaaaaaaaaaaa"])
        C.create_private_file(d / "verifier.token", C.new_token("verifier") + "\n")
        src = Path(tmp.name) / "note.txt"
        src.write_text("NOTE-V1\n")
        with redirect_stdout(io.StringIO()):
            R.main(["add", "--dir", str(d), "note", "--file", str(src)])
        return R, R.Service(d), d, src

    def stub(self, svc, between):
        grant = {"bot_id": svc.bot_id, "message_id": "msg_aaaaaaaaaaaaaaaa", "conversation_id": "cnv_aaaaaaaaaaaaaaaa",
                 "subject": "s" * 32, "generation": 1, "capability": "resource:note", "resource": "note"}
        calls = []

        def redeem(verifier, ticket, rid):
            calls.append(rid)
            if len(calls) == 2:
                between()
            return dict(grant)
        svc.redeem = redeem
        return calls

    def test_an_unchanged_export_is_released(self):
        R, svc, d, src = self.service()
        self.stub(svc, lambda: None)
        self.assertEqual("NOTE-V1\n", svc.release(C.new_token("ticket"), "note")["content"])

    def test_a_service_folder_under_a_linked_parent_is_released(self):
        R, svc, d, src = self.service(through_link=True)
        self.stub(svc, lambda: None)
        self.assertEqual("NOTE-V1\n", svc.release(C.new_token("ticket"), "note")["content"])

    def test_an_exports_folder_swapped_for_a_link_releases_nothing(self):
        R, svc, d, src = self.service()
        (d / "exports").rename(d / "exports-moved")
        os.symlink(str(d / "exports-moved"), str(d / "exports"))
        self.stub(svc, lambda: None)
        with self.assertRaises(R.Refuse) as cm:
            svc.release(C.new_token("ticket"), "note")
        self.assertEqual((503, "not_configured"), (cm.exception.status, cm.exception.code))

    def test_a_remove_before_the_final_check_releases_nothing(self):
        R, svc, d, src = self.service()
        self.stub(svc, lambda: R.main(["remove", "--dir", str(d), "note"]))
        with redirect_stdout(io.StringIO()), self.assertRaises(R.Refuse) as cm:
            svc.release(C.new_token("ticket"), "note")
        self.assertEqual((404, "not_found"), (cm.exception.status, cm.exception.code))

    def test_a_replacement_before_the_final_check_releases_neither_version(self):
        R, svc, d, src = self.service()

        def replace():
            src.write_text("NOTE-V2\n")
            R.main(["add", "--dir", str(d), "note", "--file", str(src)])
        self.stub(svc, replace)
        with redirect_stdout(io.StringIO()), self.assertRaises(R.Refuse) as cm:
            svc.release(C.new_token("ticket"), "note")
        self.assertEqual((409, "resource_changed"), (cm.exception.status, cm.exception.code))


    def test_a_title_only_replacement_in_the_same_second_releases_nothing(self):
        R, svc, d, src = self.service()

        def retitle():
            path = d / "exports" / "note.json"
            data = json.loads(path.read_text())
            data["title"] = "A different title"
            C.write_private_json(path, data)
        self.stub(svc, retitle)
        with self.assertRaises(R.Refuse) as cm:
            svc.release(C.new_token("ticket"), "note")
        self.assertEqual((409, "resource_changed"), (cm.exception.status, cm.exception.code))


class ClientFixTests(FriendsClientCase):
    """Focused regressions for the final client review: status by message id for a source-bot-only identity, and a
    bounded --text-file."""

    def test_status_by_message_id_works_for_a_source_bot_only_identity(self):
        self.owner_bot("alice")
        self.y("caller", "enroll-bot", self.gw.url, "--handle", "only-source", "--name", "Source only")
        self.y("caller", "connect", self.gw.url + "/@alice")
        mid = self.y("caller", "chat", "alice", "hello from a source bot")["message_id"]
        P.SentLog(P.State(self.homes["caller"])).db.execute("DELETE FROM ops")     # as if sent from another device
        self.assertEqual(mid, self.y("caller", "status", mid)["message_id"])
        self.assertEqual(mid, self.y("caller", "status", mid, "--gateway", self.gw.url, "--as", "@only-source")[
            "message_id"])

    def test_a_reply_file_is_read_with_a_bound_and_never_sent_when_too_large(self):
        alice = self.owner_bot("alice")
        self.native("alice", alice)
        actor = self.y("caller", "connect", self.gw.url + "/@alice", "--name", "Visitor")["as"]
        self.y("owner", "owner", "contact", "alice", actor, "trust")
        mid = self.y("caller", "chat", "alice", "long answer please")["message_id"]
        self.y("host", "job", "claim", alice, mid)
        big = self.root / "big.txt"
        with big.open("wb") as f:
            f.write(b"x" * (64 * 1024 + 1))
        real_open, sizes = open, []

        class Tracking(object):
            def __init__(self, f):
                self.f = f

            def read(self, n=-1):
                sizes.append(n)
                return self.f.read(n)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.f.close()

        import builtins
        builtins.open = lambda path, mode="r", *a, **k: Tracking(real_open(path, mode, *a, **k)) \
            if str(path) == str(big) else real_open(path, mode, *a, **k)
        try:
            code, _, err = cli(self.homes["host"], "job", "reply", alice, mid, "--text-file", str(big))
        finally:
            builtins.open = real_open
        self.assertEqual(2, code)
        self.assertIn("larger than 64 KiB", err)
        self.assertEqual([64 * 1024 + 1], sizes, "one bounded read")
        self.assertEqual("started", self.gw.sql("SELECT state FROM messages WHERE id=?", (mid,))[0]["state"],
                         "nothing was sent")
        ok_file = self.root / "ok.txt"
        ok_file.write_text("a fine answer")
        self.assertEqual("completed", self.y("host", "job", "reply", alice, mid, "--text-file", str(ok_file))["state"],
                         "an accepted (trusted) sender is in the inbox: no review")


class RetryIdentityTests(FriendsClientCase):
    def test_an_op_key_never_switches_identity(self):
        """Client review C1."""
        self.owner_bot("alice")
        a = self.y("caller", "enroll-bot", self.gw.url, "--handle", "agent-a", "--name", "A")["bot_id"]
        b = self.y("other", "enroll-bot", self.gw.url, "--handle", "agent-b", "--name", "B")["bot_id"]
        creds = json.loads((self.homes["other"] / "public" / "credentials.json").read_text())
        P.State(self.homes["caller"]).update("credentials.json", lambda c: c["gateways"][self.gw.url][
            "sources"].update({b: creds["gateways"][self.gw.url]["sources"][b]}))
        self.y("caller", "add", self.gw.url + "/@alice", "--alias", "target", "--as", "@agent-a")
        self.y("caller", "chat", "target", "same message", "--as", "@agent-a", "--op-key", "identity-retry-1")
        code, out, err = cli(self.homes["caller"], "chat", "target", "same message", "--as", "@agent-b", "--op-key",
                             "identity-retry-1")
        self.assertEqual(2, code)
        self.assertIn("identity", err)
        rows = self.gw.sql("SELECT actor_id FROM messages WHERE author='caller'")
        self.assertEqual([a], [r["actor_id"] for r in rows], "nothing was sent as B")

    def test_a_native_rebind_is_validated_before_anything_changes(self):
        """Client review C2."""
        alice = self.owner_bot("alice")
        self.native("alice", alice)
        before = self.gw.sql("SELECT agent_epoch FROM bots WHERE id=?", (alice,))[0]["agent_epoch"]
        code, _, err = cli(self.homes["owner"], "worker", "bind", "alice", "--adapter", "native", "--replace")
        self.assertEqual(2, code)
        self.assertIn("Nothing was changed", err)
        self.assertEqual(before, self.gw.sql("SELECT agent_epoch FROM bots WHERE id=?", (alice,))[0]["agent_epoch"])
        self.assertEqual(1, len(self.gw.sql("SELECT id FROM workers WHERE bot_id=? AND adapter='native' AND "
                                            "revoked_ts IS NULL", (alice,))))

    def test_a_lost_enrollment_answer_never_costs_the_identity(self):
        """Client review C3 (sequential): lost success, then a refusal, then success again: one bot."""
        real = C.api_call
        state = {"drop": True}

        def lossy(*a, **k):
            result = real(*a, **k)
            if a[2] == "/v1/enroll" and state["drop"]:
                state["drop"] = False
                raise C.ApiError(None, "unreachable", "simulated lost answer")
            return result
        C.api_call = lossy
        try:
            first = cli(self.homes["caller"], "enroll-bot", self.gw.url, "--handle", "kept", "--name", "Kept")
        finally:
            C.api_call = real
        self.assertEqual(2, first[0])
        self.gw.stop()
        cfg = json.loads((self.gw.home / "gateway.json").read_text())
        cfg["enrollment"] = {"visitors": True, "bots": False, "self_enrolled_publish": False}
        (self.gw.home / "gateway.json").write_text(json.dumps(cfg))
        self.gw.start()
        again = self.y("caller", "enroll-bot", self.gw.url, "--handle", "kept", "--name", "Kept")
        self.assertTrue(again["duplicate"], "the gateway recognized the earlier enrollment")
        self.assertEqual(1, len(self.gw.sql("SELECT id FROM bots WHERE handle='kept'")))

    def test_a_late_refusal_never_erases_a_newer_identity(self):
        """Client review C3 (race): a refusal decided from an old snapshot keeps what changed since."""
        st = P.State(self.homes["caller"])
        base = self.gw.url
        pending = {"token": C.new_token("actor"), "pending": True, "display_name": "Racer", "dispatches": 1}
        st.save("credentials.json", {"v": 1, "gateways": {base: {"actor": dict(pending)}}})
        st.update("credentials.json", lambda c: c["gateways"][base].update(actor={
            "actor_id": "act_aaaaaaaaaaaaaaaa", "token": pending["token"], "display_name": "Racer"}))
        P.forget_refused_enrollment(st, base, "actor", pending, 403)
        self.assertEqual("act_aaaaaaaaaaaaaaaa", st.creds()["gateways"][base]["actor"]["actor_id"],
                         "a completed identity is kept")
        st.update("credentials.json", lambda c: c["gateways"][base].update(actor=dict(pending, dispatches=2)))
        P.forget_refused_enrollment(st, base, "actor", pending, 403)
        self.assertIn("actor", st.creds()["gateways"][base], "redispatched since: kept")
        other = dict(pending, token=C.new_token("actor"), dispatches=1)
        st.update("credentials.json", lambda c: c["gateways"][base].update(actor=other))
        P.forget_refused_enrollment(st, base, "actor", pending, 403)
        self.assertEqual(other["token"], st.creds()["gateways"][base]["actor"]["token"], "a different attempt: kept")
        st.update("credentials.json", lambda c: c["gateways"][base].update(actor=dict(pending)))
        P.forget_refused_enrollment(st, base, "actor", pending, 403)
        self.assertNotIn("actor", st.creds()["gateways"][base], "exactly this first, refused attempt is discarded")


if __name__ == "__main__":
    unittest.main()
