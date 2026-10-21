"""Stable bot routing, the owner's private directory and the delivery contract, over real HTTP and the real CLI.

Contacts stay pinned to the immutable bot id through renames, handle reuse and binding replacement; only the owner
sees bindings, hosts and session notes; a card says a binding is configured, never that an agent is online; and
delivery evidence is a recorded correlation scoped to the current binding, judged against a TTL, monotonic, and
unknown under a clock that moved back. Real bot_gateway.py subprocesses with synthetic homes on loopback; the
native host and connector are played by the test through their credentials (protocol fixtures, never agents).
"""
import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path

from support.public_harness import C, FriendsGateway, cli

LIMITS = {"min_deadline_seconds": 1, "read_timeout_seconds": 5, "messages_per_minute_per_actor": 1000,
          "messages_per_hour_per_actor": 5000, "pending_per_contact": 100, "new_conversations_per_hour_per_bot": 1000,
          "new_contacts_per_hour_per_bot": 1000, "enroll_per_hour_per_ip": 1000, "enroll_per_hour": 1000,
          "bot_enroll_per_hour": 1000, "bot_enroll_per_hour_per_ip": 1000, "agent_messages_per_hour_per_bot": 1000,
          "quiet_wake_seconds": 2}


class RoutingCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="yobiru routing test ")
        cls.root = Path(cls._tmp.name)
        cls.gw = FriendsGateway(cls.root, limits=LIMITS).start()
        cls.owner = cls.gw.owner_token

    @classmethod
    def tearDownClass(cls):
        cls.gw.stop()
        cls._tmp.cleanup()

    def handle(self, tag=""):
        digest = hashlib.sha256(self.id().encode()).hexdigest()[:12]
        return "t%s%s" % (digest, "-" + tag if tag else "")

    def home(self, name):
        return self.root / ("%s-%s" % (self.handle(), name))

    def y(self, home, *args, expect=0):
        code, obj, err = cli(home, *args)
        self.assertEqual(expect, code, "%s\n%s\n%s" % (args, obj, err))
        return obj if expect == 0 else (obj, err)

    def rename(self, bot_id, handle):
        return self.gw.ok("PATCH", "/v1/owner/bots/%s" % bot_id, {"handle": handle}, self.owner)

    def native(self, bot_id, replace=False, **extra):
        obj = self.gw.ok("POST", "/v1/owner/bots/%s/workers" % bot_id,
                         dict({"adapter": "native", "label": "synthetic host", "replace": replace}, **extra), self.owner)
        return obj["token"], obj["worker_id"]

    def connector(self, bot_id, wid):
        return self.gw.ok("POST", "/v1/owner/bots/%s/workers/%s/connector" % (bot_id, wid), {}, self.owner)["token"]

    def check(self, bot_id, wid):
        return self.gw.ok("POST", "/v1/owner/bots/%s/workers/%s/check" % (bot_id, wid), {}, self.owner)["check_id"]

    def wake_check(self, ctok, chk, route="exec", outcome="accepted"):
        wake_id = C.new_token("ticket")[4:24]
        self.gw.ok("POST", "/v1/connector/checks/%s/wake" % chk, {"step": "intent", "wake_id": wake_id, "route": route},
                   ctok)
        self.gw.ok("POST", "/v1/connector/checks/%s/wake" % chk, {"step": "result", "wake_id": wake_id,
                                                                  "outcome": outcome}, ctok)

    def delivery(self, bot_id, wid):
        workers = self.gw.ok("GET", "/v1/owner/bots/%s/workers" % bot_id, None, self.owner)["workers"]
        return [w for w in workers if w["worker_id"] == wid][0]["delivery"]

    def card_chat(self, bot_id):
        return self.gw.ok("GET", "/@" + bot_id)["chat"]


# ---------------------------------------------------------------- identity continuity
class ContinuityTests(RoutingCase):
    def test_a_rename_keeps_the_contact_and_its_conversation(self):
        h = self.handle()
        bot_id = self.gw.bot(h)
        caller = self.home("caller")
        alias = self.y(caller, "connect", "%s/@%s" % (self.gw.url, h), "--name", "Synthetic caller")["alias"]
        first = self.y(caller, "chat", alias, "first question", "--faq")
        self.rename(bot_id, h + "-new")
        refreshed = self.y(caller, "contacts", "--refresh")["contacts"]
        self.assertEqual([(alias, bot_id, "handle_changed", "@%s-new" % h)],
                         [(c["alias"], c["bot_id"], c["status"], c["handle"]) for c in refreshed])
        again = self.y(caller, "connect", "%s/@%s-new" % (self.gw.url, h))
        self.assertEqual(alias, again["alias"], "the bot keeps its alias: a rename never splits a contact")
        self.assertEqual([alias], [c["alias"] for c in self.y(caller, "contacts")["contacts"]])
        second = self.y(caller, "chat", alias, "second question", "--faq")
        self.assertEqual(first["conversation_id"], second["conversation_id"])
        obj, err = self.y(caller, "lookup", "%s/@%s" % (self.gw.url, h), expect=2)
        self.assertEqual(("not_found", 404), (obj["error"], obj["http_status"]), "the old handle names nothing now")

    def test_a_reused_handle_never_retargets_a_saved_contact(self):
        h = self.handle()
        first_bot = self.gw.bot(h)
        caller = self.home("caller")
        self.y(caller, "connect", "%s/@%s" % (self.gw.url, h), "--name", "Synthetic caller")
        self.rename(first_bot, h + "-old")
        second_bot = self.gw.bot(h)                      # the same owner gives the handle to another bot
        self.assertNotEqual(first_bot, second_bot)
        obj, err = self.y(caller, "connect", "%s/@%s" % (self.gw.url, h), expect=2)
        self.assertIn("already names another bot", err)
        sent = self.y(caller, "chat", h, "for the first bot", "--faq")
        token = json.loads((caller / "public" / "credentials.json").read_text())["gateways"][self.gw.url]["actor"]["token"]
        self.assertEqual(first_bot, self.gw.ok("GET", "/v1/conversations/%s" % sent["conversation_id"], None,
                                               token)["bot_id"], "the saved contact still reaches the first bot")
        self.assertEqual(second_bot, self.y(caller, "connect", "%s/@%s" % (self.gw.url, h), "--alias",
                                            "second")["bot"]["bot_id"])
        report = {c["alias"]: (c["bot_id"], c["status"]) for c in self.y(caller, "contacts", "--refresh")["contacts"]}
        self.assertEqual({h: (first_bot, "handle_changed"), "second": (second_bot, "current")}, report)

    def test_a_rebind_keeps_the_bot_and_fences_the_old_binding(self):
        h = self.handle()
        bot_id = self.gw.bot(h)
        old_token, _ = self.native(bot_id)
        _, token = self.gw.actor()
        cid = self.gw.conversation(bot_id, token)
        mid = self.gw.chat(cid, token, "before the rebind")
        new_token, _ = self.native(bot_id, replace=True)
        self.assertEqual(bot_id, self.gw.ok("GET", "/@" + h)["bot_id"])
        self.assertEqual(401, self.gw.call("GET", "/v1/worker/jobs", None, old_token)[0])
        self.assertIn(mid, [j["job_id"] for j in self.gw.ok("GET", "/v1/worker/jobs", None, new_token)["jobs"]])
        later = self.gw.chat(cid, token, "after the rebind")
        self.assertEqual(cid, self.gw.ok("GET", "/v1/messages/%s" % later, None, token)["conversation_id"])

    def test_a_renamed_handle_stays_reserved_to_its_owner(self):
        h = self.handle()
        bot_id = self.gw.bot(h)
        self.rename(bot_id, h + "-moved")
        self.gw.enroll_bot(h, expect=409)                    # another owner can never take the old handle
        self.assertIn("@" + h, self.gw.ok("GET", "/v1/owner/directory", None, self.owner)["reserved_handles"])


# ---------------------------------------------------------------- the owner's private directory
class DirectoryTests(RoutingCase):
    def test_bindings_hosts_and_session_notes_are_private_to_the_owner(self):
        h = self.handle()
        bot_id = self.gw.bot(h)
        wtoken, wid = self.native(bot_id, label="LABEL-PRIVATE", host="codex", session_note="NOTE-PRIVATE thread",
                                  delivery={"tier": "push"})
        directory = self.gw.ok("GET", "/v1/owner/directory", None, self.owner)
        entry = [b for b in directory["bots"] if b["bot_id"] == bot_id][0]
        binding = entry["native_binding"]
        self.assertEqual(("LABEL-PRIVATE", "codex", "NOTE-PRIVATE thread", "push"),
                         (binding["label"], binding["host"], binding["session_note"],
                          binding["delivery"]["declared"]["tier"]))
        _, token = self.gw.actor()
        cid = self.gw.conversation(bot_id, token)
        mid = self.gw.chat(cid, token, "hello")
        self.assertEqual(200, self.gw.admit(bot_id, mid)[0])
        status, job = self.gw.claim_job(wtoken, mid)
        self.assertEqual(200, status, job)
        callers = json.dumps([self.gw.ok("GET", "/@" + h), self.gw.ok("GET", "/v1/bots/" + bot_id),
                              self.gw.relation(token, bot_id),
                              self.gw.ok("GET", "/v1/conversations/%s" % cid, None, token),
                              self.gw.tool(token, "yobiru_lookup_bot", {"bot": "@" + h})])
        agent = json.dumps([self.gw.ok("GET", "/v1/worker/jobs", None, wtoken), job,
                            self.gw.ok("GET", "/v1/worker", None, wtoken)])
        for private in ("LABEL-PRIVATE", "NOTE-PRIVATE", "codex", "session_note"):
            self.assertNotIn(private, callers + agent, private)
        self.assertNotIn(wid, callers, "the binding's id is the agent's own, never a caller's")
        other_bot, other_owner, _ = self.gw.enroll_bot(self.handle("other"))
        theirs = self.gw.ok("GET", "/v1/owner/directory", None, other_owner)
        self.assertEqual([other_bot], [b["bot_id"] for b in theirs["bots"]])
        self.assertNotIn(bot_id, json.dumps(theirs))
        self.assertEqual(404, self.gw.call("GET", "/v1/owner/bots/%s" % bot_id, None, other_owner)[0])


# ---------------------------------------------------------------- the delivery contract
class DeliveryTests(RoutingCase):
    def test_declarations_are_validated_and_stay_claims(self):
        bot_id = self.gw.bot(self.handle())
        path = "/v1/owner/bots/%s/workers" % bot_id
        for bad in ({"tier": "fast"}, {"tier": "push", "idle_wake": False}, {"tier": "turn", "idle_wake": True},
                    {"tier": "pull", "mid_turn": True}, {"tier": "unknown", "idle_wake": True},
                    {"tier": "push", "idle_wake": "yes"}, {"tier": "push", "idle_wake": 1}, {"tier": "push", "x": 1},
                    "push", {"idle_wake": True}):
            status, obj = self.gw.call("POST", path, {"adapter": "native", "delivery": bad}, self.owner)
            self.assertEqual((400, "invalid_delivery"), (status, obj.get("error")), bad)
        status, obj = self.gw.call("POST", path, {"adapter": "deterministic", "host": "codex"}, self.owner)
        self.assertEqual((400, "not_native"), (status, obj.get("error")))
        _, wid = self.native(bot_id, delivery={"tier": "push", "mid_turn": True})
        described = self.gw.ok("PATCH", "%s/%s" % (path, wid), {"delivery": {"tier": "pull"}, "host": "grok"},
                               self.owner)
        self.assertEqual({"tier": "pull", "idle_wake": False, "mid_turn": False},
                         {k: described["delivery"]["declared"][k] for k in ("tier", "idle_wake", "mid_turn")})
        self.assertEqual("none", described["delivery"]["status"]["wake_route"], "a declaration is not evidence")
        self.assertEqual((400, "invalid_host"), self.err(self.gw.call("PATCH", "%s/%s" % (path, wid),
                                                                      {"host": "anything"}, self.owner)))

    def err(self, result):
        return result[0], result[1].get("error")

    def test_the_card_says_configured_never_online(self):
        bot_id = self.gw.bot(self.handle())
        self.assertEqual({"agent_connected": False, "binding_configured": False, "declared_tier": "unknown",
                          "wake_evidence": "none"},
                         {k: self.card_chat(bot_id)[k] for k in ("agent_connected", "binding_configured",
                                                                 "declared_tier", "wake_evidence")})
        wtoken, wid = self.native(bot_id, delivery={"tier": "turn"})
        chat = self.card_chat(bot_id)
        self.assertEqual((True, "binding_configured", True, "turn", "none"),
                         (chat["agent_connected"], chat["agent_connected_means"], chat["binding_configured"],
                          chat["declared_tier"], chat["wake_evidence"]))
        self.assertFalse({"binding", "host", "session_note", "label", "worker_id", "evidence"} & set(chat))
        ctoken = self.connector(bot_id, wid)
        bare = self.check(bot_id, wid)                       # an acknowledgement alone is not wake evidence
        self.gw.ok("POST", "/v1/worker/checks/%s" % bare, {}, wtoken)
        accepted = self.check(bot_id, wid)                   # a wake 2xx alone is not wake evidence
        self.wake_check(ctoken, accepted)
        status = self.delivery(bot_id, wid)["status"]
        self.assertEqual(("recent", "none"), (status["check"], status["wake_route"]))
        self.assertEqual("none", self.card_chat(bot_id)["wake_evidence"])
        self.gw.ok("POST", "/v1/worker/checks/%s" % accepted, {}, wtoken)
        self.assertEqual("recent", self.delivery(bot_id, wid)["status"]["wake_route"])
        self.assertEqual("recent", self.card_chat(bot_id)["wake_evidence"])
        _, new_wid = self.native(bot_id, replace=True)       # evidence belongs to the binding it was recorded for
        self.assertEqual(("unknown", "none"), (self.card_chat(bot_id)["declared_tier"],
                                               self.card_chat(bot_id)["wake_evidence"]))
        old = self.delivery(bot_id, wid)
        self.assertEqual(("binding_not_current", "unknown"), (old["unknown_reason"], old["status"]["wake_route"]))

    def test_quiet_wake_correlation_needs_an_unused_credential(self):
        bot_id = self.gw.bot(self.handle())
        wtoken, wid = self.native(bot_id)
        ctoken = self.connector(bot_id, wid)
        busy = self.check(bot_id, wid)
        self.gw.ok("GET", "/v1/worker/jobs", None, wtoken)   # the host was using its credential just before the wake
        self.wake_check(ctoken, busy)
        self.gw.ok("POST", "/v1/worker/checks/%s" % busy, {}, wtoken)
        status = self.delivery(bot_id, wid)["status"]
        self.assertEqual(("recent", "none"), (status["wake_route"], status["quiet_wake"]))
        time.sleep(LIMITS["quiet_wake_seconds"] + 1.2)       # nothing used the binding credential meanwhile
        quiet = self.check(bot_id, wid)
        self.wake_check(ctoken, quiet, route="webhook")
        self.gw.ok("POST", "/v1/worker/checks/%s" % quiet, {}, wtoken)
        report = self.delivery(bot_id, wid)
        self.assertEqual("recent", report["status"]["quiet_wake"])
        self.assertEqual("webhook", report["evidence"]["quiet_wake_route"])
        self.assertIn("correlations", report["proves"])

    def test_evidence_only_moves_forward_and_keeps_its_route(self):
        bot_id = self.gw.bot(self.handle())
        wtoken, wid = self.native(bot_id)
        ctoken = self.connector(bot_id, wid)
        older = self.check(bot_id, wid)
        self.wake_check(ctoken, older, route="exec")
        self.gw.ok("POST", "/v1/worker/checks/%s" % older, {}, wtoken)
        time.sleep(1.2)
        newer = self.check(bot_id, wid)
        self.wake_check(ctoken, newer, route="webhook")
        self.gw.ok("POST", "/v1/worker/checks/%s" % newer, {}, wtoken)
        latest = self.delivery(bot_id, wid)["evidence"]
        self.gw.ok("POST", "/v1/worker/checks/%s" % older, {}, wtoken)       # a replay of the older correlation
        replayed = self.delivery(bot_id, wid)["evidence"]
        self.assertEqual((latest["wake_tested_at"], "webhook"), (replayed["wake_tested_at"],
                                                                 replayed["wake_tested_route"]))

    def test_stale_or_future_evidence_is_never_readiness(self):
        bot_id = self.gw.bot(self.handle())
        wtoken, wid = self.native(bot_id)
        ctoken = self.connector(bot_id, wid)
        chk = self.check(bot_id, wid)
        self.wake_check(ctoken, chk)
        self.gw.ok("POST", "/v1/worker/checks/%s" % chk, {}, wtoken)

        def set_wake(ts):
            ev = json.loads(self.gw.sql("SELECT evidence FROM workers WHERE id=?", (wid,))[0]["evidence"])
            ev["wake_tested_at"] = C.iso(ts)
            db = self.gw.db()
            try:
                db.execute("UPDATE workers SET evidence=? WHERE id=?", (json.dumps(ev), wid))
            finally:
                db.close()
        set_wake(C.now_ts() - 8 * 86400)
        self.assertEqual(("stale", "stale"), (self.delivery(bot_id, wid)["status"]["wake_route"],
                                              self.card_chat(bot_id)["wake_evidence"]))
        set_wake(C.now_ts() + 86400)                         # a clock that moved back: nothing is trusted
        report = self.delivery(bot_id, wid)
        self.assertEqual("clock_inconsistent", report["unknown_reason"])
        self.assertEqual({"unknown"}, set(report["status"].values()))
        self.assertEqual("unknown", self.card_chat(bot_id)["wake_evidence"])


# ---------------------------------------------------------------- the lifecycle stages
class LifecycleTests(RoutingCase):
    def test_stages_stay_separate_and_serving_is_not_delivery(self):
        bot_id = self.gw.bot(self.handle())
        wtoken, wid = self.native(bot_id)
        ctoken = self.connector(bot_id, wid)
        _, token = self.gw.actor()
        cid = self.gw.conversation(bot_id, token)
        mid = self.gw.chat(cid, token, "stages")

        def stages():
            return self.gw.ok("GET", "/v1/messages/%s" % mid, None, token)["lifecycle"]
        first = stages()
        self.assertTrue(first["received_at"])
        self.assertEqual([None] * 5, [first[k] for k in ("queue_accepted_at", "started_at", "final_at",
                                                         "response_served_at", "response_acknowledged_at")])
        self.assertEqual((409, "nothing_to_acknowledge"), self.err(self.gw.call(
            "POST", "/v1/messages/%s/ack" % mid, {}, token)))
        self.assertEqual(200, self.gw.admit(bot_id, mid)[0])
        self.assertTrue(stages()["queue_accepted_at"] and not stages()["started_at"])
        wake_id = C.new_token("ticket")[4:24]                # a wake the host's endpoint accepted: no evidence yet
        self.gw.ok("POST", "/v1/connector/messages/%s/wake" % mid,
                   {"step": "intent", "wake_id": wake_id, "phase": "ready", "route": "webhook"}, ctoken)
        self.gw.ok("POST", "/v1/connector/messages/%s/wake" % mid,
                   {"step": "result", "wake_id": wake_id, "outcome": "accepted"}, ctoken)
        self.assertEqual("none", self.delivery(bot_id, wid)["status"]["wake_route"])
        status, job = self.gw.claim_job(wtoken, mid)
        self.assertEqual(200, status, job)
        self.assertTrue(stages()["started_at"] and not stages()["final_at"])
        self.assertEqual("recent", self.delivery(bot_id, wid)["status"]["wake_route"], "the claim followed the wake")
        self.assertEqual(200, self.gw.reply_job(wtoken, job, "the answer")[0])
        self.assertEqual(200, self.gw.approve_reply(bot_id, mid)[0])
        served = stages()
        self.assertTrue(served["final_at"] and served["response_served_at"])
        self.assertIsNone(served["response_acknowledged_at"], "a GET is recorded as served, never as received")
        acked = self.gw.ok("POST", "/v1/messages/%s/ack" % mid, {}, token)
        again = self.gw.ok("POST", "/v1/messages/%s/ack" % mid, {}, token)
        self.assertEqual((False, True, acked["acknowledged_at"]), (acked["duplicate"], again["duplicate"],
                                                                   again["acknowledged_at"]))
        self.assertEqual(acked["acknowledged_at"], stages()["response_acknowledged_at"])

    def err(self, result):
        return result[0], result[1].get("error")


if __name__ == "__main__":
    unittest.main()
