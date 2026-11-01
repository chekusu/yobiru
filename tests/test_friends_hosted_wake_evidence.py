"""Hosted wake evidence uses the current native binding's activity before the intent.

The gateway and webhook run on loopback with synthetic identities. Historical binding activity is
set explicitly so the quiet interval is checked without a long sleep or changing the gateway clock.
"""
import hashlib
import tempfile
import threading
import time
import unittest
from pathlib import Path

from support.public_harness import C, FriendsGateway, wait_until
from test_friends_hosted_wake import LIMITS, Receiver, SECRET

import bot_gateway as G


class HostedWakeEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="yobiru hosted evidence test ")
        cls.gw = FriendsGateway(Path(cls._tmp.name), limits=dict(
            LIMITS, quiet_wake_seconds=300, hosted_wake_timeout_seconds=10)).start()

    @classmethod
    def tearDownClass(cls):
        cls.gw.stop()
        cls._tmp.cleanup()

    def setUp(self):
        self.receiver = Receiver()
        self.bot = self.gw.bot("e" + hashlib.sha256(self.id().encode()).hexdigest()[:12])
        self.gw.policy(self.bot, offered=["faq", "chat"], visitor=["faq", "chat"])
        actor, token = self.gw.actor()
        cid = self.gw.conversation(self.bot, token)
        self.assertEqual(200, self.gw.contact(self.bot, actor, "trust")[0])
        self.mid = self.gw.chat(cid, token, "synthetic evidence request")

    def tearDown(self):
        if hasattr(self, "release_response"):
            self.release_response.set()
        self.receiver.stop()

    def pending_wake(self, status=202):
        """Hold the real transport response so a successful claim must precede its recorded result."""
        entered, self.release_response = threading.Event(), threading.Event()
        original = self.receiver.server.RequestHandlerClass.do_POST
        self.receiver.status = status

        def blocked(handler):
            entered.set()
            self.release_response.wait(8)
            original(handler)
        self.receiver.server.RequestHandlerClass.do_POST = blocked
        self.gw.ok("PUT", "/v1/owner/bots/%s/wake" % self.bot,
                   {"url": self.receiver.url, "secret": SECRET}, self.gw.owner_token)
        self.assertTrue(entered.wait(5), "hosted delivery did not start")
        row = self.gw.sql("SELECT * FROM messages WHERE id=?", (self.mid,))[0]
        self.assertEqual("intent", row["wake_state"])
        return row

    def finish_wake(self, expected="accepted"):
        self.release_response.set()
        self.assertTrue(wait_until(lambda: self.gw.sql(
            "SELECT wake_state FROM messages WHERE id=?", (self.mid,))[0]["wake_state"] == expected, timeout=5),
            "hosted wake result was not recorded")

    def quiet_native(self):
        token, wid = self.gw.native(self.bot)
        seen = int(C.now_ts()) - 301
        self.gw.sql("UPDATE workers SET created_ts=?, last_seen_ts=? WHERE id=?", (seen - 300, seen, wid))
        return token, wid

    def delivery(self, wid):
        workers = self.gw.ok("GET", "/v1/owner/bots/%s/workers" % self.bot, None,
                             self.gw.owner_token)["workers"]
        return next(w["delivery"] for w in workers if w["worker_id"] == wid)

    def record_outcome(self, wake_id, outcome, now=None):
        store = G.Store(self.gw.home, G.load_config(self.gw.home))
        route = self.gw.sql("SELECT * FROM hosted_wakes WHERE bot_id=?", (self.bot,))[0]
        try:
            G.hosted_wake_outcome(store, route, wake_id, outcome, "synthetic delivery result",
                                  C.now_ts() if now is None else now)
        finally:
            G.close_all(store._idle)

    def wake(self):
        self.gw.ok("PUT", "/v1/owner/bots/%s/wake" % self.bot,
                   {"url": self.receiver.url, "secret": SECRET}, self.gw.owner_token)
        self.assertEqual(1, len(self.receiver.wait(1)))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            row = self.gw.sql("SELECT wake_state, wake_intent_ts, wake_seen_before_ts FROM messages WHERE id=?",
                              (self.mid,))[0]
            if row["wake_state"] == "accepted":
                return row
            time.sleep(0.05)
        self.fail("hosted wake result was not recorded")

    def claim_and_delivery(self, token, wid):
        status, job = self.gw.claim_job(token, self.mid)
        self.assertEqual(200, status, job)
        return self.delivery(wid)

    def test_claim_before_accepted_result_gains_quiet_evidence(self):
        token, wid = self.quiet_native()
        self.pending_wake()
        report = self.claim_and_delivery(token, wid)
        self.assertEqual(("none", "none"), (report["status"]["wake_route"], report["status"]["quiet_wake"]))
        claimed = self.gw.sql("SELECT started_ts FROM messages WHERE id=?", (self.mid,))[0]["started_ts"]
        self.finish_wake()
        report = self.delivery(wid)
        self.assertEqual(("recent", "recent"), (report["status"]["wake_route"], report["status"]["quiet_wake"]))
        self.assertEqual("hosted", report["evidence"]["quiet_wake_route"])
        self.assertEqual(C.iso(claimed), report["evidence"]["wake_tested_at"])
        self.assertEqual([], self.gw.sql("SELECT * FROM hosted_wake_claims WHERE message_id=?", (self.mid,)))
        card = self.gw.ok("GET", "/v1/bots/%s" % self.bot)
        self.assertEqual("recent", card["chat"]["wake_evidence"])

    def test_claim_before_uncertain_result_still_gains_evidence(self):
        token, wid = self.quiet_native()
        self.pending_wake(status=500)
        self.claim_and_delivery(token, wid)
        self.finish_wake(expected="uncertain")
        report = self.delivery(wid)
        self.assertEqual(("recent", "recent"), (report["status"]["wake_route"], report["status"]["quiet_wake"]))

    def test_claim_before_result_preserves_an_active_baseline(self):
        token, wid = self.gw.native(self.bot)
        self.gw.ok("GET", "/v1/worker/jobs", None, token)
        self.pending_wake()
        self.claim_and_delivery(token, wid)
        self.finish_wake()
        report = self.delivery(wid)
        self.assertEqual(("recent", "none"), (report["status"]["wake_route"], report["status"]["quiet_wake"]))

    def claim_and_finish_before_result(self, outcome):
        token, wid = self.quiet_native()
        self.pending_wake()
        status, job = self.gw.claim_job(token, self.mid)
        self.assertEqual(200, status, job)
        if outcome == "read":
            self.gw.ok("POST", "/v1/worker/messages/%s/finish" % self.mid,
                       {"lease_id": job["lease"]["lease_id"], "fence": job["lease"]["fence"], "outcome": "read"},
                       token)
        else:
            status, finished = self.gw.reply_job(token, job, "synthetic prompt answer")
            self.assertEqual((200, "completed"), (status, finished["state"]))
        row = self.gw.sql("SELECT state, lease_worker, lease_epoch FROM messages WHERE id=?", (self.mid,))[0]
        self.assertEqual({"state": "completed", "lease_worker": None, "lease_epoch": None}, row)
        self.finish_wake()
        report = self.delivery(wid)
        self.assertEqual(("recent", "recent"), (report["status"]["wake_route"], report["status"]["quiet_wake"]))

    def test_read_before_result_preserves_claim_evidence(self):
        self.claim_and_finish_before_result("read")

    def test_reply_before_result_preserves_claim_evidence(self):
        self.claim_and_finish_before_result("completed")

    def test_refused_result_does_not_correlate_an_earlier_claim(self):
        token, wid = self.quiet_native()
        wake = self.pending_wake()
        self.claim_and_delivery(token, wid)
        # HTTP errors are uncertain, because the request reached the host. A pre-delivery refusal is
        # supplied directly while the real response remains held.
        self.record_outcome(wake["wake_id"], "refused")
        self.finish_wake(expected="refused")
        report = self.delivery(wid)
        self.assertEqual(("none", "none"), (report["status"]["wake_route"], report["status"]["quiet_wake"]))
        self.assertEqual([], self.gw.sql("SELECT * FROM hosted_wake_claims WHERE message_id=?", (self.mid,)))

    def test_accepted_result_without_a_claim_is_not_evidence(self):
        _, wid = self.quiet_native()
        self.wake()
        report = self.delivery(wid)
        self.assertEqual(("none", "none"), (report["status"]["wake_route"], report["status"]["quiet_wake"]))

    def test_binding_replaced_before_result_does_not_inherit_evidence(self):
        token, old_wid = self.quiet_native()
        self.pending_wake()
        self.claim_and_delivery(token, old_wid)
        _, wid = self.gw.native(self.bot, replace=True)
        self.finish_wake()
        for worker in (old_wid, wid):
            report = self.delivery(worker)
            self.assertIsNone(report["evidence"]["wake_tested_at"])
            self.assertIsNone(report["evidence"]["quiet_wake_correlated_at"])

    def test_retry_before_result_fences_the_old_claim(self):
        token, wid = self.quiet_native()
        wake = self.pending_wake()
        route = self.gw.sql("SELECT * FROM hosted_wakes WHERE bot_id=?", (self.bot,))[0]
        self.claim_and_delivery(token, wid)
        self.gw.ok("DELETE", "/v1/owner/bots/%s/wake" % self.bot, None, self.gw.owner_token)
        token, wid = self.gw.native(self.bot, replace=True)
        self.assertEqual(200, self.gw.resolve(self.mid, "retry")[0])
        self.claim_and_delivery(token, wid)
        # Apply the old callback before releasing the held transport response, so this assertion does
        # not depend on when maintenance next collects the actual response.
        store = G.Store(self.gw.home, G.load_config(self.gw.home))
        try:
            G.hosted_wake_outcome(store, route, wake["wake_id"], "accepted", None, C.now_ts())
        finally:
            G.close_all(store._idle)
        report = self.delivery(wid)
        self.assertEqual(("none", "none"), (report["status"]["wake_route"], report["status"]["quiet_wake"]))
        self.assertEqual("none", self.gw.sql("SELECT wake_state FROM messages WHERE id=?", (self.mid,))[0]["wake_state"])

    def test_obsolete_and_duplicate_results_do_not_create_or_refresh_evidence(self):
        token, wid = self.quiet_native()
        wake = self.pending_wake()
        self.claim_and_delivery(token, wid)
        self.record_outcome("hosted-obsolete-synthetic-id", "accepted")
        self.assertIsNone(self.delivery(wid)["evidence"]["wake_tested_at"])
        self.assertEqual("intent", self.gw.sql("SELECT wake_state FROM messages WHERE id=?", (self.mid,))[0]["wake_state"])
        self.finish_wake()
        evidence = self.delivery(wid)["evidence"]
        self.record_outcome(wake["wake_id"], "accepted", now=C.now_ts() + 60)
        self.assertEqual(evidence, self.delivery(wid)["evidence"])

    def test_deleting_a_message_removes_its_pending_claim(self):
        token, wid = self.quiet_native()
        self.pending_wake()
        self.claim_and_delivery(token, wid)
        self.assertEqual(1, len(self.gw.sql("SELECT * FROM hosted_wake_claims WHERE message_id=?", (self.mid,))))
        # Retention and conversation deletion both remove messages with SQL DELETE.
        self.gw.sql("DELETE FROM messages WHERE id=?", (self.mid,))
        self.assertEqual([], self.gw.sql("SELECT * FROM hosted_wake_claims WHERE message_id=?", (self.mid,)))
        self.assertIsNone(self.delivery(wid)["evidence"]["wake_tested_at"])

    def test_active_binding_does_not_gain_quiet_evidence(self):
        token, wid = self.gw.native(self.bot)
        self.gw.ok("GET", "/v1/worker/jobs", None, token)
        last_seen = self.gw.sql("SELECT last_seen_ts FROM workers WHERE id=?", (wid,))[0]["last_seen_ts"]
        wake = self.wake()
        report = self.claim_and_delivery(token, wid)
        self.assertEqual(("recent", "none"), (report["status"]["wake_route"], report["status"]["quiet_wake"]))
        self.assertEqual(last_seen, wake["wake_seen_before_ts"])

    def test_quiet_current_binding_still_gains_quiet_evidence(self):
        token, wid = self.gw.native(self.bot)
        last_seen = int(C.now_ts()) - 301
        self.gw.sql("UPDATE workers SET created_ts=?, last_seen_ts=? WHERE id=?", (last_seen - 300, last_seen, wid))
        wake = self.wake()
        report = self.claim_and_delivery(token, wid)
        self.assertEqual(("recent", "recent"), (report["status"]["wake_route"], report["status"]["quiet_wake"]))
        self.assertEqual("hosted", report["evidence"]["quiet_wake_route"])
        self.assertEqual(last_seen, wake["wake_seen_before_ts"])

    def test_new_unused_binding_has_no_observed_quiet_interval(self):
        token, wid = self.gw.native(self.bot)
        created = self.gw.sql("SELECT created_ts FROM workers WHERE id=?", (wid,))[0]["created_ts"]
        wake = self.wake()
        report = self.claim_and_delivery(token, wid)
        self.assertEqual("none", report["status"]["quiet_wake"])
        self.assertEqual(created, wake["wake_seen_before_ts"])

    def test_binding_created_after_the_wake_has_no_quiet_baseline(self):
        wake = self.wake()
        token, wid = self.gw.native(self.bot)
        report = self.claim_and_delivery(token, wid)
        self.assertEqual(("recent", "none"), (report["status"]["wake_route"], report["status"]["quiet_wake"]))
        self.assertEqual(wake["wake_intent_ts"], wake["wake_seen_before_ts"])

    def test_replaced_binding_does_not_supply_an_old_quiet_baseline(self):
        _, old_wid = self.gw.native(self.bot)
        old_seen = int(C.now_ts()) - 301
        self.gw.sql("UPDATE workers SET created_ts=?, last_seen_ts=? WHERE id=?", (old_seen - 300, old_seen, old_wid))
        token, wid = self.gw.native(self.bot, replace=True)
        self.gw.ok("GET", "/v1/worker/jobs", None, token)
        last_seen = self.gw.sql("SELECT last_seen_ts FROM workers WHERE id=?", (wid,))[0]["last_seen_ts"]
        wake = self.wake()
        report = self.claim_and_delivery(token, wid)
        self.assertEqual("none", report["status"]["quiet_wake"])
        self.assertEqual(last_seen, wake["wake_seen_before_ts"])

    def test_replacing_a_binding_after_the_wake_discards_its_evidence(self):
        _, old_wid = self.gw.native(self.bot)
        old_seen = int(C.now_ts()) - 301
        self.gw.sql("UPDATE workers SET created_ts=?, last_seen_ts=? WHERE id=?", (old_seen - 300, old_seen, old_wid))
        wake = self.wake()
        self.assertGreaterEqual(wake["wake_intent_ts"] - wake["wake_seen_before_ts"], 300)

        # Keep a new registration from racing the replacement's claim. The completed wake remains in
        # the message ledger until replacement resets it, regardless of whether a route is registered.
        self.gw.ok("DELETE", "/v1/owner/bots/%s/wake" % self.bot, None, self.gw.owner_token)
        token, wid = self.gw.native(self.bot, replace=True)
        row = self.gw.sql("SELECT wake_state, wake_phase FROM messages WHERE id=?", (self.mid,))[0]
        self.assertEqual(("none", None), (row["wake_state"], row["wake_phase"]))
        report = self.claim_and_delivery(token, wid)
        self.assertEqual(("none", "none"), (report["status"]["wake_route"], report["status"]["quiet_wake"]))


if __name__ == "__main__":
    unittest.main()
