"""Hosted wakes: the gateway itself calls a bot's registered webhook when jobs wait, for owners whose agent runs
where no connector can (a hosted agent such as a Grok Bot routine). The route is sealed at rest and never shown
again; a wake carries ids only and is signed, uses the connector's ledger (one wake per job, an uncertain outcome
is not repeated), waits for a burst to settle so an online connector goes first, backs off and turns a failing
route off, and refuses private addresses, other ports and framing headers on a real gateway. Calls run in their
own processes, so a slow webhook holds nothing else (test_friends_hosted_wake_workers.py has the details).

Real bot_gateway.py subprocesses with their maintenance sweep, loopback webhook receivers, synthetic identities.
"""
import hashlib
import hmac
import json
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn

from support.public_harness import C, FriendsGateway, cli

LIMITS = {"min_deadline_seconds": 1, "read_timeout_seconds": 5, "messages_per_minute_per_actor": 1000,
          "messages_per_hour_per_actor": 5000, "pending_per_contact": 100, "new_conversations_per_hour_per_bot": 1000,
          "new_contacts_per_hour_per_bot": 1000, "enroll_per_hour_per_ip": 1000, "enroll_per_hour": 1000,
          "bot_enroll_per_hour": 1000, "bot_enroll_per_hour_per_ip": 1000, "agent_messages_per_hour_per_bot": 1000,
          "hosted_wake_private_hosts": 1, "hosted_wake_timeout_seconds": 3, "hosted_wake_settle_seconds": 0,
          "hosted_wake_max_delay_seconds": 3, "hosted_wake_min_interval_seconds": 1,
          "hosted_review_wakes_per_hour_per_bot": 2, "hosted_wake_disable_after_failures": 2,
          "hosted_wake_disable_after_seconds": 1}
# LIMITS call at once (other test files sweep once and expect the call); these wait a second for a burst to settle.
SETTLE_LIMITS = dict(LIMITS, hosted_wake_settle_seconds=1)
SECRET = "synthetic-wake-key-0123456789"


class _Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class Receiver(object):
    """A loopback webhook: records each request (with its raw body) and answers with the configured status, after
    `delay` seconds."""

    def __init__(self, delay=0):
        self.requests, self.status, self.delay = [], 202, delay
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                outer.requests.append({"at": time.time(), "path": self.path, "headers": dict(self.headers),
                                       "raw": raw, "body": json.loads(raw)})
                time.sleep(outer.delay)
                try:
                    self.send_response(outer.status)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        self.server = _Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d/hooks/routine-token-in-path" % self.server.server_port

    def wait(self, count, seconds=10):
        deadline = time.time() + seconds
        while time.time() < deadline and len(self.requests) < count:
            time.sleep(0.05)
        return self.requests

    def jobs(self):
        return [r for r in self.requests if r["body"]["type"] == "yobiru.job"]

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class HostedWakeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="yobiru hosted wake test ")
        cls.gw = FriendsGateway(Path(cls._tmp.name), limits=SETTLE_LIMITS).start()

    @classmethod
    def tearDownClass(cls):
        cls.gw.stop()
        cls._tmp.cleanup()

    def setUp(self):
        self.receiver = Receiver()

    def tearDown(self):
        self.receiver.stop()

    def bot(self, tag=""):
        bot_id = self.gw.bot("w%s%s" % (hashlib.sha256(self.id().encode()).hexdigest()[:12], tag))
        self.gw.policy(bot_id, offered=["faq", "chat"], visitor=["faq", "chat"])
        return bot_id

    def register(self, bot_id, receiver=None, **extra):
        body = dict({"url": (receiver or self.receiver).url, "secret": SECRET}, **extra)
        return self.gw.call("PUT", "/v1/owner/bots/%s/wake" % bot_id, body, self.gw.owner_token)

    def status(self, bot_id):
        return self.gw.ok("GET", "/v1/owner/bots/%s/wake" % bot_id, None, self.gw.owner_token)

    def message(self, bot_id, text="a private message the webhook must not see"):
        actor_id, token = self.gw.actor()
        cid = self.gw.conversation(bot_id, token)
        status, sent = self.gw.send(cid, token, kind="chat", text=text)
        self.assertEqual(202, status, sent)
        return actor_id, sent["message_id"]

    def wait_for(self, predicate, seconds=10):
        deadline = time.time() + seconds
        while time.time() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.1)
        return predicate()

    def test_registration_is_validated_and_never_shown_again(self):
        bot_id = self.bot()
        for bad, error in (({"url": "ftp://example.com/x"}, "invalid_wake_url"),
                           ({"secret": "has space"}, "invalid_wake_secret"),
                           ({"header": "Bad Header"}, "invalid_wake_header"),
                           ({"header": "Host"}, "invalid_wake_header"),
                           ({"header": "Content-Length", "scheme": None}, "invalid_wake_header"),
                           ({"header": "Transfer-Encoding"}, "invalid_wake_header"),
                           ({"header": "X-Yobiru-Signature"}, "invalid_wake_header"),
                           ({"note": "x" * 281}, "invalid_field"),
                           ({"note": "line\nbreak"}, "invalid_field")):
            status, body = self.register(bot_id, **bad)
            self.assertEqual((400, error), (status, body.get("error")), bad)
        status, body = self.register(bot_id, header="X-Routine-Key", scheme=None)
        self.assertEqual(200, status, body)
        status, body = self.register(bot_id)
        self.assertEqual(200, status, body)
        shown = self.status(bot_id)
        text = json.dumps(shown)
        self.assertTrue(shown["configured"])
        self.assertTrue(shown["enabled"])
        self.assertEqual("127.0.0.1", shown["host"])
        self.assertNotIn(SECRET, text)
        self.assertNotIn("routine-token-in-path", text)
        stored = self.gw.sql("SELECT url_sealed, secret_sealed FROM hosted_wakes WHERE bot_id=?", (bot_id,))
        self.assertNotIn(SECRET, json.dumps(stored))
        self.assertNotIn("routine-token-in-path", json.dumps(stored))
        self.assertEqual(200, self.gw.call("DELETE", "/v1/owner/bots/%s/wake" % bot_id, None, self.gw.owner_token)[0])
        self.assertFalse(self.status(bot_id)["configured"])
        self.assertEqual(404, self.gw.call("POST", "/v1/owner/bots/%s/wake/test" % bot_id, {},
                                           self.gw.owner_token)[0])

    def test_registering_sends_a_signed_test_wake(self):
        bot_id = self.bot()
        status, body = self.register(bot_id, test=True, note="Run the synthetic inbox command.")
        self.assertEqual(200, status, body)
        self.assertTrue(body["test_pending"])
        requests = self.receiver.wait(1)
        self.assertEqual(1, len(requests), requests)
        request = requests[0]
        self.assertEqual("yobiru.ping", request["body"]["type"])
        self.assertEqual("Run the synthetic inbox command.", request["body"]["note"])
        self.assertEqual("Bearer " + SECRET, request["headers"].get("Authorization"))
        sig = dict(part.split("=", 1) for part in request["headers"]["X-Yobiru-Signature"].split(","))
        expected = hmac.new(SECRET.encode(), sig["t"].encode() + b"." + request["raw"], hashlib.sha256).hexdigest()
        self.assertTrue(hmac.compare_digest(expected, sig["v1"]))
        self.assertLess(abs(int(sig["t"]) - time.time()), 30)
        shown = self.wait_for(lambda: (lambda s: s if s["last_outcome"] else None)(self.status(bot_id)))
        self.assertEqual(("test", "accepted", False), (shown["last_kind"], shown["last_outcome"],
                                                        shown["test_pending"]))

    def test_a_message_request_wakes_the_webhook_once_with_ids_only(self):
        bot_id = self.bot()
        self.assertEqual(200, self.register(bot_id)[0])
        sent_at = time.time()
        _, mid = self.message(bot_id)
        requests = self.receiver.wait(1)
        self.assertEqual(1, len(requests), requests)
        request = requests[0]
        self.assertGreaterEqual(request["at"] - sent_at, 0.9, "the gateway waits for the burst to settle")
        self.assertEqual("/hooks/routine-token-in-path", request["path"])
        self.assertEqual("Bearer " + SECRET, request["headers"].get("Authorization"))
        body = request["body"]
        self.assertEqual(("yobiru.job", mid, "review", bot_id, 1), (body["type"], body["job_id"], body["phase"],
                                                                      body["bot_id"], body["jobs"]))
        self.assertNotIn("private message", json.dumps(body))
        self.assertNotIn("mokutori", json.dumps(body).lower())
        time.sleep(2.5)                     # later rounds do not wake the same job again
        self.assertEqual(1, len(self.receiver.requests))
        self.assertEqual("accepted", self.status(bot_id)["last_outcome"])

    def test_a_burst_of_messages_is_one_wake(self):
        bot_id = self.bot()
        self.assertEqual(200, self.register(bot_id)[0])
        callers = []
        for _ in range(4):
            _, token = self.gw.actor()
            callers.append((self.gw.conversation(bot_id, token), token))
        for i, (cid, token) in enumerate(callers):
            self.assertEqual(202, self.gw.send(cid, token, kind="chat", text="burst %d" % i)[0])
        self.assertEqual(1, len(self.receiver.wait(1)))
        time.sleep(1.5)
        self.assertEqual(1, len(self.receiver.requests), self.receiver.requests)
        self.assertEqual(4, self.receiver.requests[0]["body"]["jobs"])

    def test_without_review_wakes_only_admitted_work_wakes(self):
        bot_id = self.bot()
        self.assertEqual(200, self.register(bot_id, wake_on_review=False)[0])
        actor_id, mid = self.message(bot_id)
        time.sleep(2.5)
        self.assertEqual([], self.receiver.requests)
        self.assertEqual(200, self.gw.contact(bot_id, actor_id, "trust")[0])
        requests = self.receiver.wait(1)
        self.assertEqual(1, len(requests), requests)
        self.assertEqual((mid, "ready"), (requests[0]["body"]["job_id"], requests[0]["body"]["phase"]))

    def test_message_requests_alone_have_their_own_hourly_limit(self):
        if 3600 - time.time() % 3600 < 30:          # keep the three requests inside one hourly window
            time.sleep(3600 - time.time() % 3600 + 1)
        bot_id = self.bot()
        self.assertEqual(200, self.register(bot_id)[0])
        for expected in (1, 2):
            self.message(bot_id, text="request %d" % expected)
            self.assertEqual(expected, len(self.receiver.wait(expected)))
            time.sleep(1.2)                         # past the minimum interval
        self.message(bot_id, text="request 3")
        time.sleep(4)
        self.assertEqual(2, len(self.receiver.requests), "the third stranger's request does not wake the routine")

    def test_an_uncertain_wake_is_not_repeated(self):
        bot_id = self.bot()
        self.receiver.status = 500
        self.assertEqual(200, self.register(bot_id)[0])
        self.message(bot_id)
        self.assertEqual(1, len(self.receiver.wait(1)))
        time.sleep(2.5)
        self.assertEqual(1, len(self.receiver.requests))
        status = self.status(bot_id)
        self.assertEqual(("uncertain", 1, True), (status["last_outcome"], status["consecutive_failures"],
                                                   status["enabled"]))
        self.assertIsNotNone(status["next_wake_after"])

    def test_a_failing_route_is_turned_off_and_an_accepted_test_turns_it_on(self):
        bot_id = self.bot()
        self.receiver.status = 503
        self.assertEqual(200, self.register(bot_id)[0])
        self.message(bot_id, text="first")
        self.assertEqual(1, len(self.receiver.wait(1)))
        self.wait_for(lambda: self.status(bot_id)["consecutive_failures"] == 1)
        self.message(bot_id, text="second")
        self.assertEqual(2, len(self.receiver.wait(2)))
        status = self.wait_for(lambda: (lambda s: s if not s["enabled"] else None)(self.status(bot_id)))
        self.assertFalse(status["enabled"], status)
        self.assertIsNotNone(status["disabled_at"])
        self.message(bot_id, text="third")
        time.sleep(2.5)
        self.assertEqual(2, len(self.receiver.requests), "a route that is off is not called")
        self.receiver.status = 202
        status, body = self.gw.call("POST", "/v1/owner/bots/%s/wake/test" % bot_id, {}, self.gw.owner_token)
        self.assertEqual(202, status, body)
        status = self.wait_for(lambda: (lambda s: s if s["enabled"] else None)(self.status(bot_id)))
        self.assertTrue(status["enabled"], status)
        self.assertEqual((0, "test"), (status["consecutive_failures"], status["last_kind"]))
        self.assertEqual("yobiru.ping", self.receiver.requests[2]["body"]["type"])

    def test_a_slow_webhook_does_not_hold_other_bots(self):
        slow = Receiver(delay=20)
        try:
            for i in range(3):
                bot_id = self.bot("s%d" % i)
                self.assertEqual(200, self.register(bot_id, receiver=slow)[0])
                self.message(bot_id)
            slow.wait(3)
            self.assertEqual(3, len(slow.requests), "the slow calls are in flight")
            fast = self.bot("fast")
            self.assertEqual(200, self.register(fast)[0])
            sent_at = time.time()
            self.message(fast)
            requests = self.receiver.wait(1, seconds=8)
            self.assertEqual(1, len(requests))
            self.assertLess(requests[0]["at"] - sent_at, 4.0, "another bot's wake is not held by slow webhooks")
        finally:
            slow.stop()

    def test_private_addresses_are_refused_on_a_real_gateway(self):
        for host in ("127.0.0.1", "10.0.0.5", "169.254.169.254", "::1", "localhost"):
            with self.assertRaises(ValueError, msg=host):
                C.public_address(host, 443)
        for address in ("64:ff9b::7f00:1", "64:ff9b::a00:5", "::ffff:10.0.0.1", "2002:a00:1::1",
                        "100.100.1.1", "fd7a:115c:a1e0::1"):
            self.assertFalse(C.is_public_address(address), address)
        self.assertTrue(C.is_public_address("93.184.216.34"))
        self.assertTrue(C.is_public_address("2606:2800:220:1:248:1893:25c8:1946"))


CONNECTOR_LIMITS = dict(LIMITS, hosted_wake_settle_seconds=6, hosted_wake_max_delay_seconds=8)


class ConnectorFirstTests(unittest.TestCase):
    """A connector that is online records its wake intent within a second; the gateway waits longer than that."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="yobiru hosted wake connector ")
        cls.gw = FriendsGateway(Path(cls._tmp.name), limits=CONNECTOR_LIMITS).start()

    @classmethod
    def tearDownClass(cls):
        cls.gw.stop()
        cls._tmp.cleanup()

    def setUp(self):
        self.receiver = Receiver()

    def tearDown(self):
        self.receiver.stop()

    def test_an_online_connector_goes_first(self):
        handle = "wconn%d" % int(time.time() * 1000 % 1000000)
        bot_id, _ = self.gw.owned_bot(handle)
        self.gw.policy(bot_id, offered=["faq", "chat"], visitor=["faq", "chat"])
        _, wid = self.gw.native(bot_id)
        home = Path(self._tmp.name) / ("home-%s" % handle)
        log = Path(self._tmp.name) / ("wakes-%s.log" % handle)
        token_file = Path(self._tmp.name) / ("conn-%s.token" % handle)
        for args in (("owner", "login", self.gw.url, "--token-file", str(self.gw.home / "owner.token")),
                     ("connector", "bind", bot_id, wid, "--token-file", str(token_file)),
                     ("connector", "login", self.gw.url, "--token-file", str(token_file)),
                     ("connector", "config", bot_id, "--exec", sys.executable, "-c",
                      "import sys; open(sys.argv[1], 'a').write(sys.argv[2] + '\\n')", str(log), "{job_id}")):
            code, out, err = cli(home, *args)
            self.assertEqual(0, code, (args, out, err))
        status, body = self.gw.call("PUT", "/v1/owner/bots/%s/wake" % bot_id,
                                    {"url": self.receiver.url, "secret": SECRET}, self.gw.owner_token)
        self.assertEqual(200, status, body)
        _, token = self.gw.actor()
        cid = self.gw.conversation(bot_id, token)
        status, sent = self.gw.send(cid, token, kind="chat", text="for the connector")
        self.assertEqual(202, status, sent)
        mid = sent["message_id"]
        code, out, err = cli(home, "connector", "run", bot_id, "--once")
        self.assertEqual(0, code, (out, err))
        self.assertEqual([mid], log.read_text().split())
        time.sleep(CONNECTOR_LIMITS["hosted_wake_max_delay_seconds"] + 2)
        self.assertEqual([], self.receiver.requests, "the gateway stands in only when nothing else woke the bot")


class RealGatewayRouteTests(unittest.TestCase):
    """Default limits: https on port 443 only, and a delivery to a private address is refused before it leaves."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="yobiru hosted wake real ")
        cls.gw = FriendsGateway(Path(cls._tmp.name), limits={"enroll_per_hour": 1000}).start()

    @classmethod
    def tearDownClass(cls):
        cls.gw.stop()
        cls._tmp.cleanup()

    def test_only_https_on_port_443_and_never_a_private_address(self):
        bot_id = self.gw.bot("wreal")
        path = "/v1/owner/bots/%s/wake" % bot_id
        for url in ("https://hooks.example.com:8443/x", "http://127.0.0.1:8080/x", "http://hooks.example.com/x"):
            status, body = self.gw.call("PUT", path, {"url": url, "secret": SECRET}, self.gw.owner_token)
            self.assertEqual((400, "invalid_wake_url"), (status, body.get("error")), url)
        status, body = self.gw.call("PUT", path, {"url": "https://localhost/hooks/x", "secret": SECRET, "test": True},
                                    self.gw.owner_token)
        self.assertEqual(200, status, body)
        deadline = time.time() + 10
        shown = {}
        while time.time() < deadline and not shown.get("last_outcome"):
            time.sleep(0.2)
            shown = self.gw.ok("GET", path, None, self.gw.owner_token)
        self.assertEqual("refused", shown["last_outcome"], shown)
        self.assertIn("not a public address", shown["last_detail"])


class MultiProcessWakeTests(unittest.TestCase):
    """With several gateway processes, the maintenance process dispatches the calls."""

    def test_the_maintenance_process_calls_the_webhook(self):
        with tempfile.TemporaryDirectory(prefix="yobiru hosted wake procs ") as tmp:
            gw = FriendsGateway(Path(tmp), limits=LIMITS)
            cfg_path = gw.home / "gateway.json"
            cfg = json.loads(cfg_path.read_text())
            cfg["processes"] = 2
            cfg_path.write_text(json.dumps(cfg))
            receiver = Receiver()
            gw.start()
            try:
                bot_id = gw.bot("wprocs")
                status, body = gw.call("PUT", "/v1/owner/bots/%s/wake" % bot_id,
                                       {"url": receiver.url, "secret": SECRET, "test": True}, gw.owner_token)
                self.assertEqual(200, status, body)
                self.assertEqual(1, len(receiver.wait(1)))
                self.assertEqual("yobiru.ping", receiver.requests[0]["body"]["type"])
            finally:
                gw.stop()
                receiver.stop()


if __name__ == "__main__":
    unittest.main()
