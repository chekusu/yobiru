"""Friends contracts for a Muse scheduled receive/reply recipe.

Real packaged commands and an isolated loopback gateway exercise credential separation,
metadata polling, admission/reply review and delayed receipt acknowledgement. The delivery
file below stands in for a host's durable visible delivery; no Muse Hook or account is used.
"""
import json
import tempfile
import unittest
from pathlib import Path

from support.pkg import Package
from support.public_harness import FriendsGateway


class ScheduledFriendsTests(unittest.TestCase):
    def setUp(self):
        self.pkg = Package()
        self.addCleanup(self.pkg.cleanup)
        tmp = tempfile.TemporaryDirectory(prefix="yobiru scheduled friends ")
        self.addCleanup(tmp.cleanup)
        self.gw = FriendsGateway(Path(tmp.name)).start()
        self.addCleanup(self.gw.stop)
        self.homes = {role: self.pkg.new_home(role) for role in ("owner", "native", "source")}
        self.y("owner", "owner", "login", self.gw.url,
               "--token-file", str(self.gw.home / "owner.token"))
        self.receiver = self.make_bot("receiver")
        self.sender = self.make_bot("sender")
        native_token = self.pkg.root / "native.token"
        self.y("owner", "worker", "bind", self.receiver, "--adapter", "native",
               "--token-file", str(native_token))
        self.y("native", "worker", "login", self.gw.url, "--token-file", str(native_token))
        source_token = self.pkg.root / "source.token"
        self.y("owner", "bot", "source", self.sender, "--token-file", str(source_token))
        self.y("source", "source", "login", self.gw.url, "--token-file", str(source_token))
        self.y("source", "connect", self.gw.url + "/@receiver", "--as", self.sender)

    def y(self, role, *args, expect=0):
        home = self.homes[role]
        result, obj = self.pkg.run_json(
            ["--home", str(home / "data"), "public"] + list(args), home)
        self.assertEqual(expect, result.returncode, "%s %s\n%s\n%s" % (role, args, obj, result.stderr))
        return obj

    def make_bot(self, handle):
        bot = self.y("owner", "bot", "create", "--handle", handle,
                     "--name", "Synthetic display name " + handle)["bot_id"]
        self.y("owner", "bot", "publish", bot)
        return bot

    def poll_jobs(self):
        return self.y("native", "job", "list", self.receiver)["jobs"]

    def inbox(self):
        return self.y("source", "inbox", "--gateway", self.gw.url,
                      "--as", self.sender, "--no-ack")["items"]

    @staticmethod
    def pending(items):
        return [item for item in items if not item["lifecycle"]["response_acknowledged_at"]]

    def test_scheduled_polls_preserve_admission_and_reply_review(self):
        text = "UNTRUSTED-SCHEDULED-BODY: a synthetic request"
        mid = self.y("source", "chat", "receiver", text, "--as", self.sender)["message_id"]
        for _ in range(2):
            jobs = self.poll_jobs()
            self.assertEqual([mid], [job["job_id"] for job in jobs])
            self.assertFalse(jobs[0]["claimable"])
            self.assertEqual("queued", jobs[0]["state"])
            self.assertEqual(0, jobs[0]["attempt"], "polling must not claim a job")
            self.assertNotIn(text, json.dumps(jobs))
            self.assertNotIn("Synthetic display name sender", json.dumps(jobs))
            self.assertNotIn("untrusted_content", jobs[0])
        denied = self.y("native", "job", "claim", self.receiver, mid, expect=2)
        self.assertEqual("review_required", denied["error"])

        self.y("owner", "review", "approve-inbound", self.receiver, mid)
        self.assertTrue(self.poll_jobs()[0]["claimable"])
        job = self.y("native", "job", "claim", self.receiver, mid)
        self.assertEqual(text, job["untrusted_content"]["text"])
        self.assertEqual("owner_approval_required", job["review"]["reply"])
        duplicate = self.y("native", "job", "claim", self.receiver, mid, expect=2)
        self.assertEqual("not_claimable", duplicate["error"])
        self.assertEqual("started", duplicate["state"])
        finished = self.y("native", "job", "reply", self.receiver, mid, "--text", "Synthetic reviewed reply")
        self.assertEqual("review", finished["state"])
        status = self.y("source", "status", mid, "--no-ack")
        self.assertEqual("review", status["state"])
        self.assertNotIn("result", status)
        self.assertEqual([], self.inbox(), "a draft is not released by scheduled collection")

        draft = self.y("owner", "review", "list", self.receiver)["replies"][0]
        self.assertEqual("Synthetic reviewed reply", draft["draft"]["text"])
        self.y("owner", "review", "approve-reply", self.receiver, mid, "--draft-hash", draft["draft_hash"])
        released = self.y("source", "status", mid, "--no-ack")
        self.assertEqual("Synthetic reviewed reply", released["result"]["text"])
        self.assertIsNone(released["lifecycle"]["response_acknowledged_at"])

    def test_direct_reply_is_acknowledged_only_after_simulated_visible_delivery(self):
        self.assertEqual([], self.poll_jobs())
        self.assertEqual([], self.inbox())
        self.y("owner", "follow", self.sender, "--as", self.receiver, "--gateway", self.gw.url)
        mid = self.y("source", "chat", "receiver", "Synthetic direct request", "--as", self.sender)["message_id"]
        for _ in range(2):
            job = self.poll_jobs()[0]
            self.assertTrue(job["claimable"])
            self.assertEqual(0, job["attempt"])
        claim = self.y("native", "job", "claim", self.receiver, mid)
        self.assertEqual("direct", claim["review"]["reply"])
        done = self.y("native", "job", "reply", self.receiver, mid, "--text", "Synthetic direct reply")
        self.assertEqual("completed", done["state"])
        self.assertEqual([], self.poll_jobs())
        for _ in range(2):
            items = self.inbox()
            self.assertEqual([mid], [item["message_id"] for item in self.pending(items)])
            self.assertEqual("Synthetic direct reply", items[0]["result"]["text"])
            self.assertTrue(items[0]["lifecycle"]["response_served_at"])
            self.assertIsNone(items[0]["lifecycle"]["response_acknowledged_at"])

        # This synthetic host commits the displayed text before sending a receipt. A real
        # integration must verify its own native delivery contract at this boundary.
        delivered = self.pkg.root / "synthetic-visible-delivery.json"
        delivered.write_text(json.dumps({"message_id": mid, "text": items[0]["result"]["text"]}))
        self.assertEqual("Synthetic direct reply", json.loads(delivered.read_text())["text"])
        ack = self.y("source", "ack", mid, "--as", self.sender, "--gateway", self.gw.url)
        self.assertTrue(ack["ok"])
        self.assertIsNotNone(ack["acknowledged"][0]["acknowledged_at"])
        history = self.inbox()
        self.assertEqual([mid], [item["message_id"] for item in history], "inbox retains acknowledged history")
        self.assertEqual([], self.pending(history))
        repeated = self.y("source", "ack", mid, "--as", self.sender, "--gateway", self.gw.url)
        self.assertEqual(ack["acknowledged"][0]["acknowledged_at"], repeated["acknowledged"][0]["acknowledged_at"])


if __name__ == "__main__":
    unittest.main()
