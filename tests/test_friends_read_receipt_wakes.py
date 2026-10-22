"""Resultless inbox completions must not hide later answers from connector wakes."""
import tempfile
import unittest
from pathlib import Path

from support.public_harness import C, FriendsGateway


class ReadReceiptWakeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="yobiru read receipt wakes ")
        cls.gw = FriendsGateway(Path(cls._tmp.name), limits={
            "messages_per_minute_per_actor": 1000,
            "messages_per_hour_per_actor": 5000,
            "agent_messages_per_hour_per_bot": 1000,
            "pending_per_contact": 100,
            "automation_turns_per_conversation": 100,
        }).start()

    @classmethod
    def tearDownClass(cls):
        cls.gw.stop()
        cls._tmp.cleanup()

    def check_answer_after_reads(self, expire_leases):
        tag = "expiry" if expire_leases else "explicit"
        target, target_source = self.gw.owned_bot("read-target-" + tag)
        sender, source = self.gw.owned_bot("read-sender-" + tag)
        self.assertEqual(201, self.gw.follow(source, target)[0])
        self.assertEqual(201, self.gw.follow(target_source, sender)[0])
        worker, _ = self.gw.native(target)
        _, sender_worker = self.gw.native(sender)
        connector = self.gw.ok("POST", "/v1/owner/bots/%s/workers/%s/connector" % (sender, sender_worker),
                               {}, self.gw.owner_token)["token"]
        cid = self.gw.conversation(target, source)
        read_ids = []
        for i in range(50):
            mid = self.gw.chat(cid, source, "message read without a reply %d" % i)
            read_ids.append(mid)
            status, job = self.gw.claim_job(worker, mid)
            self.assertEqual(200, status, job)
            if not expire_leases:
                body = {"lease_id": job["lease"]["lease_id"], "fence": job["lease"]["fence"], "outcome": "read"}
                done = self.gw.ok("POST", "/v1/worker/messages/%s/finish" % mid, body, worker)
                self.assertEqual(("completed", "read"), (done["state"], done["code"]))
        if expire_leases:
            # Advance only these synthetic leases; the normal jobs endpoint performs expiry handling.
            self.gw.sql("UPDATE messages SET lease_expires=? WHERE bot_id=? AND state='started'",
                        (C.now_ts() - 5, target))
            self.gw.ok("GET", "/v1/worker/jobs", None, worker)
        completed = self.gw.sql("SELECT state, code, result, caller_ack_ts FROM messages WHERE bot_id=?", (target,))
        self.assertEqual(50, len(completed))
        self.assertTrue(all(m == {"state": "completed", "code": "read", "result": None, "caller_ack_ts": None}
                            for m in completed), completed)
        self.assertEqual([], self.gw.ok("GET", "/v1/connector/jobs", None, connector)["replies"])
        read_view = self.gw.ok("GET", "/v1/messages/%s" % read_ids[0], None, source)
        self.assertNotIn("result", read_view)
        status, ack = self.gw.call("POST", "/v1/messages/%s/ack" % read_ids[0], {}, source)
        self.assertEqual((409, "nothing_to_acknowledge"), (status, ack["error"]))

        answered = self.gw.chat(cid, source, "this one needs an answer")
        status, job = self.gw.claim_job(worker, answered)
        self.assertEqual(200, status, job)
        status, done = self.gw.reply_job(worker, job, "the answer reaches the sender")
        self.assertEqual((200, "completed"), (status, done["state"]))
        replies = self.gw.ok("GET", "/v1/connector/jobs", None, connector)["replies"]
        self.assertEqual([{"message_id": answered, "from_bot_id": target, "state": "completed"}], replies)
        inbox = self.gw.ok("GET", "/v1/inbox", None, source)["items"]
        answer = next(m for m in inbox if m["message_id"] == answered)
        self.assertEqual("the answer reaches the sender", answer["result"]["text"])
        ack = self.gw.ok("POST", "/v1/messages/%s/ack" % answered, {}, source)
        self.assertFalse(ack["duplicate"])
        self.assertEqual([], self.gw.ok("GET", "/v1/connector/jobs", None, connector)["replies"])

    def test_answer_after_fifty_explicit_read_receipts_wakes_sender(self):
        self.check_answer_after_reads(expire_leases=False)

    def test_answer_after_fifty_expired_inbox_leases_wakes_sender(self):
        self.check_answer_after_reads(expire_leases=True)


if __name__ == "__main__":
    unittest.main()
