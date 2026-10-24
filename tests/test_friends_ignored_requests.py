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
        self.assertEqual([second], [j["job_id"] for j in listed])
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
        self.assertEqual([], self.gw.ok("GET", "/v1/worker/jobs", None, worker)["jobs"])
        status, result = self.gw.call("POST", "/v1/connector/messages/%s/wake" % mid,
                                      {"step": "intent", "wake_id": C.new_token("ticket")[4:],
                                       "phase": "review"}, connector)
        self.assertEqual((409, "not_due"), (status, result["error"]))
        self.assertEqual(200, self.gw.contact(bot, actor, "trust")[0])
        self.assertEqual([{"job_id": mid, "phase": "ready", "wake_attempts": 0}], jobs())
        listed = self.gw.ok("GET", "/v1/worker/jobs", None, worker)["jobs"]
        self.assertEqual([mid], [j["job_id"] for j in listed])
        self.assertTrue(listed[0]["claimable"])
        status, job = self.gw.claim_job(worker, mid)
        self.assertEqual(200, status, job)
        self.assertEqual("IGNORED-WAKE-SECRET", job["untrusted_content"]["text"])

    def test_an_old_clients_notice_is_a_request_that_can_be_ignored_and_admitted_later(self):
        bot, _, caller, worker, _, cid = self.request_sender()
        status, sent = self.gw.send(cid, caller, kind="notice", text="IGNORED-NOTICE-SECRET")
        self.assertEqual((202, "queued", False), (status, sent["state"], sent["reply_required"]), sent)
        mid = sent["message_id"]
        request = next(r for r in self.gw.reviews(bot)["inbound"] if r["job_id"] == mid)
        before = self.gw.ok("GET", "/v1/messages/%s" % mid, None, caller)
        self.assertEqual("chat", before["kind"], "since 0.7.14 a notice is a direct message")
        for duplicate in (False, True):
            status, result = self.ignore(mid)
            self.assertEqual((200, "ignored", duplicate),
                             (status, result.get("inbound"), result.get("duplicate")), result)
        self.assertEqual([], self.gw.reviews(bot)["inbound"])
        self.assertEqual([], self.gw.ok("GET", "/v1/worker/jobs", None, worker)["jobs"])
        status, blocked = self.gw.claim_job(worker, mid)
        self.assertEqual((409, "review_required"), (status, blocked["error"]))
        self.assertEqual(before, self.gw.ok("GET", "/v1/messages/%s" % mid, None, caller))
        self.gw.ok("POST", "/v1/owner/messages/%s/review" % mid,
                   {"action": "approve_inbound", "request_hash": request["request_hash"]}, self.gw.owner_token)
        self.assertEqual([mid], [j["job_id"] for j in self.gw.ok("GET", "/v1/worker/jobs", None, worker)["jobs"]])
        status, job = self.gw.claim_job(worker, mid)
        self.assertEqual(200, status, job)
        self.assertEqual("IGNORED-NOTICE-SECRET", job["untrusted_content"]["text"])
        legacy = self.gw.ok("GET", "/v1/worker/notices", None, worker)
        self.assertEqual(([], 0), (legacy["notices"], legacy["waiting_for_owner_review"]), "the old inbox stays empty")


class IgnoredPendingCapacityTests(unittest.TestCase):
    def gateway(self, **limits):
        tmp = tempfile.TemporaryDirectory(prefix="yobiru ignored capacity ")
        self.addCleanup(tmp.cleanup)
        self.gw = FriendsGateway(Path(tmp.name), limits=limits).start()
        self.addCleanup(self.gw.stop)
        return self.gw

    def sender(self, bot, trusted=False):
        actor, caller = self.gw.actor()
        cid = self.gw.conversation(bot, caller)
        if trusted:
            self.assertEqual(200, self.gw.contact(bot, actor, "trust")[0])
        return actor, caller, cid

    def ignore(self, mid):
        self.gw.ok("POST", "/v1/owner/messages/%s/review" % mid, {"action": "ignore"}, self.gw.owner_token)

    def assert_send_error(self, cid, caller, expected):
        status, result = self.gw.send(cid, caller, kind="chat", text="another request")
        self.assertEqual(expected, (status, result.get("error")), result)

    def test_ignoring_full_contact_queue_allows_more_requests_silently(self):
        gw = self.gateway(pending_per_contact=2)
        bot = gw.bot("ignored-contact-capacity")
        _, caller, cid = self.sender(bot)
        mids = [gw.chat(cid, caller, "request %d" % i) for i in range(2)]
        before = [gw.ok("GET", "/v1/messages/%s" % mid, None, caller) for mid in mids]
        self.assert_send_error(cid, caller, (429, "too_many_pending"))
        for mid in mids:
            self.ignore(mid)
        self.assertEqual([], gw.reviews(bot)["inbound"])
        self.assertEqual(before, [gw.ok("GET", "/v1/messages/%s" % mid, None, caller) for mid in mids])
        for i in range(2):
            gw.chat(cid, caller, "new request %d" % i)
        self.assert_send_error(cid, caller, (429, "too_many_pending"))
        self.assertEqual(4, len(gw.owner_view(cid)["messages"]))

    def test_requests_never_fill_the_inbox_and_ignoring_frees_their_capacity(self):
        gw = self.gateway(pending_per_bot=2)
        bot = gw.bot("ignored-bot-capacity")
        _, trusted, trusted_cid = self.sender(bot, trusted=True)
        mids = []
        for i in range(2):
            _, caller, cid = self.sender(bot)
            mids.append(gw.chat(cid, caller, "request %d" % i))
        _, stranger, stranger_cid = self.sender(bot)
        self.assert_send_error(stranger_cid, stranger, (503, "mailbox_full"))
        for i in range(2):                    # since 0.7.14 requests and the inbox are separate pools
            gw.chat(trusted_cid, trusted, "inbox message %d" % i)
        self.assert_send_error(trusted_cid, trusted, (503, "mailbox_full"))
        for mid in mids:
            self.ignore(mid)
        self.assertEqual([], gw.reviews(bot)["inbound"])
        gw.chat(stranger_cid, stranger, "a request after the owner ignored the others")
        self.assert_send_error(trusted_cid, trusted, (503, "mailbox_full"))

    def test_requests_never_fill_the_gateway_for_inboxes_and_ignoring_frees_their_capacity(self):
        gw = self.gateway(pending_total=2)
        mids = []
        for i in range(2):
            bot = gw.bot("ignored-total-%d" % i)
            _, caller, cid = self.sender(bot)
            mids.append(gw.chat(cid, caller, "request"))
        recipient = gw.bot("ignored-total-recipient")
        _, stranger, stranger_cid = self.sender(recipient)
        self.assert_send_error(stranger_cid, stranger, (503, "mailbox_full"))
        _, caller, cid = self.sender(recipient, trusted=True)
        for i in range(2):
            gw.chat(cid, caller, "inbox message %d" % i)
        self.assert_send_error(cid, caller, (503, "mailbox_full"))
        for mid in mids:
            self.ignore(mid)
        gw.chat(stranger_cid, stranger, "a request after the owners ignored the others")

    def test_ignored_requests_still_count_toward_stored_message_limit(self):
        gw = self.gateway(pending_per_contact=2, max_messages=2)
        bot = gw.bot("ignored-storage-capacity")
        _, caller, cid = self.sender(bot)
        key = C.new_token("ticket")[4:24]
        first = gw.chat(cid, caller, "retained request", op_key=key)
        second = gw.chat(cid, caller, "another retained request")
        for mid in (first, second):
            self.ignore(mid)
        self.assert_send_error(cid, caller, (503, "capacity"))
        self.assertEqual(2, len(gw.owner_view(cid)["messages"]))
        status, retry = gw.send(cid, caller, kind="chat", text="retained request", op_key=key)
        self.assertEqual((200, first, True), (status, retry.get("message_id"), retry.get("duplicate")))

    def test_accumulated_ignored_requests_do_not_crowd_the_binding_job_list(self):
        gw = self.gateway(pending_per_bot=2, pending_per_contact=2)
        bot = gw.bot("ignored-binding-capacity")
        worker, _ = gw.native(bot)
        _, caller, cid = self.sender(bot)
        ignored = gw.chat(cid, caller, "IGNORED-ACCUMULATED-SECRET")
        self.ignore(ignored)
        # Seed an accumulated history without hundreds of HTTP send/ignore round trips.
        # Ignored requests remain stored, so their count can exceed even the default
        # 200-pending-per-bot limit while a new request and inbox job still fit.
        count = 250
        db = gw.db()
        try:
            template = dict(db.execute("SELECT * FROM messages WHERE id=?", (ignored,)).fetchone())
            columns = list(template)
            statement = "INSERT INTO messages (%s) VALUES (%s)" % (
                ",".join(columns), ",".join("?" for _ in columns))
            copies = [dict(template, id=C.new_id("msg"), op_key="c:ignored-%d" % i)
                      for i in range(count - 1)]
            with db:
                db.executemany(statement, ([row[k] for k in columns] for row in copies))
        finally:
            db.close()
        pending = gw.chat(cid, caller, "still waiting for review")
        _, trusted, trusted_cid = self.sender(bot, trusted=True)
        ready = gw.chat(trusted_cid, trusted, "please answer this")
        listed = gw.ok("GET", "/v1/worker/jobs", None, worker)
        self.assertEqual([pending, ready], [j["job_id"] for j in listed["jobs"]])
        self.assertEqual([False, True], [j["claimable"] for j in listed["jobs"]])
        self.assertNotIn("IGNORED-ACCUMULATED-SECRET", json.dumps(listed))
        self.assertEqual(count, gw.sql("SELECT COUNT(*) AS n FROM messages WHERE bot_id=? AND review_in='ignored'",
                                       (bot,))[0]["n"])
        status, job = gw.claim_job(worker, ready)
        self.assertEqual(200, status, job)
        self.assertEqual("please answer this", job["untrusted_content"]["text"])

    def test_ignored_attachments_still_count_toward_retained_byte_limit(self):
        gw = self.gateway(pending_per_contact=1, attachment_retained_bytes_per_caller=6)
        bot = gw.bot("ignored-attachment-capacity", policy={
            "offered": ["chat", "files"], "visitor": ["chat", "files"],
            "follower": ["chat", "files"], "friend": ["chat", "files"]})
        _, caller, cid = self.sender(bot)
        files = [{"name": "request.txt", "content": "abcdef"}]
        status, sent = gw.send(cid, caller, kind="chat", text="retained file", attachments=files)
        self.assertEqual(202, status, sent)
        self.ignore(sent["message_id"])
        status, result = gw.send(cid, caller, kind="chat", text="another file", attachments=files)
        self.assertEqual((429, "attachment_quota", "retained"),
                         (status, result.get("error"), result.get("scope")), result)
        gw.chat(cid, caller, "text without an attachment still fits")

    def test_accepting_an_ignored_sender_counts_its_pending_work_again(self):
        gw = self.gateway(pending_per_contact=1)
        bot = gw.bot("ignored-accepted-capacity")
        actor, caller, cid = self.sender(bot)
        mid = gw.chat(cid, caller, "request later accepted")
        self.ignore(mid)
        self.assertEqual(200, gw.contact(bot, actor, "trust")[0])
        worker, _ = gw.native(bot)
        jobs = gw.ok("GET", "/v1/worker/jobs", None, worker)["jobs"]
        self.assertTrue(next(job for job in jobs if job["job_id"] == mid)["claimable"])
        self.assert_send_error(cid, caller, (429, "too_many_pending"))

    def test_restored_ignored_backlog_wakes_past_accepted_and_uncertain_attempts(self):
        gw = self.gateway()
        bot = gw.bot("ignored-wake-backlog")
        actor, caller, first_cid = self.sender(bot)
        worker, wid = gw.native(bot)
        connector = gw.ok("POST", "/v1/owner/bots/%s/workers/%s/connector" % (bot, wid), {},
                          gw.owner_token)["token"]
        cids = [first_cid] + [gw.conversation(bot, caller) for _ in range(2)]
        # Retained ignored requests can accumulate beyond the 200-pending limit.
        # Seed 67 per conversation to stay within the default 100-turn limit,
        # avoiding hours of rate-limited sends while preserving the stored state.
        for cid in cids:
            mid = gw.chat(cid, caller, "RESTORED-REQUEST-SECRET")
            self.ignore(mid)
            db = gw.db()
            try:
                template = dict(db.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone())
                columns = list(template)
                statement = "INSERT INTO messages (%s) VALUES (%s)" % (
                    ",".join(columns), ",".join("?" for _ in columns))
                copies = [dict(template, id=C.new_id("msg"), op_key="c:retained-%d" % i)
                          for i in range(66)]
                with db:
                    db.executemany(statement, ([row[k] for k in columns] for row in copies))
                    db.execute("UPDATE conversations SET turns=67 WHERE id=?", (cid,))
            finally:
                db.close()
        mids = [row["id"] for row in gw.sql("SELECT id FROM messages WHERE bot_id=? "
                                           "ORDER BY created_ts, rowid", (bot,))]
        self.assertEqual(201, len(mids))
        listed = lambda: gw.ok("GET", "/v1/connector/jobs", None, connector)["jobs"]
        self.assertEqual([], listed())
        self.assertEqual(200, gw.contact(bot, actor, "trust")[0])
        jobs = gw.ok("GET", "/v1/worker/jobs", None, worker)["jobs"]
        self.assertEqual(201, sum(job["claimable"] for job in jobs))
        self.assertEqual(mids[:200], [job["job_id"] for job in listed()])
        for i, mid in enumerate(mids[:200]):
            path = "/v1/connector/messages/%s/wake" % mid
            wake_id = "restored-wake-%03d" % i
            gw.ok("POST", path, {"step": "intent", "wake_id": wake_id, "phase": "ready"}, connector)
            gw.ok("POST", path, {"step": "result", "wake_id": wake_id,
                                  "outcome": "accepted" if i % 2 else "uncertain"}, connector)
        # Earlier accepted/uncertain wakes stay queued without being retried, but
        # must not hide the last request's first wake behind the response limit.
        expected = [{"job_id": mids[-1], "phase": "ready", "wake_attempts": 0}]
        self.assertEqual(expected, listed())
        self.assertEqual(expected, listed())
        status, job = gw.claim_job(worker, mids[-1])
        self.assertEqual(200, status, job)
        self.assertEqual("RESTORED-REQUEST-SECRET", job["untrusted_content"]["text"])
        self.assertEqual([], listed())

    def test_admitting_an_ignored_request_counts_toward_global_capacity_again(self):
        gw = self.gateway(pending_total=1)
        bot = gw.bot("ignored-admitted-capacity")
        _, caller, cid = self.sender(bot)
        mid = gw.chat(cid, caller, "request later admitted")
        request = gw.reviews(bot)["inbound"][0]
        self.ignore(mid)
        gw.ok("POST", "/v1/owner/messages/%s/review" % mid,
              {"action": "approve_inbound", "request_hash": request["request_hash"]}, gw.owner_token)
        worker, _ = gw.native(bot)
        jobs = gw.ok("GET", "/v1/worker/jobs", None, worker)["jobs"]
        self.assertEqual([mid], [j["job_id"] for j in jobs])
        self.assertTrue(jobs[0]["claimable"])
        self.assertEqual(200, gw.claim_job(worker, mid)[0])
        other = gw.bot("ignored-admitted-other")
        _, other_caller, other_cid = self.sender(other, trusted=True)
        self.assert_send_error(other_cid, other_caller, (503, "mailbox_full"))


if __name__ == "__main__":
    unittest.main()
