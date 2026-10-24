"""Friends direct and email-verified enrollment.

Friends talk directly: chat and notices from a friend (two exact follow edges) reach the bot's agent without a
per-message review and the agent's replies to that friend are released without one; there is no switch. Each
admission and release still records a decision receipt (authority owner_policy). Strangers, visitors and one-way
followers stay reviewed; unfollowing sends the next draft back to review. A bot's connector also wakes its agent when
a bot it wrote to answers, until that answer is acknowledged.

Email verification: with enrollment.email_verification on, a new self-enrolled owner first proves control of an
email address with a 6-digit code; one address (lowercase, without +tag, Gmail without dots) names one owner. The
gateway only queues the mail; `mail-relay` delivers it and then forgets the address and the code.

Real bot_gateway.py subprocesses with synthetic homes on loopback; the relay runs in-process with a fake sender.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from support.public_harness import PUBLIC, C, FriendsGateway, cli, plain_env

import bot_gateway as G  # noqa: E402  (on sys.path through the harness)

LIMITS = {"min_deadline_seconds": 1, "read_timeout_seconds": 5, "messages_per_minute_per_actor": 1000,
          "messages_per_hour_per_actor": 5000, "pending_per_contact": 100, "new_conversations_per_hour_per_bot": 1000,
          "new_contacts_per_hour_per_bot": 1000, "enroll_per_hour_per_ip": 1000, "enroll_per_hour": 1000,
          "bot_enroll_per_hour": 1000, "bot_enroll_per_hour_per_ip": 1000, "agent_messages_per_hour_per_bot": 1000,
          "email_codes_per_hour_per_ip": 1000, "email_codes_per_hour_per_address": 1000}


class FriendDirectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="yobiru friend direct ")
        cls.gw = FriendsGateway(Path(cls._tmp.name), limits=LIMITS).start()
        cls.n = 0

    @classmethod
    def tearDownClass(cls):
        cls.gw.stop()
        cls._tmp.cleanup()

    def pair(self):
        """A (native agent) and B, mutual followers. -> (a, wtoken, b_source, a_source, conversation of B with A)."""
        FriendDirectTests.n += 1
        a, a_source = self.gw.owned_bot("fd-a%d" % self.n)
        b, b_source = self.gw.owned_bot("fd-b%d" % self.n)
        self.assertEqual(201, self.gw.follow(b_source, a)[0])
        self.assertEqual(201, self.gw.follow(a_source, b)[0])
        wtoken, _ = self.gw.native(a)
        return a, wtoken, b_source, a_source, b, self.gw.conversation(a, b_source)

    def read_job(self, wtoken, job):
        return self.gw.call("POST", "/v1/worker/messages/%s/finish" % job["job_id"],
                            {"lease_id": job["lease"]["lease_id"], "fence": job["lease"]["fence"], "outcome": "read"},
                            wtoken)

    def test_a_friend_reaches_the_agent_and_gets_its_reply_without_review(self):
        a, wtoken, b_source, _, _, cid = self.pair()
        self.assertNotIn("friend_direct", self.gw.ok("GET", "/v1/owner/bots/%s" % a, None,
                                                     self.gw.owner_token)["policy"], "there is no switch")
        mid = self.gw.chat(cid, b_source, "are you free on friday?")
        self.assertNotIn(mid, [x["job_id"] for x in self.gw.reviews(a)["inbound"]])
        status, job = self.gw.claim_job(wtoken, mid)
        self.assertEqual(200, status, job)
        self.assertEqual({"inbound": "approved", "reply": "direct"}, job["review"])
        self.assertEqual(G.AGENT_NOTICE_DIRECT, job["handling"])
        self.assertEqual("friend", job["relationship"])
        status, done = self.gw.reply_job(wtoken, job, "friday works")
        self.assertEqual((200, "completed", "friend_direct"), (status, done["state"], done["released"]))
        msg = self.gw.ok("GET", "/v1/messages/%s" % mid, None, b_source)
        self.assertEqual(("completed", "friday works"), (msg["state"], msg["result"]["text"]))
        self.assertEqual([], self.gw.reviews(a)["replies"])
        decisions = self.gw.ok("GET", "/v1/owner/bots/%s/decisions" % a, None, self.gw.owner_token)["decisions"]
        mine = sorted((d["action"], d["authority"], d["actor"]["kind"]) for d in decisions if d["message_id"] == mid)
        self.assertEqual([("friend_direct_inbound", "owner_policy", "owner_policy"),
                          ("friend_direct_reply", "owner_policy", "owner_policy")], mine)
        caller = self.gw.ok("GET", "/v1/messages/%s/decisions" % mid, None, b_source)["decisions"]
        self.assertEqual({"friend_direct_inbound", "friend_direct_reply"}, {d["action"] for d in caller})
        self.assertTrue(all(d["subject_hash"] for d in caller), "both are content the friend holds")
        self.assertTrue(all(d["note"] == G.POLICY_DECISION_NOTE for d in caller))
        # a retried finish is the same released answer
        self.assertEqual((200, True), (lambda s, o: (s, o.get("duplicate")))(*self.gw.reply_job(wtoken, job,
                                                                                               "friday works")))

    def test_strangers_and_one_way_followers_stay_reviewed(self):
        a, wtoken, _, _, _, _ = self.pair()
        c, c_source = self.gw.owned_bot("fd-c%d" % self.n)
        self.assertEqual(201, self.gw.follow(c_source, a)[0])            # c follows a; a does not follow c
        self.assertEqual("follower", self.gw.relation(c_source, a)["relationship"])
        follower_mid = self.gw.chat(self.gw.conversation(a, c_source), c_source, "hi from a follower")
        _, actor = self.gw.actor()
        visitor_mid = self.gw.chat(self.gw.conversation(a, actor), actor, "hi from a visitor")
        waiting = [x["job_id"] for x in self.gw.reviews(a)["inbound"]]
        self.assertIn(follower_mid, waiting)
        self.assertIn(visitor_mid, waiting)

    def test_unfollowing_sends_the_next_reply_back_to_review(self):
        a, wtoken, b_source, a_source, b, cid = self.pair()
        mid = self.gw.chat(cid, b_source, "one more thing")
        status, job = self.gw.claim_job(wtoken, mid)
        self.assertEqual(200, status, job)
        self.gw.unfollow(a_source, b)                                     # no longer friends: b is a follower
        status, done = self.gw.reply_job(wtoken, job, "draft only")
        self.assertEqual((200, "review"), (status, done["state"]))
        self.assertIn(mid, [x["job_id"] for x in self.gw.reviews(a)["replies"]])

    def test_the_old_switch_is_accepted_and_ignored(self):
        a, wtoken, b_source, _, _, cid = self.pair()
        status, obj = self.gw.call("PATCH", "/v1/owner/bots/%s" % a, {"policy": {"friend_direct": False}},
                                   self.gw.owner_token)
        self.assertEqual(200, status, obj)
        self.assertNotIn("friend_direct", obj["policy"])
        mid = self.gw.chat(cid, b_source, "still direct?")
        self.assertNotIn(mid, [x["job_id"] for x in self.gw.reviews(a)["inbound"]])


    def test_someone_the_bot_follows_is_in_its_inbox_even_without_a_follow_back(self):
        """X's rule: whom you follow reaches you directly, whether or not they follow you."""
        FriendDirectTests.n += 1
        a, a_source = self.gw.owned_bot("fd-x%d" % self.n)
        d, d_source = self.gw.owned_bot("fd-y%d" % self.n)
        self.assertEqual(201, self.gw.follow(a_source, d)[0])             # a follows d; d does not follow a
        self.gw.policy(a, visitor=["faq", "chat"])
        wtoken, _ = self.gw.native(a)
        mid = self.gw.chat(self.gw.conversation(a, d_source), d_source, "hello from someone you follow")
        self.assertNotIn(mid, [x["job_id"] for x in self.gw.reviews(a)["inbound"]])
        status, job = self.gw.claim_job(wtoken, mid)
        self.assertEqual((200, "direct"), (status, job["review"]["reply"]), job)
        self.assertEqual("completed", self.gw.reply_job(wtoken, job, "hi back")[1]["state"])

    def test_a_message_request_is_shown_in_full_and_can_be_ignored_or_accepted(self):
        a, wtoken, _, _, _, _ = self.pair()
        c, c_source = self.gw.owned_bot("fd-r%d" % self.n)
        self.assertEqual(201, self.gw.follow(c_source, a)[0])            # a one-way follower: a message request
        cid = self.gw.conversation(a, c_source)
        first = self.gw.chat(cid, c_source, "REQUEST-ONE can we talk?")
        request = [x for x in self.gw.reviews(a)["inbound"] if x["job_id"] == first][0]
        self.assertEqual("REQUEST-ONE can we talk?", request["message"]["text"], "the owner reads the request")
        self.assertEqual(G.REQUEST_TRUST, request["content_trust"])
        self.assertNotIn("REQUEST-ONE", json.dumps(self.gw.ok("GET", "/v1/worker/jobs", None, wtoken)),
                         "the agent's own listing stays metadata only")
        self.assertEqual("review_required", self.gw.claim_job(wtoken, first)[1]["error"])
        ignore = lambda mid: self.gw.call("POST", "/v1/owner/messages/%s/review" % mid,  # noqa: E731
                                          {"action": "ignore"}, self.gw.owner_token)
        self.assertEqual((200, "ignored", False), (lambda s, o: (s, o["inbound"], o["duplicate"]))(*ignore(first)))
        self.assertEqual((200, True), (lambda s, o: (s, o["duplicate"]))(*ignore(first)))
        self.assertNotIn(first, [x["job_id"] for x in self.gw.reviews(a)["inbound"]], "ignored: out of requests")
        self.assertEqual("queued", self.gw.ok("GET", "/v1/messages/%s" % first, None, c_source)["state"],
                         "the sender is not told")
        second = self.gw.chat(cid, c_source, "REQUEST-TWO hello again")
        self.assertIn(second, [x["job_id"] for x in self.gw.reviews(a)["inbound"]])
        # accepting the sender (trust) puts them in the inbox: both messages reach the agent, replies go out
        self.assertEqual(200, self.gw.call("POST", "/v1/owner/bots/%s/contacts/%s" % (a, c), {"action": "trust"},
                                           self.gw.owner_token)[0])
        self.assertEqual([], [x for x in self.gw.reviews(a)["inbound"] if x["job_id"] in (first, second)])
        status, job = self.gw.claim_job(wtoken, second)
        self.assertEqual((200, "direct"), (status, job["review"]["reply"]), job)
        self.assertEqual("completed", self.gw.reply_job(wtoken, job, "welcome")[1]["state"])
        self.assertEqual(409, ignore(second)[0], "only a request can be ignored")

    def test_messages_in_the_inbox_need_no_reply_each(self):
        """Bobo's case: a later message (a picture, a file) never waits for a reply to an earlier one."""
        a, wtoken, b_source, _, _, cid = self.pair()
        first = self.gw.chat(cid, b_source, "here is the menu")
        second = self.gw.chat(cid, b_source, "and the picture")
        third = self.gw.chat(cid, b_source, "which one do you want?")
        listed = {j["job_id"]: j for j in self.gw.ok("GET", "/v1/worker/jobs", None, wtoken)["jobs"]}
        self.assertTrue(all(listed[m]["claimable"] for m in (first, second, third)), listed)
        status, j1 = self.gw.claim_job(wtoken, first)
        self.assertEqual(200, status, j1)
        self.assertEqual([second, third], j1["more_from_this_sender"])
        status, j2 = self.gw.claim_job(wtoken, second)
        self.assertEqual(200, status, j2)
        status, j3 = self.gw.claim_job(wtoken, third)
        self.assertEqual(200, status, j3)
        for job in (j1, j2):
            status, done = self.read_job(wtoken, job)
            self.assertEqual((200, "completed", "read"), (status, done["state"], done["code"]), done)
        self.assertTrue(self.read_job(wtoken, j1)[1]["duplicate"])
        status, done = self.gw.reply_job(wtoken, j3, "the second one, please")
        self.assertEqual((200, "completed"), (status, done["state"]))
        seen = {m["message_id"]: m for m in self.gw.ok("GET", "/v1/conversations/%s" % cid, None,
                                                       b_source)["messages"]}
        self.assertEqual(G.READ_TEXT, seen[first]["explanation"])
        self.assertNotIn("result", seen[first])
        self.assertEqual("the second one, please", seen[third]["result"]["text"])
        # the next turn's transcript carries the read messages and the answer
        fourth = self.gw.chat(cid, b_source, "great")
        status, j4 = self.gw.claim_job(wtoken, fourth)
        said = [(t["role"], t.get("text"), t.get("read_without_reply")) for t in j4["transcript"]]
        self.assertEqual([("caller", "here is the menu", True), ("caller", "and the picture", True),
                          ("caller", "which one do you want?", None), ("bot", "the second one, please", None)], said)

    def test_only_the_inbox_can_be_marked_read_and_an_unanswered_inbox_message_counts_as_read(self):
        a, wtoken, b_source, a_source, b, cid = self.pair()
        c, c_source = self.gw.owned_bot("fd-m%d" % self.n)
        self.assertEqual(201, self.gw.follow(c_source, a)[0])
        other = self.gw.chat(self.gw.conversation(a, c_source), c_source, "a request")
        self.assertEqual(200, self.gw.admit(a, other)[0])                 # admitted once, not accepted
        status, job = self.gw.claim_job(wtoken, other)
        self.assertEqual(200, status, job)
        status, refused = self.read_job(wtoken, job)
        self.assertEqual((409, "not_in_inbox"), (status, refused["error"]))
        mid = self.gw.chat(cid, b_source, "anyone there?")
        status, job = self.gw.claim_job(wtoken, mid, lease_seconds=30)
        self.assertEqual(200, status, job)
        self.gw.sql("UPDATE messages SET lease_expires=? WHERE id=?", (C.now_ts() - 5, mid))
        self.gw.ok("GET", "/v1/worker/jobs", None, wtoken)
        status, late = self.gw.reply_job(wtoken, job, "too late")
        self.assertEqual(409, status, late)
        self.assertEqual({"state": "completed", "code": "read"},
                         self.gw.sql("SELECT state, code FROM messages WHERE id=?", (mid,))[0])
        self.assertEqual([], self.gw.reviews(a)["uncertain"])

    def test_the_answer_wakes_the_friend_that_asked(self):
        a, wtoken, b_source, a_source, b, cid = self.pair()
        _, b_wid = self.gw.native(b)
        conn = self.gw.ok("POST", "/v1/owner/bots/%s/workers/%s/connector" % (b, b_wid), {},
                          self.gw.owner_token)["token"]
        mid = self.gw.chat(cid, b_source, "what's for lunch?")
        self.assertEqual([], self.gw.ok("GET", "/v1/connector/jobs", None, conn)["replies"], "nothing answered yet")
        status, job = self.gw.claim_job(wtoken, mid)
        self.assertEqual(200, self.gw.reply_job(wtoken, job, "noodles")[0])
        replies = self.gw.ok("GET", "/v1/connector/jobs", None, conn)["replies"]
        self.assertEqual([{"message_id": mid, "from_bot_id": a, "state": "completed"}], replies)
        self.assertNotIn("noodles", json.dumps(replies), "ids only")
        inbox = self.gw.ok("GET", "/v1/inbox", None, b_source)
        self.assertIn(mid, [x["message_id"] for x in inbox["items"]])
        self.assertEqual(200, self.gw.call("POST", "/v1/messages/%s/ack" % mid, {}, b_source)[0])
        self.assertEqual([], self.gw.ok("GET", "/v1/connector/jobs", None, conn)["replies"], "acknowledged: done")

    def test_the_connector_wakes_once_for_an_answer(self):
        a, wtoken, b_source, a_source, b, cid = self.pair()
        _, b_wid = self.gw.native(b)
        home = Path(self._tmp.name) / ("wake-%d" % self.n)
        log = Path(self._tmp.name) / ("wakes-%d.log" % self.n)
        token_file = Path(self._tmp.name) / ("conn-%d.token" % self.n)
        for args in (("owner", "login", self.gw.url, "--token-file", str(self.gw.home / "owner.token")),
                     ("connector", "bind", b, b_wid, "--token-file", str(token_file)),
                     ("connector", "login", self.gw.url, "--token-file", str(token_file)),
                     ("connector", "config", b, "--exec", sys.executable, "-c",
                      "import sys; open(sys.argv[1], 'a').write(' '.join(sys.argv[2:]) + '\\n')", str(log),
                      "{kind}", "{message_id}")):
            code, out, err = cli(home, *args)
            self.assertEqual(0, code, err)
        mid = self.gw.chat(cid, b_source, "dinner?")
        _, job = self.gw.claim_job(wtoken, mid)
        self.gw.reply_job(wtoken, job, "hotpot")
        for _ in range(2):
            code, out, err = cli(home, "connector", "run", b, "--once")
            self.assertEqual(0, code, err)
        self.assertEqual(["yobiru.reply %s" % mid], log.read_text().splitlines(), "woken once, ids only")

class EmailEnrollmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="yobiru email enrollment ")
        cls.gw = FriendsGateway(Path(cls._tmp.name), limits=LIMITS)
        cfg = json.loads((cls.gw.home / "gateway.json").read_text())
        cfg["enrollment"] = {"visitors": True, "bots": True, "self_enrolled_publish": True, "email_verification": True}
        (cls.gw.home / "gateway.json").write_text(json.dumps(cfg))
        cls.gw.start()

    @classmethod
    def tearDownClass(cls):
        cls.gw.stop()
        cls._tmp.cleanup()

    def code_for(self, challenge_id):
        return self.gw.sql("SELECT code FROM mail_outbox WHERE challenge_id=?", (challenge_id,))[0]["code"]

    def challenge(self, email):
        obj = self.gw.ok("POST", "/v1/enroll/email", {"email": email, "lang": "zh"})
        return obj["challenge_id"], self.code_for(obj["challenge_id"]), obj

    def enroll(self, handle, challenge=None):
        owner, source = C.new_token("owner"), C.new_token("source")
        body = {"display_name": "Synthetic %s" % handle, "handle": handle,
                "source_credential_sha256": C.token_hash(source)}
        if challenge is not None:
            body["email_challenge"] = challenge
        return self.gw.call("POST", "/v1/enroll", body, owner)

    def test_the_gateway_advertises_it(self):
        enrollment = self.gw.ok("GET", "/.well-known/yobiru")["enrollment"]
        self.assertEqual((True, "POST /v1/enroll/email"), (enrollment["email_verification"], enrollment["email_code"]))

    def test_one_email_registers_one_owner(self):
        status, obj = self.enroll("em-nocode")
        self.assertEqual((400, "email_required"), (status, obj["error"]))
        cid, code, sent = self.challenge("Alice.Smith+yobiru@Gmail.com")
        self.assertEqual("a***@gmail.com", sent["sent_to"])
        wrong = "%06d" % ((int(code) + 1) % 10 ** 6)
        status, obj = self.enroll("em-first", {"id": cid, "code": wrong})
        self.assertEqual((400, "email_code_invalid", 4), (status, obj["error"], obj["attempts_left"]))
        self.assertEqual(1, self.gw.sql("SELECT attempts FROM email_challenges WHERE id=?", (cid,))[0]["attempts"],
                         "a wrong code is counted, not rolled back")
        status, obj = self.enroll("em-first", {"id": cid, "code": code})
        self.assertEqual((201, True), (status, obj["email_verified"]), obj)
        status, again = self.enroll("em-reuse", {"id": cid, "code": code})
        self.assertEqual((400, "email_code_expired"), (status, again["error"]), "a code works once")
        cid2, code2, _ = self.challenge("alicesmith@googlemail.com")       # the same mailbox, spelled differently
        status, obj = self.enroll("em-second", {"id": cid2, "code": code2})
        self.assertEqual((409, "email_in_use"), (status, obj["error"]))
        self.assertEqual(1, len(self.gw.sql("SELECT id FROM owners WHERE email_hash IS NOT NULL")))
        stored = json.dumps(self.gw.sql("SELECT * FROM owners")) + json.dumps(self.gw.sql("SELECT * FROM "
                                                                                          "email_challenges"))
        self.assertNotIn("alice", stored.lower(), "only keyed hashes of the address are kept")

    def test_a_challenge_is_spent_after_five_wrong_codes(self):
        cid, code, _ = self.challenge("bob@example.com")
        wrong = "%06d" % ((int(code) + 7) % 10 ** 6)
        for left in (4, 3, 2, 1, 0):
            status, obj = self.enroll("em-bob", {"id": cid, "code": wrong})
            self.assertEqual((400, left), (status, obj["attempts_left"]))
        status, obj = self.enroll("em-bob", {"id": cid, "code": code})
        self.assertEqual((400, "email_code_expired"), (status, obj["error"]))

    def test_bad_addresses_are_refused(self):
        for bad in ("not-an-email", "a@b", "+tag@example.com", "x" * 250 + "@example.com"):
            status, obj = self.gw.call("POST", "/v1/enroll/email", {"email": bad})
            self.assertEqual((400, "invalid_email"), (status, obj["error"]), bad)

    def test_the_relay_sends_once_and_forgets_the_address(self):
        cid, code, _ = self.challenge("carol@example.com")
        store = G.Store(self.gw.home, G.load_config(self.gw.home))
        mail = {"host": "smtp.invalid", "port": 465, "security": "ssl", "username": None, "password": None,
                "from": "Yobiru <no-reply@yobiru.test>", "reply_to": None}
        sent = []

        def fake(mail_cfg, messages):
            sent.extend(messages)
            return [None] * len(messages)
        G.relay_once(store, mail, send=fake)
        mine = [m for m in sent if m["To"] == "carol@example.com"]
        self.assertEqual(1, len(mine))
        self.assertIn(code, mine[0]["Subject"])
        self.assertIn(code, mine[0].get_body(("plain",)).get_content())
        self.assertIn(code, mine[0].get_body(("html",)).get_content())
        self.assertIn("验证码", mine[0]["Subject"])
        row = self.gw.sql("SELECT * FROM mail_outbox WHERE challenge_id=?", (cid,))[0]
        self.assertEqual((None, None), (row["address"], row["code"]))
        self.assertIsNotNone(row["sent_ts"])
        sent.clear()
        G.relay_once(store, mail, send=fake)
        self.assertEqual([], [m for m in sent if m["To"] == "carol@example.com"], "sent once")

    def test_a_failed_send_is_retried_then_given_up(self):
        cid, _, _ = self.challenge("dave@example.com")
        store = G.Store(self.gw.home, G.load_config(self.gw.home))
        mail = {"host": "smtp.invalid", "port": 465, "security": "ssl", "username": None, "password": None,
                "from": "no-reply@yobiru.test", "reply_to": None}
        for _ in range(4):
            G.relay_once(store, mail, send=lambda m, msgs: ["SMTPServerDisconnected"] * len(msgs))
        row = self.gw.sql("SELECT * FROM mail_outbox WHERE challenge_id=?", (cid,))[0]
        self.assertEqual((3, "SMTPServerDisconnected", None), (row["attempts"], row["error"], row["sent_ts"]))

    def test_the_client_enrolls_in_two_steps(self):
        home = Path(self._tmp.name) / "client"
        code, out, err = cli(home, "enroll-bot", self.gw.url, "--handle", "em-client", "--name", "Client",
                             "--email", "erin@example.com", "--lang", "ja")
        self.assertEqual(0, code, err)
        self.assertEqual("e***@example.com", out["sent_to"])
        saved = (home / "public" / "credentials.json").read_text()
        self.assertNotIn("erin", saved, "the client never stores the address")
        cid = self.gw.sql("SELECT challenge_id FROM mail_outbox WHERE address='erin@example.com'")[0]["challenge_id"]
        code, out, err = cli(home, "enroll-bot", self.gw.url, "--handle", "em-client", "--name", "Client",
                             "--email-code", self.code_for(cid))
        self.assertEqual(0, code, (out, err))
        self.assertTrue(out["email_verified"])


class SchemaThreeMigrationTests(unittest.TestCase):
    def test_a_schema_3_database_moves_to_4(self):
        with tempfile.TemporaryDirectory(prefix="yobiru migration 3-4 ") as tmp:
            gw = FriendsGateway(Path(tmp))
            db = gw.db()
            try:
                for statement in ("DROP INDEX owners_email", "ALTER TABLE owners DROP COLUMN email_hash",
                                  "DROP TABLE email_challenges", "DROP TABLE mail_outbox",
                                  "ALTER TABLE bots DROP COLUMN e2e_key", "ALTER TABLE bots DROP COLUMN e2e_key_ts",
                                  "ALTER TABLE actors DROP COLUMN e2e_key", "ALTER TABLE actors DROP COLUMN e2e_key_ts",
                                  "UPDATE meta SET value='3' WHERE key='schema_version'"):
                    db.execute(statement)
            finally:
                db.close()

            def run(*args):
                return subprocess.run([sys.executable, str(PUBLIC / "bot_gateway.py"), "--gateway-home",
                                       str(gw.home)] + list(args), capture_output=True, text=True,
                                      env=plain_env(Path(tmp)), timeout=60)
            refused = run("serve", "--port", "0")
            self.assertEqual(1, refused.returncode)
            self.assertIn("schema 3", refused.stderr)
            migrated = run("migrate")
            self.assertEqual(0, migrated.returncode, migrated.stderr)
            self.assertEqual(("8", "3"), tuple(json.loads(migrated.stdout)[k] for k in ("schema", "from_schema")))
            gw.start()
            try:
                bot_id = gw.bot("after-migration")
                self.assertTrue(bot_id)
                self.assertEqual([None], [r["email_hash"] for r in gw.sql("SELECT email_hash FROM owners")])
            finally:
                gw.stop()


if __name__ == "__main__":
    unittest.main()
