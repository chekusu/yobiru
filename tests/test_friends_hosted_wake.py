"""Hosted wakes: the gateway itself calls a bot's registered webhook when jobs wait, for owners whose agent runs
where no connector can (a hosted agent such as a Grok Bot routine). The route is sealed at rest and never shown
again; a wake carries ids only, uses the connector's ledger (one wake per job, an uncertain outcome is not
repeated) and refuses private addresses on a real gateway.

Real bot_gateway.py subprocess with its maintenance sweep, a loopback webhook receiver, synthetic identities.
"""
import hashlib
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from support.public_harness import C, FriendsGateway

LIMITS = {"min_deadline_seconds": 1, "read_timeout_seconds": 5, "messages_per_minute_per_actor": 1000,
          "messages_per_hour_per_actor": 5000, "pending_per_contact": 100, "new_conversations_per_hour_per_bot": 1000,
          "new_contacts_per_hour_per_bot": 1000, "enroll_per_hour_per_ip": 1000, "enroll_per_hour": 1000,
          "bot_enroll_per_hour": 1000, "bot_enroll_per_hour_per_ip": 1000, "agent_messages_per_hour_per_bot": 1000,
          "hosted_wake_private_hosts": 1, "hosted_wake_timeout_seconds": 3}
SECRET = "synthetic-wake-key-0123456789"


class Receiver(object):
    """A loopback webhook: records each request and answers with the configured status."""

    def __init__(self):
        self.requests, self.status = [], 202
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                outer.requests.append({"path": self.path, "headers": dict(self.headers), "body": json.loads(body)})
                self.send_response(outer.status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d/hooks/routine-token-in-path" % self.server.server_port

    def wait(self, count, seconds=8):
        deadline = time.time() + seconds
        while time.time() < deadline and len(self.requests) < count:
            time.sleep(0.1)
        return self.requests

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class HostedWakeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="yobiru hosted wake test ")
        cls.gw = FriendsGateway(Path(cls._tmp.name), limits=LIMITS).start()

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

    def register(self, bot_id, **extra):
        body = dict({"url": self.receiver.url, "secret": SECRET}, **extra)
        return self.gw.call("PUT", "/v1/owner/bots/%s/wake" % bot_id, body, self.gw.owner_token)

    def message(self, bot_id, text="a private message the webhook must not see"):
        actor_id, token = self.gw.actor()
        cid = self.gw.conversation(bot_id, token)
        status, sent = self.gw.send(cid, token, kind="chat", text=text)
        self.assertEqual(202, status, sent)
        return actor_id, sent["message_id"]

    def test_registration_is_validated_and_never_shown_again(self):
        bot_id = self.bot()
        for bad, error in (({"url": "ftp://example.com/x"}, "invalid_wake_url"),
                           ({"secret": "has space"}, "invalid_wake_secret"),
                           ({"header": "Bad Header"}, "invalid_wake_header")):
            status, body = self.register(bot_id, **bad)
            self.assertEqual((400, error), (status, body.get("error")), bad)
        status, body = self.register(bot_id)
        self.assertEqual(200, status, body)
        shown = self.gw.ok("GET", "/v1/owner/bots/%s/wake" % bot_id, None, self.gw.owner_token)
        text = json.dumps(shown)
        self.assertTrue(shown["configured"])
        self.assertEqual("127.0.0.1", shown["host"])
        self.assertNotIn(SECRET, text)
        self.assertNotIn("routine-token-in-path", text)
        stored = self.gw.sql("SELECT url_sealed, secret_sealed FROM hosted_wakes WHERE bot_id=?", (bot_id,))
        self.assertNotIn(SECRET, json.dumps(stored))
        self.assertNotIn("routine-token-in-path", json.dumps(stored))
        self.assertEqual(200, self.gw.call("DELETE", "/v1/owner/bots/%s/wake" % bot_id, None, self.gw.owner_token)[0])
        self.assertFalse(self.gw.ok("GET", "/v1/owner/bots/%s/wake" % bot_id, None, self.gw.owner_token)["configured"])

    def test_a_message_request_wakes_the_webhook_once_with_ids_only(self):
        bot_id = self.bot()
        self.assertEqual(200, self.register(bot_id)[0])
        _, mid = self.message(bot_id)
        requests = self.receiver.wait(1)
        self.assertEqual(1, len(requests), requests)
        request = requests[0]
        self.assertEqual("/hooks/routine-token-in-path", request["path"])
        self.assertEqual("Bearer " + SECRET, request["headers"].get("Authorization"))
        body = request["body"]
        self.assertEqual(("yobiru.job", mid, "review", bot_id, 1), (body["type"], body["job_id"], body["phase"],
                                                                      body["bot_id"], body["jobs"]))
        self.assertNotIn("private message", json.dumps(body))
        time.sleep(2.5)                     # later sweeps do not wake the same job again
        self.assertEqual(1, len(self.receiver.requests))
        status = self.gw.ok("GET", "/v1/owner/bots/%s/wake" % bot_id, None, self.gw.owner_token)
        self.assertEqual("accepted", status["last_outcome"])

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

    def test_an_uncertain_wake_is_not_repeated(self):
        bot_id = self.bot()
        self.receiver.status = 500
        self.assertEqual(200, self.register(bot_id)[0])
        self.message(bot_id)
        self.assertEqual(1, len(self.receiver.wait(1)))
        time.sleep(2.5)
        self.assertEqual(1, len(self.receiver.requests))
        status = self.gw.ok("GET", "/v1/owner/bots/%s/wake" % bot_id, None, self.gw.owner_token)
        self.assertEqual("uncertain", status["last_outcome"])

    def test_private_addresses_are_refused_on_a_real_gateway(self):
        for host in ("127.0.0.1", "10.0.0.5", "169.254.169.254", "::1", "localhost"):
            with self.assertRaises(ValueError, msg=host):
                C.public_address(host, 443)


if __name__ == "__main__":
    unittest.main()
