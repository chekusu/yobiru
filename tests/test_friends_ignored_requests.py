"""Ignored requests stay private and silent without blocking later admitted work."""
import json
import tempfile
import unittest
from pathlib import Path

from support.public_harness import C, FriendsGateway


class IgnoredRequestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="yobiru ignored requests ")
        cls.gw = FriendsGateway(Path(cls.tmp.name)).start()
        cls.sequence = 0

    @classmethod
    def tearDownClass(cls):
        cls.gw.stop()
        cls.tmp.cleanup()

    def request_sender(self):
        type(self).sequence += 1
        bot, _ = self.gw.owned_bot("ignored-%d" % self.sequence)
        actor, token = self.gw.actor()
        worker, wid = self.gw.native(bot)
        connector = self.gw.ok("POST", "/v1/owner/bots/%s/workers/%s/connector" % (bot, wid), {},
                               self.gw.owner_token)["token"]
        return bot, actor, token, worker, connector, self.gw.conversation(bot, token)

    def ignore(self, mid):
        return self.gw.call("POST", "/v1/owner/messages/%s/review" % mid, {"action": "ignore"},
                            self.gw.owner_token)

    def test_ignored_chat_does_not_block_the_next_individually_admitted_turn(self):
        bot, _, caller, worker, _, cid = self.request_sender()
        first = self.gw.chat(cid, caller, "IGNORED-SECRET")
        second = self.gw.chat(cid, caller, "please answer this one")
        before = self.gw.ok("GET", "/v1/messages/%s" % first, None, caller)
        self.assertEqual(200, self.ignore(first)[0])
        self.assertEqual(before, self.gw.ok("GET", "/v1/messages/%s" % first, None, caller))
        self.assertEqual(200, self.gw.admit(bot, second)[0])
        self.assertEqual([], self.gw.reviews(bot)["inbound"])
        listed = self.gw.ok("GET", "/v1/worker/jobs", None, worker)["jobs"]
        self.assertTrue(next(j for j in listed if j["job_id"] == second)["claimable"])
        status, job = self.gw.claim_job(worker, second)
        self.assertEqual(200, status, job)
        self.assertNotIn("IGNORED-SECRET", json.dumps(job))
        status, reply = self.gw.reply_job(worker, job, "owner reviews this draft")
        self.assertEqual((200, "review"), (status, reply["state"]))
        self.assertEqual(200, self.gw.approve_reply(bot, second)[0])
        status, blocked = self.gw.claim_job(worker, first)
        self.assertEqual((409, "review_required"), (status, blocked["error"]))
        self.assertEqual(before, self.gw.ok("GET", "/v1/messages/%s" % first, None, caller))

    def test_ignored_chat_is_not_a_connector_wake_until_sender_is_trusted(self):
        bot, actor, caller, worker, connector, cid = self.request_sender()
        mid = self.gw.chat(cid, caller, "IGNORED-WAKE-SECRET")
        jobs = lambda: self.gw.ok("GET", "/v1/connector/jobs", None, connector)["jobs"]
        self.assertEqual([{"job_id": mid, "phase": "review", "wake_attempts": 0}], jobs())
        self.assertEqual(200, self.ignore(mid)[0])
        self.assertEqual([], jobs())
        status, result = self.gw.call("POST", "/v1/connector/messages/%s/wake" % mid,
                                      {"step": "intent", "wake_id": C.new_token("ticket")[4:],
                                       "phase": "review"}, connector)
        self.assertEqual((409, "not_due"), (status, result["error"]))
        self.assertEqual(200, self.gw.contact(bot, actor, "trust")[0])
        self.assertEqual([{"job_id": mid, "phase": "ready", "wake_attempts": 0}], jobs())
        status, job = self.gw.claim_job(worker, mid)
        self.assertEqual(200, status, job)
        self.assertEqual("IGNORED-WAKE-SECRET", job["untrusted_content"]["text"])

    def test_notice_ignore_is_silent_idempotent_and_can_be_admitted_later(self):
        bot, _, caller, worker, _, cid = self.request_sender()
        status, sent = self.gw.send(cid, caller, kind="notice", text="IGNORED-NOTICE-SECRET")
        self.assertEqual(202, status, sent)
        mid = sent["message_id"]
        request = next(r for r in self.gw.reviews(bot)["inbound"] if r["job_id"] == mid)
        before = self.gw.ok("GET", "/v1/messages/%s" % mid, None, caller)
        for duplicate in (False, True):
            status, result = self.ignore(mid)
            self.assertEqual((200, "ignored", duplicate),
                             (status, result.get("inbound"), result.get("duplicate")), result)
        self.assertEqual([], self.gw.reviews(bot)["inbound"])
        listed = self.gw.ok("GET", "/v1/worker/notices", None, worker)
        self.assertEqual([], listed["notices"])
        self.assertEqual(0, listed["waiting_for_owner_review"])
        self.assertNotIn("IGNORED-NOTICE-SECRET", json.dumps(listed))
        status, blocked = self.gw.call("GET", "/v1/worker/notices/%s" % mid, None, worker)
        self.assertEqual((409, "review_required"), (status, blocked["error"]))
        self.assertEqual(before, self.gw.ok("GET", "/v1/messages/%s" % mid, None, caller))
        self.gw.ok("POST", "/v1/owner/messages/%s/review" % mid,
                   {"action": "approve_inbound", "request_hash": request["request_hash"]}, self.gw.owner_token)
        self.assertEqual([mid], [n["notice_id"] for n in
                                self.gw.ok("GET", "/v1/worker/notices", None, worker)["notices"]])
        notice = self.gw.ok("GET", "/v1/worker/notices/%s" % mid, None, worker)
        self.assertEqual("IGNORED-NOTICE-SECRET", notice["untrusted_content"]["text"])
        self.gw.ok("POST", "/v1/worker/notices/%s/ack" % mid, {}, worker)


if __name__ == "__main__":
    unittest.main()
