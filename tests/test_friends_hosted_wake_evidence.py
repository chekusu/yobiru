"""Hosted wake evidence uses the current native binding's activity before the intent.

The gateway and webhook run on loopback with synthetic identities. Historical binding activity is
set explicitly so the quiet interval is checked without a long sleep or changing the gateway clock.
"""
import hashlib
import tempfile
import time
import unittest
from pathlib import Path

from support.public_harness import C, FriendsGateway
from test_friends_hosted_wake import LIMITS, Receiver, SECRET


class HostedWakeEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="yobiru hosted evidence test ")
        cls.gw = FriendsGateway(Path(cls._tmp.name), limits=dict(LIMITS, quiet_wake_seconds=300)).start()

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
        self.receiver.stop()

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
        workers = self.gw.ok("GET", "/v1/owner/bots/%s/workers" % self.bot, None,
                             self.gw.owner_token)["workers"]
        return next(w["delivery"] for w in workers if w["worker_id"] == wid)

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
