"""An inbox upgrade must preserve inbound-only trust and already claimed reply-review boundaries.

The core authorization tests need no Git history. Additional upgrade tests create real databases and leases
with the exact public PR base, then restart the same gateway home with the current implementation. All identities,
messages and homes are synthetic and local to the test.
"""
import subprocess
import sys
import tempfile
import unittest
from support.public_harness import legacy_gateway_prefix
from pathlib import Path

from support.public_harness import C, FriendsGateway, PUBLIC

import bot_gateway as G


BASE_COMMIT = "0154a9375d9b7a2d3da70b8ebf074eeb12cf5cb1"


class InboxAuthorizationTests(unittest.TestCase):
    """Run in shallow checkouts and source archives, without the historical gateway fixture."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="yobiru inbox authorization ")
        self.addCleanup(self.tmp.cleanup)
        self.gw = FriendsGateway(Path(self.tmp.name)).start()
        self.addCleanup(self.gw.stop)
        self.bot = self.gw.bot("inbox-authorization")
        self.worker, _ = self.gw.native(self.bot)
        self.actor, self.caller = self.gw.actor()
        self.cid = self.gw.conversation(self.bot, self.caller)

    def accept(self):
        status, accepted = self.gw.contact(self.bot, self.actor, "trust")
        self.assertEqual(200, status, accepted)
        self.assertEqual({"reply": "direct"}, accepted["decision"]["scope"])
        return accepted["decision"]

    def new_job(self, policy, admit=False, new_conversation=False):
        cid = self.gw.conversation(self.bot, self.caller) if new_conversation else self.cid
        mid = self.gw.chat(cid, self.caller, "Synthetic authorization request")
        if admit:
            self.assertEqual(200, self.gw.admit(self.bot, mid)[0])
        status, job = self.gw.claim_job(self.worker, mid)
        self.assertEqual(200, status, job)
        self.assertEqual(policy, job["review"]["reply"])
        return job

    def read(self, job):
        return self.gw.call("POST", "/v1/worker/messages/%s/finish" % job["job_id"],
                            {"lease_id": job["lease"]["lease_id"], "fence": job["lease"]["fence"],
                             "outcome": "read"}, self.worker)

    def finish(self, job, state):
        status, finished = self.gw.reply_job(self.worker, job, "Synthetic authorization answer")
        self.assertEqual((200, state), (status, finished["state"]), finished)
        caller = self.gw.ok("GET", "/v1/messages/%s" % job["job_id"], None, self.caller)
        if state == "review":
            self.assertNotIn("result", caller)
            self.assertIn(job["job_id"], [m["job_id"] for m in self.gw.reviews(self.bot)["replies"]])
        else:
            self.assertEqual("Synthetic authorization answer", caller["result"]["text"])

    def assert_read_refused(self, job):
        status, refused = self.read(job)
        self.assertEqual((409, "not_in_inbox"), (status, refused.get("error")), refused)

    def test_reviewed_claims_keep_their_boundary_after_acceptance_and_restart(self):
        reviewed = self.new_job("owner_approval_required", admit=True)
        expiring = self.new_job("owner_approval_required", admit=True, new_conversation=True)
        self.accept()
        self.gw.restart()
        self.assert_read_refused(reviewed)
        self.finish(reviewed, "review")
        self.gw.sql("UPDATE messages SET lease_expires=? WHERE id=?", (C.now_ts() - 5, expiring["job_id"]))
        self.gw.ok("GET", "/v1/worker/jobs", None, self.worker)
        self.assertEqual({"state": "uncertain", "code": "lease_expired"}, self.gw.sql(
            "SELECT state, code FROM messages WHERE id=?", (expiring["job_id"],))[0])
        self.assertEqual(409, self.gw.reply_job(self.worker, expiring, "Synthetic stale answer")[0])
        self.finish(self.new_job("direct", new_conversation=True), "completed")

    def test_direct_reply_and_read_require_permissions_that_are_still_active(self):
        self.accept()
        self.finish(self.new_job("direct"), "completed")
        read = self.new_job("direct")
        status, finished = self.read(read)
        self.assertEqual((200, "completed", "read"), (status, finished["state"], finished["code"]))
        revoked = self.new_job("direct")
        self.assertEqual(200, self.gw.contact(self.bot, self.actor, "untrust")[0])
        self.assert_read_refused(revoked)
        self.finish(revoked, "review")
        pending_cid = self.gw.conversation(self.bot, self.caller)
        pending = self.gw.chat(pending_cid, self.caller, "Synthetic request after untrust")
        self.assertIn(pending, [m["job_id"] for m in self.gw.reviews(self.bot)["inbound"]])
        self.accept()
        status, blocked = self.gw.claim_job(self.worker, pending)
        self.assertEqual(200, status, blocked)
        self.assertEqual("direct", blocked["review"]["reply"])
        self.assertEqual(200, self.gw.contact(self.bot, self.actor, "block")[0])
        self.assertEqual(409, self.read(blocked)[0])
        self.assertEqual(409, self.gw.reply_job(self.worker, blocked, "Synthetic blocked answer")[0])
        status, caller = self.gw.call("GET", "/v1/messages/%s" % pending, None, self.caller)
        self.assertEqual(404, status, "a blocked caller cannot retrieve the message")
        self.assertNotIn("result", caller)
        self.assertEqual({"state": "cancelled", "result": None}, self.gw.sql(
            "SELECT state, result FROM messages WHERE id=?", (pending,))[0])

    def test_durable_acceptance_outlives_receipt_retention(self):
        accepted = self.accept()
        self.assertEqual(accepted, self.accept())
        claimed = self.new_job("direct")
        self.gw.stop()
        cfg = G.load_config(self.gw.home)
        cutoff = int(C.now_ts()) - cfg["limits"]["decision_retention_days"] * 86400
        self.gw.sql("UPDATE decisions SET decided_ts=? WHERE id=?", (cutoff - 1, accepted["decision_id"]))
        G.maintenance(G.Store(self.gw.home, cfg))
        self.assertEqual([], self.gw.sql("SELECT * FROM decisions WHERE id=?", (accepted["decision_id"],)))
        self.gw.start()
        self.finish(claimed, "completed")
        self.finish(self.new_job("direct"), "completed")
        renewed = self.accept()
        self.assertNotEqual(accepted["decision_id"], renewed["decision_id"])
        self.assertEqual(renewed, self.accept())

    def test_legacy_preapproval_without_a_receipt_keeps_only_inbound_authority(self):
        # Schema 7 permits preapproved contacts without a retained decision receipt. Reconstruct that durable
        # state without fabricating a new acceptance or depending on a historical Git object.
        self.gw.sql("UPDATE contacts SET preapproved=1, trust_decision=NULL WHERE bot_id=? AND actor_id=?",
                    (self.bot, self.actor))
        self.gw.restart()
        job = self.new_job("owner_approval_required")
        self.assertEqual("preapproved", job["review"]["inbound"])
        self.assertNotIn(job["job_id"], [m["job_id"] for m in self.gw.reviews(self.bot)["inbound"]])
        self.accept()
        self.assert_read_refused(job)
        self.finish(job, "review")
        self.finish(self.new_job("direct", new_conversation=True), "completed")

    def test_a_claim_without_persisted_permission_requires_review(self):
        self.accept()
        job = self.new_job("direct")
        self.gw.stop()
        # An older gateway left started messages with no claim-policy record. Losing that record must never
        # promote a lease to direct merely because the sender is accepted when it finishes.
        self.gw.sql("DELETE FROM job_reply_policies WHERE message_id=?", (job["job_id"],))
        self.gw.start()
        self.assert_read_refused(job)
        self.finish(job, "review")


class InboxUpgradeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.code_tmp = tempfile.TemporaryDirectory(prefix="yobiru inbox base code ")
        cls.addClassCleanup(cls.code_tmp.cleanup)
        cls.legacy = Path(cls.code_tmp.name)
        for name in ("bot_gateway.py", "bot_common.py", "bot_e2e.py", "bot_mcp.py", "bot_blobs.py",
                     "bot_profile.py"):
            try:
                source = subprocess.run(["git", "show", BASE_COMMIT + ":public_bots/" + name],
                                        cwd=PUBLIC.parent, capture_output=True, check=True).stdout
            except (FileNotFoundError, subprocess.CalledProcessError):
                raise unittest.SkipTest("the public PR base commit is unavailable (for example, a source archive)")
            (cls.legacy / name).write_bytes(source)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="yobiru inbox upgrade ")
        self.addCleanup(self.tmp.cleanup)
        self.gw = FriendsGateway(Path(self.tmp.name),
                                 prefix=legacy_gateway_prefix(self.legacy / "bot_gateway.py")).start()
        self.addCleanup(self.gw.stop)
        self.bot = self.gw.bot("upgrade-inbox")
        self.worker, _ = self.gw.native(self.bot)
        self.actor, self.caller = self.gw.actor()
        self.cid = self.gw.conversation(self.bot, self.caller)
        status, trusted = self.gw.contact(self.bot, self.actor, "trust")
        self.assertEqual(200, status, trusted)
        self.legacy_decision = trusted["decision"]
        self.assertIsNone(self.legacy_decision["scope"])

    def upgrade(self):
        self.gw.stop()
        self.gw.prefix = [sys.executable, str(PUBLIC / "bot_gateway.py")]
        migrated = subprocess.run(self.gw.prefix + ["--gateway-home", str(self.gw.home), "migrate"],
                                  capture_output=True, text=True, timeout=60)       # schema 7 -> 8 (0.7.14)
        self.assertEqual(0, migrated.returncode, migrated.stderr)
        self.gw.start()

    def claim(self, mid, reply_policy):
        status, job = self.gw.claim_job(self.worker, mid)
        self.assertEqual(200, status, job)
        self.assertEqual({"inbound": "preapproved", "reply": reply_policy}, job["review"])
        return job

    def message(self, text, cid=None):
        return self.gw.chat(cid or self.cid, self.caller, text)

    def assert_draft(self, job, text):
        status, finished = self.gw.reply_job(self.worker, job, text)
        self.assertEqual((200, "review"), (status, finished["state"]), finished)
        caller_view = self.gw.ok("GET", "/v1/messages/%s" % job["job_id"], None, self.caller)
        self.assertNotIn("result", caller_view, "a reply prepared under review must remain private")
        self.assertIn(job["job_id"], [m["job_id"] for m in self.gw.reviews(self.bot)["replies"]])

    def accept(self):
        status, accepted = self.gw.contact(self.bot, self.actor, "trust")
        self.assertEqual(200, status, accepted)
        self.assertEqual({"reply": "direct"}, accepted["decision"]["scope"])
        self.assertNotEqual(self.legacy_decision["decision_id"], accepted["decision"]["decision_id"])
        return accepted["decision"]

    def read(self, job):
        return self.gw.call("POST", "/v1/worker/messages/%s/finish" % job["job_id"],
                            {"lease_id": job["lease"]["lease_id"], "fence": job["lease"]["fence"],
                             "outcome": "read"}, self.worker)

    def test_live_base_lease_keeps_reply_review_until_owner_approves_its_draft(self):
        mid = self.message("Synthetic request before upgrade")
        job = self.claim(mid, "owner_approval_required")
        before = self.gw.sql("SELECT * FROM decisions WHERE id=?", (self.legacy_decision["decision_id"],))
        self.upgrade()
        self.assertEqual(before, self.gw.sql("SELECT * FROM decisions WHERE id=?",
                                            (self.legacy_decision["decision_id"],)), "old receipts stay immutable")
        self.assert_draft(job, "Synthetic draft prepared under the old review policy")
        self.assertEqual(200, self.gw.approve_reply(self.bot, mid)[0])
        released = self.gw.ok("GET", "/v1/messages/%s" % mid, None, self.caller)
        self.assertEqual("Synthetic draft prepared under the old review policy", released["result"]["text"])

    def test_legacy_trust_still_admits_new_claims_but_does_not_authorize_direct_replies(self):
        mid = self.message("Synthetic queued request before upgrade")
        self.upgrade()
        self.assertNotIn(mid, [m["job_id"] for m in self.gw.reviews(self.bot)["inbound"]])
        job = self.claim(mid, "owner_approval_required")
        self.assert_draft(job, "Synthetic reviewed answer after upgrade")
        other = self.gw.conversation(self.bot, self.caller)
        fresh = self.claim(self.message("Synthetic new request after upgrade", other), "owner_approval_required")
        self.assert_draft(fresh, "Synthetic new reviewed answer")

    def test_explicit_acceptance_enables_future_claims_but_preserves_an_existing_base_lease(self):
        job = self.claim(self.message("Synthetic request claimed on the base"), "owner_approval_required")
        self.upgrade()
        reviewed_cid = self.gw.conversation(self.bot, self.caller)
        reviewed = self.claim(self.message("Synthetic request claimed before acceptance", reviewed_cid),
                              "owner_approval_required")
        accepted = self.accept()
        self.assertEqual(accepted, self.accept(), "repeated explicit acceptance is idempotent")
        self.gw.restart()
        self.assert_draft(job, "Synthetic old lease draft still requires approval")
        self.assert_draft(reviewed, "Synthetic reviewed lease still requires approval after acceptance")
        other = self.gw.conversation(self.bot, self.caller)
        fresh = self.claim(self.message("Synthetic accepted sender's next request", other), "direct")
        status, finished = self.gw.reply_job(self.worker, fresh, "Synthetic direct answer")
        self.assertEqual((200, "completed"), (status, finished["state"]), finished)
        self.assertEqual("Synthetic direct answer", self.gw.ok("GET", "/v1/messages/%s" % fresh["job_id"],
                                                              None, self.caller)["result"]["text"])

    def test_claimed_review_boundary_survives_later_acceptance_for_read_and_expiry(self):
        old_job = self.claim(self.message("Synthetic base lease to expire"), "owner_approval_required")
        self.upgrade()
        other = self.gw.conversation(self.bot, self.caller)
        reviewed = self.claim(self.message("Synthetic new lease before acceptance", other),
                              "owner_approval_required")
        self.accept()
        for job in (old_job, reviewed):
            with self.subTest(message=job["job_id"]):
                status, refused = self.read(job)
                self.assertEqual((409, "not_in_inbox"), (status, refused.get("error")), refused)
                self.gw.sql("UPDATE messages SET lease_expires=? WHERE id=?", (C.now_ts() - 5, job["job_id"]))
        direct_cid = self.gw.conversation(self.bot, self.caller)
        direct = self.claim(self.message("Synthetic inbox lease after acceptance", direct_cid), "direct")
        self.gw.sql("UPDATE messages SET lease_expires=? WHERE id=?", (C.now_ts() - 5, direct["job_id"]))
        self.gw.ok("GET", "/v1/worker/jobs", None, self.worker)
        for job in (old_job, reviewed):
            self.assertEqual({"state": "uncertain", "code": "lease_expired"}, self.gw.sql(
                "SELECT state, code FROM messages WHERE id=?", (job["job_id"],))[0])
            self.assertEqual(409, self.gw.reply_job(self.worker, job, "Synthetic stale answer")[0])
        self.assertEqual({"state": "completed", "code": "read"}, self.gw.sql(
            "SELECT state, code FROM messages WHERE id=?", (direct["job_id"],))[0])

    def test_acceptance_survives_receipt_retention_and_can_be_renewed_and_withdrawn(self):
        self.upgrade()
        accepted = self.accept()
        self.gw.stop()
        cfg = G.load_config(self.gw.home)
        cutoff = int(C.now_ts()) - cfg["limits"]["decision_retention_days"] * 86400
        self.gw.sql("UPDATE decisions SET decided_ts=? WHERE id=?", (cutoff - 1, accepted["decision_id"]))
        G.maintenance(G.Store(self.gw.home, cfg))
        self.assertEqual([], self.gw.sql("SELECT * FROM decisions WHERE id=?", (accepted["decision_id"],)))
        self.gw.start()
        direct = self.claim(self.message("Synthetic request after receipt retention"), "direct")
        renewed = self.accept()
        self.assertNotEqual(accepted["decision_id"], renewed["decision_id"])
        self.assertEqual(renewed, self.accept())
        self.assertEqual(200, self.gw.contact(self.bot, self.actor, "untrust")[0])
        self.assert_draft(direct, "Synthetic reply withheld after trust withdrawal")
        other = self.gw.conversation(self.bot, self.caller)
        mid = self.message("Synthetic untrusted request", other)
        self.assertIn(mid, [m["job_id"] for m in self.gw.reviews(self.bot)["inbound"]])


if __name__ == "__main__":
    unittest.main()
