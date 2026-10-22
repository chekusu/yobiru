"""Resultless inbox completions must not hide answers from connector wakes or the caller inbox."""
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
            "pending_per_contact": 250,
            "turns_per_conversation": 250,
            "automation_turns_per_conversation": 250,
            "automation_turns_per_pair_per_day": 250,
        }).start()

    @classmethod
    def tearDownClass(cls):
        cls.gw.stop()
        cls._tmp.cleanup()

    def check_answer_with_reads(self, expire_leases, answer_first=False, count=50):
        tag = "expiry" if expire_leases else "explicit"
        tag += "-first" if answer_first else "-last"
        target, target_source = self.gw.owned_bot("read-target-" + tag)
        sender, source = self.gw.owned_bot("read-sender-" + tag)
        self.assertEqual(201, self.gw.follow(source, target)[0])
        self.assertEqual(201, self.gw.follow(target_source, sender)[0])
        worker, _ = self.gw.native(target)
        _, sender_worker = self.gw.native(sender)
        connector = self.gw.ok("POST", "/v1/owner/bots/%s/workers/%s/connector" % (sender, sender_worker),
                               {}, self.gw.owner_token)["token"]
        cid = self.gw.conversation(target, source)

        def send_answer():
            mid = self.gw.chat(cid, source, "this one needs an answer")
            status, job = self.gw.claim_job(worker, mid)
            self.assertEqual(200, status, job)
            status, done = self.gw.reply_job(worker, job, "the answer reaches the sender")
            self.assertEqual((200, "completed"), (status, done["state"]))
            return mid

        answered = send_answer() if answer_first else None
        read_ids = []
        for i in range(count):
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
        completed = self.gw.sql("SELECT state, code, result, caller_ack_ts FROM messages WHERE bot_id=? AND id!=?",
                                (target, answered or ""))
        self.assertEqual(count, len(completed))
        self.assertTrue(all(m == {"state": "completed", "code": "read", "result": None, "caller_ack_ts": None}
                            for m in completed), completed)
        if not answer_first:
            self.assertEqual([], self.gw.ok("GET", "/v1/connector/jobs", None, connector)["replies"])
        read_view = self.gw.ok("GET", "/v1/messages/%s" % read_ids[0], None, source)
        self.assertEqual(("completed", "read"), (read_view["state"], read_view["code"]))
        self.assertNotIn("result", read_view)
        status, ack = self.gw.call("POST", "/v1/messages/%s/ack" % read_ids[0], {}, source)
        self.assertEqual((409, "nothing_to_acknowledge"), (status, ack["error"]))

        if not answer_first:
            answered = send_answer()
        replies = self.gw.ok("GET", "/v1/connector/jobs", None, connector)["replies"]
        self.assertEqual([{"message_id": answered, "from_bot_id": target, "state": "completed"}], replies)
        inbox = self.gw.ok("GET", "/v1/inbox", None, source)["items"]
        self.assertIn(answered, [m["message_id"] for m in inbox], "the reply wake must be consumable via the inbox")
        self.assertEqual([answered], [m["message_id"] for m in inbox], "read receipts need no acknowledgement")
        answer = next(m for m in inbox if m["message_id"] == answered)
        self.assertEqual("the answer reaches the sender", answer["result"]["text"])
        ack = self.gw.ok("POST", "/v1/messages/%s/ack" % answered, {}, source)
        self.assertFalse(ack["duplicate"])
        self.assertEqual([], self.gw.ok("GET", "/v1/connector/jobs", None, connector)["replies"])

    def test_answer_after_fifty_explicit_read_receipts_wakes_sender(self):
        self.check_answer_with_reads(expire_leases=False)

    def test_answer_after_fifty_expired_inbox_leases_wakes_sender(self):
        self.check_answer_with_reads(expire_leases=True)

    def test_pending_answer_before_fifty_read_receipts_remains_in_inbox(self):
        self.check_answer_with_reads(expire_leases=False, answer_first=True)

    def test_pending_answer_before_two_hundred_expired_leases_remains_in_inbox(self):
        self.check_answer_with_reads(expire_leases=True, answer_first=True, count=200)

    def test_other_terminal_outcomes_and_owner_notices_remain_in_inbox(self):
        target, target_source = self.gw.owned_bot("receipt-events")
        sender, source = self.gw.owned_bot("receipt-sender")
        self.assertEqual(201, self.gw.follow(source, target)[0])
        self.assertEqual(201, self.gw.follow(target_source, sender)[0])
        worker, _ = self.gw.native(target)
        cid = self.gw.conversation(target, source)
        notice = self.gw.ok("POST", "/v1/owner/conversations/%s/messages" % cid,
                            {"op_key": "owner-notice", "text": "An owner update"}, self.gw.owner_token)
        expected = {notice["message_id"]: "delivered"}
        for outcome in ("declined", "failed"):
            mid = self.gw.chat(cid, source, "a request that will be %s" % outcome)
            status, job = self.gw.claim_job(worker, mid)
            self.assertEqual(200, status, job)
            status, done = self.gw.reply_job(worker, job, "", outcome=outcome, code="no_answer")
            self.assertEqual((200, outcome), (status, done["state"]))
            expected[mid] = outcome
        expired = self.gw.chat(cid, source, "a request that will expire")
        self.gw.sql("UPDATE messages SET deadline_ts=? WHERE id=?", (C.now_ts() - 5, expired))
        self.gw.ok("GET", "/v1/worker/jobs", None, worker)
        expected[expired] = "expired"
        cancelled = self.gw.chat(cid, source, "a request that will be cancelled")
        self.assertEqual(200, self.gw.contact(target, sender, "mute")[0])
        expected[cancelled] = "cancelled"
        inbox = self.gw.ok("GET", "/v1/inbox", None, source)["items"]
        self.assertEqual(expected, {m["message_id"]: m["state"] for m in inbox})
        resultless = {m["message_id"] for m in inbox if "result" not in m}
        self.assertTrue({expired, cancelled, notice["message_id"]}.issubset(resultless))


if __name__ == "__main__":
    unittest.main()
