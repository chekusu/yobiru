"""Loop budgets, causal hops, owner continuation and direct messages over real HTTP.

Source bots are automated callers: consecutive turns per conversation and turns per (source bot, bot) pair per day
are checked before anything is charged and charged only on admission; identical op-key retries are free even when
a budget is exhausted, changed ones are refused first; the pair budget survives new and deleted conversations and
restarts; concurrent admissions are charged exactly; causes are validated by the gateway; an owner continuation is
bounded, exact and voided by a rebind or a narrowed grant; an old client's notice is a direct message, charged like
one, and what an owner sends as their bot is never charged. Real bot_gateway.py subprocesses with synthetic homes on
loopback.
"""
import hashlib
import tempfile
import threading
import unittest
from pathlib import Path

from support.public_harness import C, FriendsGateway

LIMITS = {"min_deadline_seconds": 1, "read_timeout_seconds": 5, "messages_per_minute_per_actor": 5000,
          "messages_per_hour_per_actor": 50000, "pending_per_contact": 500, "pending_per_bot": 2000,
          "new_conversations_per_hour_per_bot": 1000, "new_contacts_per_hour_per_bot": 1000,
          "enroll_per_hour_per_ip": 1000, "enroll_per_hour": 1000, "bot_enroll_per_hour": 1000,
          "bot_enroll_per_hour_per_ip": 1000, "agent_messages_per_hour_per_bot": 1000, "conversations_per_contact": 50,
          "automation_turns_per_conversation": 3, "automation_turns_per_pair_per_day": 5, "max_causal_hops": 2,
          "continuation_max_turns": 2}


class LoopCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="yobiru loops test ")
        cls.gw = FriendsGateway(Path(cls._tmp.name), limits=LIMITS).start()
        cls.owner = cls.gw.owner_token

    @classmethod
    def tearDownClass(cls):
        cls.gw.stop()
        cls._tmp.cleanup()

    def handle(self, tag=""):
        return "t%s%s" % (hashlib.sha256(self.id().encode()).hexdigest()[:12], "-" + tag if tag else "")

    def op(self):
        return C.new_token("ticket")[4:24]

    def pair(self):
        """A fresh target bot and a fresh source bot that talks to it: -> (target id, source id, source token)."""
        target = self.gw.bot(self.handle("t"))
        source, token = self.gw.owned_bot(self.handle("s"))
        return target, source, token

    def send(self, cid, token, text="question", kind="faq", op_key=None, **extra):
        return self.gw.send(cid, token, kind=kind, text=text, op_key=op_key or self.op(), **extra)

    def charged(self, source, target, cid=None):
        rows = self.gw.sql("SELECT count FROM counters WHERE key=?", ("auto:pair:%s:%s" % (source, target),))
        turns = self.gw.sql("SELECT auto_turns FROM conversations WHERE id=?", (cid,))[0]["auto_turns"] if cid else None
        return (rows[0]["count"] if rows else 0), turns

    def exhaust(self, cid, token, n=3):
        ids = []
        for i in range(n):
            status, obj = self.send(cid, token, "turn %d" % i)
            self.assertEqual(202, status, obj)
            ids.append(obj["message_id"])
        return ids

    def err(self, result):
        return result[0], result[1].get("error")


class BudgetTests(LoopCase):
    def test_exact_retries_succeed_once_exhausted_and_changes_are_refused_before_any_charge(self):
        target, source, token = self.pair()
        cid = self.gw.conversation(target, token)
        keys = [self.op() for _ in range(3)]
        sent = [self.send(cid, token, "turn %d" % i, op_key=k) for i, k in enumerate(keys)]
        self.assertEqual([202] * 3, [s for s, _ in sent])
        status, refused = self.send(cid, token, "one more")
        self.assertEqual((429, "automation_budget_exhausted", "conversation"),
                         (status, refused["error"], refused.get("scope")))
        self.assertEqual((3, 3), self.charged(source, target, cid))
        status, again = self.send(cid, token, "turn 2", op_key=keys[2])
        self.assertEqual((200, True, sent[2][1]["message_id"]), (status, again["duplicate"], again["message_id"]))
        for changed in ({"text": "turn 2, edited"}, {"kind": "notice"}, {"cause": sent[0][1]["message_id"]}):
            body = dict({"text": "turn 2", "kind": "faq"}, **changed)
            status, obj = self.send(cid, token, op_key=keys[2], **body)
            self.assertEqual((409, "op_key_conflict"), (status, obj["error"]), changed)
        self.assertEqual((3, 3), self.charged(source, target, cid), "nothing was charged again")

    def test_the_pair_budget_spans_new_and_deleted_conversations_and_restarts(self):
        target, source, token = self.pair()
        first = self.gw.conversation(target, token)
        self.exhaust(first, token, 3)
        second = self.gw.conversation(target, token)
        self.exhaust(second, token, 2)
        status, obj = self.send(self.gw.conversation(target, token), token)
        self.assertEqual((429, "pair"), (status, obj.get("scope")))
        for cid in (first, second):
            self.gw.ok("DELETE", "/v1/owner/conversations/%s" % cid, None, self.owner)
        status, obj = self.send(self.gw.conversation(target, token), token)
        self.assertEqual((429, "pair"), (status, obj.get("scope")), "deleting conversations resets nothing")
        self.gw.restart()
        status, obj = self.send(self.gw.conversation(target, token), token)
        self.assertEqual((429, "pair"), (status, obj.get("scope")), "a restart resets nothing")
        self.assertEqual(5, self.charged(source, target)[0])

    def test_a_caller_cannot_reset_or_skip_a_budget(self):
        target, source, token = self.pair()
        cid = self.gw.conversation(target, token)
        self.exhaust(cid, token)
        for field in ({"human": True}, {"reset": True}, {"hop": 0}, {"automated": False}):
            status, obj = self.send(cid, token, **field)
            self.assertEqual((400, "unknown_field"), (status, obj["error"]), field)
        wtoken, _ = self.gw.native(target)
        for other in (token, wtoken):
            status, _ = self.gw.call("POST", "/v1/owner/conversations/%s/continue" % cid,
                                     {"op_key": self.op(), "turns": 1, "anchor_message_id": None}, other)
            self.assertEqual(401, status, "only the bot's owner continues a conversation")
        _, other_owner, _ = self.gw.enroll_bot(self.handle("other"))
        self.assertEqual(404, self.gw.call("POST", "/v1/owner/conversations/%s/continue" % cid,
                                           {"op_key": self.op(), "turns": 1, "anchor_message_id": None},
                                           other_owner)[0])
        self.assertEqual((3, 3), self.charged(source, target, cid))

    def test_concurrent_admissions_are_charged_exactly(self):
        target, source, token = self.pair()
        cid = self.gw.conversation(target, token)
        results, barrier = [], threading.Barrier(8)

        def worker(i):
            barrier.wait()
            results.append(self.send(cid, token, "parallel %d" % i)[0])
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual((3, 5), (results.count(202), results.count(429)))
        self.assertEqual((3, 3), self.charged(source, target, cid))
        target2, source2, token2 = self.gw.bot(self.handle("t2")), source, token     # a fresh pair, same source
        cid2 = self.gw.conversation(target2, token2)
        key, same, barrier = self.op(), [], threading.Barrier(4)

        def retry():
            barrier.wait()
            same.append(self.send(cid2, token2, "the same one", op_key=key))
        threads = [threading.Thread(target=retry) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(1, len({obj["message_id"] for _, obj in same}))
        self.assertEqual([202, 200, 200, 200], sorted((s for s, _ in same), reverse=True))
        self.assertEqual((1, 1), self.charged(source2, target2, cid2), "one stored message, charged once")


class ContinuationTests(LoopCase):
    def test_a_continuation_is_bounded_exact_and_idempotent(self):
        target, source, token = self.pair()
        cid = self.gw.conversation(target, token)
        ids = self.exhaust(cid, token)
        path = "/v1/owner/conversations/%s/continue" % cid
        self.assertEqual((409, "anchor_mismatch"), self.err(self.gw.call(
            "POST", path, {"op_key": "continue-a1", "turns": 2, "anchor_message_id": ids[0]}, self.owner)))
        self.assertEqual((400, "invalid_turns"), self.err(self.gw.call(
            "POST", path, {"op_key": "continue-a1", "turns": 3, "anchor_message_id": ids[-1]}, self.owner)))
        status, granted = self.gw.call("POST", path, {"op_key": "continue-a1", "turns": 2, "ttl_seconds": 600,
                                                      "anchor_message_id": ids[-1]}, self.owner)
        self.assertEqual(201, status, granted)
        dec = granted["decision"]
        self.assertEqual(("continue_automation", {"turns": 2, "ttl_seconds": 600, "anchor_message_id": ids[-1]}),
                         (dec["action"], dec["scope"]))
        again = self.gw.ok("POST", path, {"op_key": "continue-a1", "turns": 2, "ttl_seconds": 600,
                                          "anchor_message_id": ids[-1]}, self.owner)
        self.assertEqual((True, dec["decision_id"]), (again["duplicate"], again["decision"]["decision_id"]))
        for changed in ({"turns": 1}, {"ttl_seconds": 900}, {"anchor_message_id": ids[0]}):
            body = dict({"op_key": "continue-a1", "turns": 2, "ttl_seconds": 600, "anchor_message_id": ids[-1]},
                        **changed)
            self.assertEqual((409, "op_key_conflict"), self.err(self.gw.call("POST", path, body, self.owner)), changed)
        self.assertEqual([202, 202], [self.send(cid, token, "allowed %d" % i)[0] for i in range(2)])
        self.assertEqual((429, "conversation"), (lambda r: (r[0], r[1].get("scope")))(self.send(cid, token)))
        current = self.gw.ok("GET", "/v1/owner/decisions/%s" % dec["decision_id"], None, self.owner)["current"]
        self.assertEqual((False, "used_up"), (current["authorized_now"], current["reason"]))
        _, visitor = self.gw.actor()
        vcid = self.gw.conversation(target, visitor)
        self.assertEqual((409, "not_applicable"), self.err(self.gw.call(
            "POST", "/v1/owner/conversations/%s/continue" % vcid,
            {"op_key": self.op(), "turns": 1, "anchor_message_id": None}, self.owner)))

    def test_a_rebind_or_a_narrowed_grant_voids_a_continuation(self):
        target, source, token = self.pair()
        self.gw.native(target)
        cid = self.gw.conversation(target, token)
        ids = self.exhaust(cid, token)
        path = "/v1/owner/conversations/%s/continue" % cid
        granted = self.gw.ok("POST", path, {"op_key": self.op(), "turns": 2, "anchor_message_id": ids[-1]}, self.owner)
        self.gw.native(target, replace=True)                  # a new binding epoch
        status, obj = self.send(cid, token)
        self.assertEqual((429, "conversation"), (status, obj.get("scope")))
        current = self.gw.ok("GET", "/v1/owner/decisions/%s" % granted["decision"]["decision_id"], None,
                             self.owner)["current"]
        self.assertEqual("binding_replaced", current["reason"])
        renewed = self.gw.ok("POST", path, {"op_key": self.op(), "turns": 2, "anchor_message_id": ids[-1]}, self.owner)
        self.assertEqual(200, self.gw.contact(target, source, "restrict", ["faq"])[0])
        fresh = self.gw.conversation(target, token)            # same budget scope: the pair, and this new conversation
        self.assertNotEqual(cid, fresh)
        current = self.gw.ok("GET", "/v1/owner/decisions/%s" % renewed["decision"]["decision_id"], None,
                             self.owner)["current"]
        self.assertFalse(current["authorized_now"])


class HopTests(LoopCase):
    def test_causes_are_validated_and_hops_are_bounded(self):
        a, a_token = self.gw.owned_bot(self.handle("a"))
        b, b_token = self.gw.owned_bot(self.handle("b"))
        c, c_token = self.gw.owned_bot(self.handle("c"))

        def say(token, target, cause=None, **extra):
            cid = self.gw.conversation(target, token)
            return self.send(cid, token, "chain", cause=cause, **extra) if cause else self.send(cid, token, "chain",
                                                                                                **extra)
        status, m0 = say(a_token, b)
        status, m1 = say(b_token, c, m0["message_id"])             # b received m0 as a job of its bot
        self.assertEqual((202, 1), (status, m1["causal_hop"]))
        status, m2 = say(c_token, a, m1["message_id"])
        self.assertEqual((202, 2), (status, m2["causal_hop"]))
        status, obj = say(a_token, b, m2["message_id"])
        self.assertEqual((409, "hop_limit_exceeded", 3), (status, obj["error"], obj.get("hop")))
        status, obj = say(a_token, c, m1["message_id"])            # a neither sent m1 nor received it
        self.assertEqual((400, "invalid_cause"), (status, obj["error"]))
        status, obj = say(a_token, c, "msg_aaaaaaaaaaaaaaaa")
        self.assertEqual((400, "invalid_cause"), (status, obj["error"]))
        status, obj = say(a_token, c, None, hop=0)
        self.assertEqual((400, "unknown_field"), (status, obj["error"]))


class DirectMessageTests(LoopCase):
    def test_an_old_clients_notice_is_a_direct_message_and_charged_like_one(self):
        target, source, token = self.pair()
        wtoken, wid = self.gw.native(target)
        ctoken = self.gw.ok("POST", "/v1/owner/bots/%s/workers/%s/connector" % (target, wid), {}, self.owner)["token"]
        cid = self.gw.conversation(target, token)
        self.exhaust(cid, token)
        status, obj = self.send(cid, token, "for your information", kind="notice")
        self.assertEqual((429, "conversation"), (status, obj.get("scope")), "since 0.7.14 a notice is a message")
        fresh = self.gw.conversation(target, token)
        status, sent = self.send(fresh, token, "for your information", kind="notice")
        self.assertEqual((202, "queued", None, False),
                         (status, sent["state"], sent["deadline"], sent["reply_required"]), sent)
        self.assertEqual((4, 1), self.charged(source, target, fresh))
        mid = sent["message_id"]
        self.assertIn(mid, [j["job_id"] for j in self.gw.ok("GET", "/v1/worker/jobs", None, wtoken)["jobs"]])
        self.assertIn(mid, [j["job_id"] for j in self.gw.ok("GET", "/v1/connector/jobs", None, ctoken)["jobs"]])
        self.assertEqual(("chat", "agent", None), tuple(self.gw.sql(
            "SELECT kind, route, deadline_ts FROM messages WHERE id=?", (mid,))[0].values()))

    def test_what_an_owner_sends_as_their_bot_is_never_charged(self):
        target, source, token = self.pair()
        cid = self.gw.conversation(target, token)
        self.exhaust(cid, token)
        path = "/v1/owner/bots/%s/conversations/%s/messages" % (source, cid)
        for i in range(3):
            status, sent = self.gw.call("POST", path, {"op_key": self.op(), "kind": "chat",
                                                       "text": "my own words %d" % i}, self.owner)
            self.assertEqual((202, "queued"), (status, sent.get("state")), sent)
        self.assertEqual((3, 3), self.charged(source, target, cid), "the owner's words are not automated turns")
        authors = self.gw.sql("SELECT actor_id FROM messages WHERE conversation_id=? AND author='caller'", (cid,))
        self.assertEqual({source}, {row["actor_id"] for row in authors}, "sent as the bot, into its conversation")
        _, visitor = self.gw.actor()
        vcid = self.gw.conversation(target, visitor)
        status, refused = self.gw.call("POST", "/v1/owner/bots/%s/conversations/%s/messages" % (source, vcid),
                                       {"op_key": self.op(), "kind": "chat", "text": "not my conversation"},
                                       self.owner)
        self.assertEqual((404, "not_found"), (status, refused.get("error")))

    def test_the_old_notice_inbox_is_empty_and_says_why(self):
        target = self.gw.bot(self.handle("t"))
        wtoken, _ = self.gw.native(target)
        actor_id, token = self.gw.actor()
        self.gw.contact(target, actor_id, "trust")
        cid = self.gw.conversation(target, token)
        sent = [self.send(cid, token, "notice %d" % i, kind="notice")[1]["message_id"] for i in range(3)]
        legacy = self.gw.ok("GET", "/v1/worker/notices", None, wtoken)
        self.assertEqual(([], False, 0), (legacy["notices"], legacy["more"], legacy["waiting_for_owner_review"]))
        self.assertIn("job list", legacy["note"])
        for path in ("/v1/worker/notices/after/%s" % sent[0],):
            self.assertEqual([], self.gw.ok("GET", path, None, wtoken)["notices"])
        for method, path in (("GET", "/v1/worker/notices/%s" % sent[0]),
                             ("POST", "/v1/worker/notices/%s/ack" % sent[0])):
            status, obj = self.gw.call(method, path, None if method == "GET" else {}, wtoken)
            self.assertEqual((404, "not_found"), (status, obj.get("error")), path)
        jobs = [j["job_id"] for j in self.gw.ok("GET", "/v1/worker/jobs", None, wtoken)["jobs"]]
        self.assertEqual(sent, jobs, "every one is a job, oldest first")


if __name__ == "__main__":
    unittest.main()
