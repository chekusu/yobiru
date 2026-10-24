"""One kind of message (0.7.14): notices and left messages are direct messages, and none needs a reply.

A schema-7 database with notices and left messages in every state moves to schema 8 without losing one: unread
ones wait for the agent (in the inbox or the message requests, by their sender), read ones end as read, and left
messages no worker handled are offered again. Older clients keep working: `leave_message`, kinds `message` and
`notice`, the old CLI flags and a retry across the upgrade. A direct message has no deadline and ends with the
retention period. Real bot_gateway.py subprocesses with synthetic homes on loopback.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from support.public_harness import C, FriendsGateway, PUBLIC, cli, plain_env

import bot_gateway as G


def old_hash(**hashed):
    return G.sha(G.canonical(hashed))


class MergeMigrationTests(unittest.TestCase):
    def test_a_schema_7_database_turns_notices_and_left_messages_into_direct_messages(self):
        with tempfile.TemporaryDirectory(prefix="yobiru merge 7-8 ") as tmp:
            gw = FriendsGateway(Path(tmp)).start()
            try:
                bot, bot_source = gw.owned_bot("merge-target")
                wtoken, _ = gw.native(bot)
                friend, friend_source = gw.owned_bot("merge-friend")
                gw.follow(friend_source, bot)
                gw.follow(bot_source, friend)                      # the bot follows back: the friend is in its inbox
                visitor_id, visitor = gw.actor("Visitor")
                trusted_id, trusted = gw.actor("Trusted")
                self.assertEqual(200, gw.contact(bot, trusted_id, "trust")[0])
                v_cid, f_cid, t_cid = (gw.conversation(bot, t) for t in (visitor, friend_source, trusted))
                ids = {name: gw.chat(cid, token, text) for name, cid, token, text in (
                    ("unread", v_cid, visitor, "NOTICE-UNREAD"), ("read", f_cid, friend_source, "NOTICE-READ"),
                    ("expired", v_cid, visitor, "LEFT-EXPIRED"), ("done", t_cid, trusted, "LEFT-DONE"),
                    ("queued", f_cid, friend_source, "LEFT-QUEUED"))}
            finally:
                gw.stop()
            now = int(C.now_ts())
            db = gw.db()
            try:
                db.execute("BEGIN")
                # what 0.7.13 stored: notices (route notice, state stored) and left messages (deterministic route)
                notice = "kind='notice', capability='leave_message', route='notice', replay_safe=1, deadline_ts=NULL, "
                db.execute("UPDATE messages SET " + notice + "state='stored', finished_ts=created_ts, "
                           "admitted_ts=NULL, review_in=NULL WHERE id=?", (ids["unread"],))
                db.execute("UPDATE messages SET " + notice + "state='stored', finished_ts=created_ts, "
                           "agent_ack_ts=? WHERE id=?", (now - 5, ids["read"]))
                left = "kind='message', capability='leave_message', route='deterministic', replay_safe=1, "
                db.execute("UPDATE messages SET " + left + "state='expired', code='deadline_before_start', "
                           "finished_ts=?, deadline_ts=? WHERE id=?", (now - 60, now - 60, ids["expired"]))
                db.execute("UPDATE messages SET " + left + "state='completed', result=?, finished_ts=?, deadline_ts=? "
                           "WHERE id=?", (json.dumps({"text": "Your message was delivered."}), now - 30, now + 3600,
                                          ids["done"]))
                db.execute("UPDATE messages SET " + left + "state='queued', admitted_ts=NULL, review_in=NULL, "
                           "deadline_ts=? WHERE id=?", (now + 3600, ids["queued"]))
                pol = json.loads(db.execute("SELECT policy FROM bots WHERE id=?", (bot,)).fetchone()["policy"])
                pol.update(offered=["faq", "leave_message", "chat", "files"], visitor=["faq", "leave_message"],
                           follower=["faq", "leave_message", "chat"], friend=["faq", "leave_message", "chat", "files"])
                db.execute("UPDATE bots SET policy=? WHERE id=?", (json.dumps(pol), bot))
                db.execute("UPDATE contacts SET grants=?, ceiling=? WHERE bot_id=? AND actor_id=?",
                           (json.dumps(["faq", "leave_message"]), json.dumps(["leave_message", "chat"]), bot,
                            visitor_id))
                db.execute("UPDATE meta SET value='7' WHERE key='schema_version'")
                db.execute("COMMIT")
            finally:
                db.close()

            def migrate():
                return subprocess.run([sys.executable, str(PUBLIC / "bot_gateway.py"), "--gateway-home",
                                       str(gw.home), "migrate"], capture_output=True, text=True,
                                      env=plain_env(Path(tmp)), timeout=60)
            done = migrate()
            self.assertEqual(0, done.returncode, done.stderr)
            summary = json.loads(done.stdout)
            self.assertEqual(("8", "7", 1, 1, 1, 1, 2), tuple(summary[k] for k in (
                "schema", "from_schema", "policies_merged", "contacts_merged", "notices_waiting", "notices_read",
                "left_messages_waiting")))
            self.assertEqual("already current", json.loads(migrate().stdout)["note"])

            rows = {r["id"]: r for r in gw.sql("SELECT * FROM messages")}
            self.assertEqual({("chat", "chat", "agent")}, {(r["kind"], r["capability"], r["route"])
                                                            for r in rows.values()})
            state = lambda name: (rows[ids[name]]["state"], rows[ids[name]]["code"])  # noqa: E731
            self.assertEqual([("queued", None), ("completed", "read"), ("queued", None), ("completed", None),
                              ("queued", None)], [state(n) for n in ("unread", "read", "expired", "done", "queued")])
            for name in ("unread", "expired", "queued"):
                self.assertEqual((None, None), (rows[ids[name]]["deadline_ts"], rows[ids[name]]["finished_ts"]), name)
            self.assertEqual("Your message was delivered.", json.loads(rows[ids["done"]]["result"])["text"],
                             "what a worker answered stays as history")
            pol = json.loads(gw.sql("SELECT policy FROM bots WHERE id=?", (bot,))[0]["policy"])
            self.assertEqual((["faq", "chat", "files"], ["faq", "chat"], ["faq", "chat"]),
                             (pol["offered"], pol["visitor"], pol["follower"]))
            c = gw.sql("SELECT grants, ceiling FROM contacts WHERE bot_id=? AND actor_id=?", (bot, visitor_id))[0]
            self.assertEqual((["faq", "chat"], ["chat"]), (json.loads(c["grants"]), json.loads(c["ceiling"])))

            gw.start()
            try:
                requests = {r["job_id"] for r in gw.reviews(bot)["inbound"]}
                self.assertEqual({ids["unread"], ids["expired"]}, requests,
                                 "the visitor's notice and left message wait in the message requests")
                jobs = {j["job_id"]: j for j in gw.ok("GET", "/v1/worker/jobs", None, wtoken)["jobs"]}
                self.assertTrue(jobs[ids["queued"]]["claimable"], "the friend's left message is in the inbox")
                status, job = gw.claim_job(wtoken, ids["queued"])
                self.assertEqual(200, status, job)
                self.assertEqual("LEFT-QUEUED", job["untrusted_content"]["text"])
                self.assertEqual([], gw.ok("GET", "/v1/worker/notices", None, wtoken)["notices"])
                index = gw.sql("SELECT name FROM sqlite_master WHERE name='messages_open_undated'")
                self.assertEqual(1, len(index), "the retention sweep has its index")
            finally:
                gw.stop()


class DirectMessageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="yobiru direct messages ")
        cls.root = Path(cls.tmp.name)
        cls.gw = FriendsGateway(cls.root).start()
        cls.n = 0

    @classmethod
    def tearDownClass(cls):
        cls.gw.stop()
        cls.tmp.cleanup()

    def handle(self):
        type(self).n += 1
        return "dm-%d" % self.n

    def test_the_gateway_says_messages_are_one_kind(self):
        info = self.gw.ok("GET", "/.well-known/yobiru")
        self.assertEqual({"kinds": ["chat", "faq", "slots"], "owner_send": True, "notices": "merged_into_chat"},
                         info["messages"])

    def test_older_clients_names_are_read_as_a_direct_message(self):
        bot = self.gw.bot(self.handle(), policy={"offered": ["faq", "leave_message", "chat"],
                                                 "visitor": ["faq", "leave_message"]})
        pol = self.gw.ok("GET", "/v1/owner/bots/%s" % bot, None, self.gw.owner_token)["policy"]
        self.assertEqual((["faq", "chat"], ["faq", "chat"]), (pol["offered"], pol["visitor"]))
        _, token = self.gw.actor()
        cid = self.gw.conversation(bot, token)
        for kind in ("message", "notice", "chat"):
            status, sent = self.gw.send(cid, token, kind=kind, text="from an older client: %s" % kind)
            self.assertEqual((202, "queued", None, False),
                             (status, sent["state"], sent["deadline"], sent["reply_required"]), kind)
            self.assertEqual("chat", self.gw.ok("GET", "/v1/messages/%s" % sent["message_id"], None, token)["kind"])
        status, refused = self.gw.send(cid, token, kind="leave_message", text="not a kind")
        self.assertEqual((400, "unknown_kind"), (status, refused["error"]))
        status, timed = self.gw.send(cid, token, kind="chat", text="answer within an hour", deadline_seconds=3600)
        self.assertEqual(202, status, timed)
        self.assertIsNotNone(timed["deadline"], "a sender may still give a deadline")
        status, faq = self.gw.send(cid, token, kind="faq", text="hours?")
        self.assertIsNotNone(faq["deadline"], "FAQ questions keep the default deadline")

    def test_a_retry_from_before_the_upgrade_is_the_same_message(self):
        bot = self.gw.bot(self.handle())
        _, token = self.gw.actor()
        cid = self.gw.conversation(bot, token)
        for kind, before in (("chat", old_hash(kind="chat", content={"text": "same"}, deadline_seconds=86400)),
                             ("message", old_hash(kind="message", content={"text": "same"}, deadline_seconds=86400)),
                             ("notice", old_hash(kind="notice", content={"text": "same"}))):
            key = C.new_token("ticket")[4:24]
            status, first = self.gw.send(cid, token, kind=kind, text="same", op_key=key)
            self.assertEqual(202, status, first)
            self.gw.sql("UPDATE messages SET request_hash=? WHERE id=?", (before, first["message_id"]))
            status, again = self.gw.send(cid, token, kind=kind, text="same", op_key=key)
            self.assertEqual((200, first["message_id"], True), (status, again["message_id"], again["duplicate"]),
                             kind)
            status, changed = self.gw.send(cid, token, kind=kind, text="changed", op_key=key)
            self.assertEqual((409, "op_key_conflict"), (status, changed["error"]), kind)

    def test_an_unread_direct_message_waits_until_the_retention_period_ends(self):
        bot = self.gw.bot(self.handle())
        _, token = self.gw.actor()
        cid = self.gw.conversation(bot, token)
        old, recent = self.gw.chat(cid, token, "nobody read me"), self.gw.chat(cid, token, "still waiting")
        timed = self.gw.send(cid, token, kind="chat", text="with a deadline", deadline_seconds=60)[1]["message_id"]
        cfg = G.load_config(self.gw.home)
        days = cfg["limits"]["retention_days"]
        self.gw.sql("UPDATE messages SET created_ts=? WHERE id=?", (int(C.now_ts()) - days * 86400 - 60, old))
        self.gw.sql("UPDATE messages SET created_ts=?, deadline_ts=? WHERE id=?",
                    (int(C.now_ts()) - 120, int(C.now_ts()) - 60, timed))
        G.maintenance(G.Store(self.gw.home, cfg))
        left = {r["id"]: (r["state"], r["code"]) for r in self.gw.sql(
            "SELECT id, state, code FROM messages WHERE conversation_id=?", (cid,))}
        self.assertNotIn(old, left, "deleted at the end of the retention period, like any message")
        self.assertEqual(("queued", None), left[recent], "no deadline: it waits")
        self.assertEqual("expired", left[timed][0], "a deadline the sender gave still ends it")

    def test_the_old_cli_flags_send_a_direct_message(self):
        h = self.handle()
        self.gw.bot(h)
        home = self.root / ("caller-%s" % h)
        for args in (("enroll", self.gw.url, "--name", "Older habits"), ("add", "%s/@%s" % (self.gw.url, h))):
            code, out, err = cli(home, *args)
            self.assertEqual(0, code, err)
        for flag in ("--leave-message", "--notice"):
            code, sent, err = cli(home, "chat", h, "an old flag: %s" % flag, flag)
            self.assertEqual((0, "queued", False), (code, sent["state"], sent["reply_required"]), err)
            code, status, err = cli(home, "status", sent["message_id"], "--no-ack")
            self.assertEqual("chat", status["kind"], flag)
        code, _, err = cli(home, "chat", h, "both", "--faq", "--notice")
        self.assertNotEqual(0, code, "--faq is a question for the responder; the old flags cannot be combined")


if __name__ == "__main__":
    unittest.main()
