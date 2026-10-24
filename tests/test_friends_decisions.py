"""Decision receipts: written in the same transaction as each review action, idempotent, queried only by the bot's
owner (full audit record) or by the caller a decision concerns (masked projection), uniformly not found for
everyone else, never a credential, and with the current authorization recomputed apart from the history.

Real bot_gateway.py subprocesses with synthetic homes on loopback. The deadline check of the current authorization
is also exercised in-process with a fake clock, so no background sweep can hide a missing check.
"""
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from support.public_harness import FriendsGateway

import bot_gateway as G  # noqa: E402  (on sys.path through the harness)

LIMITS = {"min_deadline_seconds": 1, "read_timeout_seconds": 5, "messages_per_minute_per_actor": 1000,
          "messages_per_hour_per_actor": 5000, "pending_per_contact": 100, "new_conversations_per_hour_per_bot": 1000,
          "new_contacts_per_hour_per_bot": 1000, "enroll_per_hour_per_ip": 1000, "enroll_per_hour": 1000,
          "bot_enroll_per_hour": 1000, "bot_enroll_per_hour_per_ip": 1000, "agent_messages_per_hour_per_bot": 1000}


def canonical(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def sha(obj):
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()


class DecisionCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="yobiru decisions test ")
        cls.gw = FriendsGateway(Path(cls._tmp.name), limits=LIMITS).start()
        cls.owner = cls.gw.owner_token

    @classmethod
    def tearDownClass(cls):
        cls.gw.stop()
        cls._tmp.cleanup()

    def handle(self, tag=""):
        return "t%s%s" % (hashlib.sha256(self.id().encode()).hexdigest()[:12], "-" + tag if tag else "")

    def setup_chat(self, files=False):
        bot_id = self.gw.bot(self.handle())
        if files:
            self.gw.policy(bot_id, offered=["faq", "leave_message", "chat", "files"],
                           visitor=["faq", "leave_message", "chat", "files"])
        wtoken, wid = self.gw.native(bot_id)
        actor_id, token = self.gw.actor()
        cid = self.gw.conversation(bot_id, token)
        return bot_id, wtoken, wid, actor_id, token, cid

    def review(self, mid, body):
        return self.gw.call("POST", "/v1/owner/messages/%s/review" % mid, body, self.owner)

    def request_hash(self, bot_id, mid):
        return [x for x in self.gw.reviews(bot_id)["inbound"] if x["job_id"] == mid][0]["request_hash"]

    def verify(self, token, **body):
        return self.gw.call("POST", "/v1/decisions/verify", body, token)

    def released(self, bot_id, wtoken, token, cid, text):
        mid = self.gw.chat(cid, token, text)
        self.gw.answer(bot_id, wtoken, mid, "reply to " + text)
        return mid


class ReceiptTests(DecisionCase):
    def test_review_actions_write_one_idempotent_receipt_each(self):
        bot_id, wtoken, wid, actor_id, token, cid = self.setup_chat()
        mid = self.gw.chat(cid, token, "hello")
        rhash = self.request_hash(bot_id, mid)
        status, admitted = self.review(mid, {"action": "approve_inbound", "request_hash": rhash})
        self.assertEqual(200, status, admitted)
        dec = admitted["decision"]
        self.assertEqual(("approve_inbound", rhash, "owner_credential", self.gw.url, mid, actor_id),
                         (dec["action"], dec["subject_hash"], dec["authority"], dec["issuer"], dec["message_id"],
                          dec["contact"]))
        record = {k: dec[k] for k in ("decision_id", "issuer", "bot_id", "conversation_id", "message_id", "contact",
                                      "action", "subject_hash", "grant_generation", "contact_epoch", "binding_epoch",
                                      "decided_at", "expires_at", "scope", "authority")}
        self.assertEqual(sha(record), dec["digest"], "the digest covers exactly the immutable record")
        again = self.review(mid, {"action": "approve_inbound", "request_hash": rhash})[1]
        self.assertEqual((True, dec["decision_id"]), (again["duplicate"], again["decision"]["decision_id"]))
        view = [m for m in self.gw.owner_view(cid)["messages"] if m["message_id"] == mid][0]
        self.assertEqual(dec["decision_id"], view["decisions"]["admission"])
        status, job = self.gw.claim_job(wtoken, mid)
        self.assertEqual(200, self.gw.reply_job(wtoken, job, "the answer")[0])
        draft = self.gw.draft_hash(bot_id, mid)
        released = self.review(mid, {"action": "approve_reply", "draft_hash": draft})[1]["decision"]
        self.assertEqual(("approve_reply", draft), (released["action"], released["subject_hash"]))
        self.assertEqual(released["decision_id"],
                         self.review(mid, {"action": "approve_reply", "draft_hash": draft})[1]["decision"]["decision_id"])
        listed = self.gw.ok("GET", "/v1/owner/bots/%s/decisions" % bot_id, None, self.owner)["decisions"]
        self.assertEqual({dec["decision_id"], released["decision_id"]}, {d["decision_id"] for d in listed})
        mine = self.gw.ok("GET", "/v1/messages/%s/decisions" % mid, None, token)["decisions"]
        self.assertEqual([("approve_inbound", rhash), ("approve_reply", draft)],
                         [(d["action"], d["subject_hash"]) for d in mine])
        for d in mine:
            self.assertFalse({"digest", "actor", "scope", "contact", "owner_id"} & set(d), d)
            self.assertTrue(d["current"]["authorized_now"] or d["action"] == "approve_inbound")
        result = self.gw.ok("GET", "/v1/messages/%s" % mid, None, token)["result"]
        checked = self.verify(token, decision_id=released["decision_id"], subject_hash=sha({"text": result["text"]}),
                              issuer=self.gw.url)[1]
        self.assertEqual((True, True), (checked["matches"]["subject_hash"], checked["current"]["authorized_now"]))

    def test_a_receipt_is_never_a_credential(self):
        bot_id, wtoken, wid, actor_id, token, cid = self.setup_chat()
        mid = self.gw.chat(cid, token, "hello")
        rhash = self.request_hash(bot_id, mid)
        ctoken = self.gw.ok("POST", "/v1/owner/bots/%s/workers/%s/connector" % (bot_id, wid), {}, self.owner)["token"]
        source = self.gw.source_for(bot_id)
        for other in (wtoken, ctoken, source, token):
            status, _ = self.gw.call("POST", "/v1/owner/messages/%s/review" % mid,
                                     {"action": "approve_inbound", "request_hash": rhash}, other)
            self.assertEqual(401, status, "only the owner credential reviews")
        dec = self.review(mid, {"action": "approve_inbound", "request_hash": rhash})[1]["decision"]
        status, _ = self.gw.raw("GET", "/v1/owner/bots/%s/decisions" % bot_id,
                                headers={"Authorization": "Bearer " + dec["decision_id"]})[:2]
        self.assertEqual(401, status, "a decision id authorizes nothing")
        self.assertEqual(401, self.verify(wtoken, decision_id=dec["decision_id"])[0])


class ProjectionTests(DecisionCase):
    def test_rejected_and_unreleased_drafts_leave_no_fingerprint_for_the_caller(self):
        bot_id, wtoken, wid, actor_id, token, cid = self.setup_chat()
        mid = self.gw.chat(cid, token, "ask")
        self.assertEqual(200, self.gw.admit(bot_id, mid)[0])
        status, job = self.gw.claim_job(wtoken, mid)
        self.assertEqual(200, self.gw.reply_job(wtoken, job, "SECRET DRAFT never released")[0])
        draft = self.gw.draft_hash(bot_id, mid)
        self.assertEqual(["approve_inbound"], [d["action"] for d in self.gw.ok(
            "GET", "/v1/messages/%s/decisions" % mid, None, token)["decisions"]], "a pending draft has no receipt yet")
        rejected = self.review(mid, {"action": "reject_reply"})[1]["decision"]
        self.assertEqual(draft, rejected["subject_hash"], "the owner's audit record keeps the rejected draft's hash")
        mine = [d for d in self.gw.ok("GET", "/v1/messages/%s/decisions" % mid, None, token)["decisions"]
                if d["action"] == "reject_reply"][0]
        self.assertIsNone(mine["subject_hash"])
        self.assertNotIn("digest", mine)
        checked = self.verify(token, decision_id=rejected["decision_id"], subject_hash=draft,
                              projection_digest=mine["projection_digest"])[1]
        self.assertEqual((None, True), (checked["matches"]["subject_hash"], checked["matches"]["projection_digest"]),
                         "no oracle for the hidden draft; the projection itself verifies")
        self.assertNotIn(draft, json.dumps(checked))
        self.assertEqual("refusal", checked["current"]["state"])

    def test_only_the_owner_and_the_concerned_caller_see_a_decision(self):
        bot_id, wtoken, wid, actor_id, token, cid = self.setup_chat()
        mid = self.released(bot_id, wtoken, token, cid, "mine")
        dec = [d for d in self.gw.ok("GET", "/v1/messages/%s/decisions" % mid, None, token)["decisions"]
               if d["action"] == "approve_reply"][0]
        other_actor, other = self.gw.actor("Another caller")
        trust = self.gw.contact(bot_id, actor_id, "trust")[1]["decision"]
        _, other_owner, other_source = self.gw.enroll_bot(self.handle("other"))
        for principal, did, body in ((other, dec["decision_id"], {}), (token, trust["decision_id"], {}),
                                     (token, dec["decision_id"], {"issuer": "https://other.example"}),
                                     (token, "dec_aaaaaaaaaaaaaaaa", {}), (other_owner, dec["decision_id"], {}),
                                     (other_source, dec["decision_id"], {})):
            self.assertEqual((404, "not_found"), self.err(self.verify(principal, decision_id=did, **body)))
        self.assertEqual(404, self.gw.call("GET", "/v1/messages/%s/decisions" % mid, None, other)[0])
        self.assertEqual(404, self.gw.call("GET", "/v1/owner/decisions/%s" % dec["decision_id"], None, other_owner)[0])
        self.assertEqual((400, "invalid_decision_id"), self.err(self.verify(token, decision_id="../dec")))
        self.assertEqual(200, self.verify(token, decision_id=dec["decision_id"])[0])
        self.assertEqual(200, self.gw.contact(bot_id, actor_id, "block")[0])
        self.assertEqual((404, "not_found"), self.err(self.verify(token, decision_id=dec["decision_id"])),
                         "a revoked caller learns nothing more")
        full = self.gw.ok("GET", "/v1/owner/decisions/%s" % dec["decision_id"], None, self.owner)
        self.assertEqual(("owner", False), (full["decision"]["actor"]["kind"], full["current"]["authorized_now"]))

    def err(self, result):
        return result[0], result[1].get("error")


class CurrentAuthorizationTests(DecisionCase):
    def test_history_stays_while_the_current_authorization_ends(self):
        bot_id, wtoken, wid, actor_id, token, cid = self.setup_chat()
        mid = self.released(bot_id, wtoken, token, cid, "keep the record")
        dec = [d for d in self.gw.ok("GET", "/v1/owner/bots/%s/decisions" % bot_id, None, self.owner)["decisions"]
               if d["action"] == "approve_reply"][0]
        self.assertTrue(self.gw.ok("GET", "/v1/owner/decisions/%s" % dec["decision_id"], None,
                                   self.owner)["current"]["authorized_now"])
        self.assertEqual(200, self.gw.contact(bot_id, actor_id, "restrict", ["faq"])[0])
        after = self.gw.ok("GET", "/v1/owner/decisions/%s" % dec["decision_id"], None, self.owner)
        self.assertEqual(dec["digest"], after["decision"]["digest"], "the record is immutable")
        self.assertEqual((False, "not_released_now"), (after["current"]["authorized_now"], after["current"]["reason"]))

    def test_trust_decisions_follow_the_current_state(self):
        bot_id, wtoken, wid, actor_id, token, cid = self.setup_chat()
        first = self.gw.contact(bot_id, actor_id, "trust")[1]["decision"]
        self.assertEqual("trust_sender", first["action"])
        self.assertEqual(first["decision_id"], self.gw.contact(bot_id, actor_id, "trust")[1]["decision"]["decision_id"])
        mid = self.gw.chat(cid, token, "trusted")
        view = [m for m in self.gw.owner_view(cid)["messages"] if m["message_id"] == mid][0]
        self.assertEqual(first["decision_id"], view["decisions"]["admission"])
        self.assertTrue(view["lifecycle"]["queue_accepted_at"])
        self.assertEqual(200, self.gw.contact(bot_id, actor_id, "restrict", ["chat"])[0])   # a new generation
        renewed = self.gw.contact(bot_id, actor_id, "trust")[1]["decision"]
        self.assertNotEqual(first["decision_id"], renewed["decision_id"], "a stale trust decision is never reused")
        self.assertEqual(renewed["grant_generation"], first["grant_generation"] + 1)
        ended = self.gw.contact(bot_id, actor_id, "untrust")[1]["decision"]
        self.assertEqual("untrust_sender", ended["action"])
        self.assertIsNone(self.gw.contact(bot_id, actor_id, "untrust")[1]["decision"])
        self.assertFalse(self.gw.ok("GET", "/v1/owner/decisions/%s" % renewed["decision_id"], None,
                                    self.owner)["current"]["authorized_now"])

    def test_an_expired_admission_is_not_current_before_any_sweep(self):
        bot_id, wtoken, wid, actor_id, token, cid = self.setup_chat()
        status, sent = self.gw.send(cid, token, kind="chat", text="short deadline", deadline_seconds=600)
        self.assertEqual(202, status, sent)
        mid = sent["message_id"]
        dec = self.review(mid, {"action": "approve_inbound", "request_hash": self.request_hash(bot_id, mid)})[1]
        db = sqlite3.connect("file:%s?mode=ro" % (self.gw.home / "gateway.sqlite3"), uri=True)
        db.row_factory = sqlite3.Row
        try:
            row = db.execute("SELECT * FROM decisions WHERE id=?", (dec["decision"]["decision_id"],)).fetchone()
            deadline = db.execute("SELECT deadline_ts FROM messages WHERE id=?", (mid,)).fetchone()[0]
            self.assertEqual("queued", db.execute("SELECT state FROM messages WHERE id=?", (mid,)).fetchone()[0])
            before = G.decision_current(db, row, deadline - 30)
            after = G.decision_current(db, row, deadline + 1)          # a fake clock: no sweep has run for it
        finally:
            db.close()
        self.assertEqual((True, False, "deadline_passed"), (before["authorized_now"], after["authorized_now"],
                                                            after["reason"]))


class AttachmentHashTests(DecisionCase):
    def test_attachment_names_and_contents_are_bound_into_both_hashes(self):
        bot_id, wtoken, wid, actor_id, token, cid = self.setup_chat(files=True)
        files = [{"name": "notes.md", "content": "one"}]
        status, sent = self.gw.send(cid, token, kind="chat", text="see the file", attachments=files)
        self.assertEqual(202, status, sent)
        mid = sent["message_id"]
        expected = sha({"kind": "chat", "content": {"text": "see the file", "attachments": files}})
        self.assertEqual(expected, self.request_hash(bot_id, mid))
        renamed = sha({"kind": "chat", "content": {"text": "see the file", "attachments": [
            {"name": "other.md", "content": "one"}]}})
        self.assertEqual(409, self.review(mid, {"action": "approve_inbound", "request_hash": renamed})[0])
        self.assertEqual(200, self.review(mid, {"action": "approve_inbound", "request_hash": expected})[0])
        status, job = self.gw.claim_job(wtoken, mid)
        self.assertEqual(files[0]["content"], job["untrusted_content"]["attachments"][0]["content"])
        reply = {"text": "answer", "attachments": [{"name": "answer.txt", "content": "two"}]}
        status, drafted = self.gw.call("POST", "/v1/worker/messages/%s/finish" % mid,
                                       {"lease_id": job["lease"]["lease_id"], "fence": job["lease"]["fence"],
                                        "outcome": "completed", "reply": reply}, wtoken)
        self.assertEqual((200, sha(reply)), (status, drafted["draft_hash"]))
        self.assertEqual((409, "draft_mismatch"), self.err(self.review(mid, {"action": "approve_reply",
                                                                             "draft_hash": sha({"text": "answer"})})))
        released = self.review(mid, {"action": "approve_reply", "draft_hash": sha(reply)})[1]["decision"]
        self.assertEqual(sha(reply), released["subject_hash"])

    def err(self, result):
        return result[0], result[1].get("error")


if __name__ == "__main__":
    unittest.main()
