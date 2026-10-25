"""Contact bots: one bot per friend in the owner's own host, named after the friend and showing their picture, whose
scheduled task brings in that friend's messages (`friend sync`) and sends the owner's words back (`friend send`).

Two people, each with a self-enrolled bot (owner and source credential in one data home) and that bot's native
binding in a second data home, become friends through the CLI. Direct messages carry text and pictures both
ways; the friend's latest message is held for the owner's next words, which answer it, and earlier ones count as
read (no message needs a reply); what the owner sends goes with the owner credential, so no automation budget
counts it; replies and notes come in once each; the bot's general routine is kept away from these friends' jobs;
full conversations make room for new ones. Real bot_gateway.py subprocesses, synthetic homes and files, loopback only.
"""
import argparse
import base64
import itertools
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler
from support.numeric_http import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from support.public_harness import FriendsGateway, cli
from test_public_gateway import png
import bot_client as P

LIMITS = {"min_deadline_seconds": 1, "read_timeout_seconds": 5, "messages_per_minute_per_actor": 1000,
          "messages_per_hour_per_actor": 5000, "pending_per_contact": 100, "new_conversations_per_hour_per_bot": 1000,
          "new_contacts_per_hour_per_bot": 1000, "enroll_per_hour_per_ip": 1000, "enroll_per_hour": 1000,
          "bot_enroll_per_hour": 1000, "bot_enroll_per_hour_per_ip": 1000, "agent_messages_per_hour_per_bot": 1000,
          "turns_per_conversation": 3, "conversations_per_contact": 2, "blob_spill_bytes": 1024}
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
COUNTER = itertools.count(1)


class ContactBotCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="yobiru contact bots ")
        cls.root = Path(cls._tmp.name)
        cls.gw = FriendsGateway(cls.root, limits=LIMITS)
        cfg = json.loads((cls.gw.home / "gateway.json").read_text())
        cfg.setdefault("enrollment", {})["self_enrolled_publish"] = True     # as on bots.yobiru.com
        (cls.gw.home / "gateway.json").write_text(json.dumps(cfg))
        blobs = cls.gw.home / "blobs.json"                                   # bodies with pictures live in a store
        blobs.write_text(json.dumps({"kind": "local"}))
        os.chmod(str(blobs), 0o600)
        cls.gw.start()

    @classmethod
    def tearDownClass(cls):
        cls.gw.stop()
        cls._tmp.cleanup()

    def setUp(self):
        n = next(COUNTER)
        self.homes = {}
        for who in ("ann", "ben"):
            for role in ("", "-agent"):
                self.homes[who + role] = self.root / ("%s%s-%d" % (who, role, n))
        self.handles = {"ann": "ann-%d" % n, "ben": "ben-%d" % n}
        self.ids = {who: self.person(who) for who in ("ann", "ben")}
        self.y("ann", "connect", "%s/@%s" % (self.gw.url, self.handles["ben"]), "--follow", "--alias", "ben")
        self.y("ben", "connect", "%s/@%s" % (self.gw.url, self.handles["ann"]), "--follow", "--alias", "ann")

    def y(self, home, *args, expect=0):
        code, obj, err = cli(self.homes[home], *args)
        self.assertEqual(expect, code, "%s %s\n%s\n%s" % (home, args, obj, err))
        return obj if expect == 0 else (obj, err)

    def person(self, who):
        """A self-enrolled, published bot and its native binding in its own data home. -> bot id."""
        handle = self.handles[who]
        made = self.y(who, "enroll-bot", self.gw.url, "--handle", handle, "--name", who.title())
        self.y(who, "bot", "publish", handle)
        token_file = self.root / ("%s.native.token" % handle)
        self.y(who, "worker", "bind", handle, "--adapter", "native", "--token-file", str(token_file))
        self.y(who + "-agent", "worker", "login", self.gw.url, "--token-file", str(token_file))
        return made["bot_id"]

    def link(self, who, friend):
        return self.y(who, "friend", "link", friend, "--agent-home", str(self.homes[who + "-agent"]))

    def sync(self, who, friend):
        return self.y(who, "friend", "sync", friend)

    def allow_files(self, who):
        folder = self.root / ("%s-files" % self.handles[who])
        folder.mkdir()
        self.y(who, "attachments", "allow", str(folder))
        return folder


class FriendListTests(ContactBotCase):
    def test_friends_are_listed_with_their_names_and_contact_bots(self):
        friends = self.y("ann", "friend", "list")["friends"]
        self.assertEqual([("ben", self.ids["ben"], "Ben", None)],
                         [(f["alias"], f["bot_id"], f["display_name"], f["contact_bot"]) for f in friends])
        self.assertIn("avatar", friends[0], "null until the friend uploads a picture")
        self.link("ann", "ben")
        self.assertEqual("Ben", self.y("ann", "friend", "list")["friends"][0]["contact_bot"]["label"])
        self.y("ben", "unfollow", "ann")
        self.assertEqual([], self.y("ann", "friend", "list")["friends"], "a one-way follow is not a friend")
        _, err = self.y("ann", "friend", "link", "ben", "--agent-home", str(self.homes["ann-agent"]), expect=2)
        self.assertIn("not friends yet", err)

    def test_linking_needs_the_binding_and_keeps_the_owner_credential_out(self):
        _, err = self.y("ann", "friend", "link", "ben", "--agent-home", str(self.root / "nowhere"), expect=2)
        self.assertIn("holds no native binding credential", err)
        _, err = self.y("ann", "friend", "sync", "ben", expect=2)
        self.assertIn("has no contact bot here", err)


class MessageTests(ContactBotCase):
    def setUp(self):
        super().setUp()
        self.link("ann", "ben")
        self.link("ben", "ann")

    def test_messages_go_both_ways_once_each(self):
        sent = self.y("ben", "friend", "send", "ann", "--text", "dinner tonight?")
        self.assertEqual(("message", "queued", "owner credential"), (sent["sent_as"], sent["state"], sent["sent_with"]))
        got = self.sync("ann", "ben")
        self.assertEqual([("message", "dinner tonight?", False)],
                         [(m["type"], m["text"], m["answer_expected"]) for m in got["messages"]])
        self.assertEqual([sent["message_id"]], got["waiting_for_your_answer"], "held for the owner's next words")
        self.assertIn("Ben", got["show"])
        self.assertEqual([], self.sync("ann", "ben")["messages"], "each message comes in once")
        answered = self.y("ann", "friend", "send", "ben", "--text", "yes, 7pm")
        self.assertEqual(("answer", sent["message_id"]), (answered["sent_as"], answered["answered"]))
        self.assertEqual([("reply", "yes, 7pm", "dinner tonight?")],
                         [(m["type"], m["text"], m["to_your_message"]) for m in self.sync("ben", "ann")["messages"]])
        stored = self.gw.sql("SELECT content, result FROM messages WHERE kind='chat'")
        self.assertNotIn("dinner tonight", json.dumps(stored), "messages stay end-to-end encrypted")
        self.assertNotIn("7pm", json.dumps(stored))
        self.assertEqual([], self.gw.sql("SELECT count FROM counters WHERE key=?", (
            "auto:pair:%s:%s" % (self.ids["ben"], self.ids["ann"]),)), "the owner's words cost no automation budget")

    def test_pictures_travel_with_a_message(self):
        folder = self.allow_files("ben")
        (folder / "menu.png").write_bytes(PNG)
        sent = self.y("ben", "friend", "send", "ann", "--attach", str(folder / "menu.png"))
        self.assertEqual("message", sent["sent_as"], "files travel with any message")
        got = self.sync("ann", "ben")["messages"]
        self.assertEqual("menu.png", got[0]["text"], "a picture without words is named by its file")
        self.assertTrue(self.gw.sql("SELECT content FROM messages WHERE id=?", (got[0]["id"],))[0]["content"]
                        .startswith("blob:"), "the picture's body went to the blob store")
        saved = Path(got[0]["files"][0]["path"])
        self.assertEqual((PNG, "image/png"), (saved.read_bytes(), got[0]["files"][0]["media_type"]))
        self.assertEqual(0o600, saved.stat().st_mode & 0o777)

    def test_what_the_friends_bot_asks_is_answered_by_the_owners_next_words(self):
        asked = self.y("ben", "chat", "ann", "which wine do you like?")
        got = self.sync("ann", "ben")
        self.assertEqual([("message", "which wine do you like?", False)],
                         [(m["type"], m["text"], m["answer_expected"]) for m in got["messages"]])
        self.assertEqual([asked["message_id"]], got["waiting_for_your_answer"])
        again = self.sync("ann", "ben")
        self.assertEqual(([], [asked["message_id"]]), (again["messages"], again["waiting_for_your_answer"]),
                         "the message is held (its lease renewed), not shown twice")
        jobs = self.y("ann-agent", "job", "list", self.ids["ann"])["jobs"]
        self.assertEqual("ben", jobs[0]["contact_bot"]["alias"])
        answered = self.y("ann", "friend", "send", "ben", "--text", "a dry riesling")
        self.assertEqual(("answer", asked["message_id"], []),
                         (answered["sent_as"], answered["answered"], answered["marked_read"]))
        back = self.sync("ben", "ann")["messages"]
        self.assertEqual([("reply", "a dry riesling", "which wine do you like?")],
                         [(m["type"], m["text"], m["to_your_message"]) for m in back])
        self.assertEqual([], self.sync("ben", "ann")["messages"])
        nxt = self.y("ann", "friend", "send", "ben", "--text", "and you?")
        self.assertEqual("message", nxt["sent_as"], "with nothing held, the owner's words are a new message")

    def test_of_several_messages_the_latest_is_held_and_the_others_count_as_read(self):
        asked = [self.y("ben", "chat", "ann", "Question %d" % i)["message_id"] for i in range(3)]
        stamp = self.gw.sql("SELECT created_ts FROM messages WHERE id=?", (asked[0],))[0]["created_ts"]
        for mid in asked:
            self.gw.sql("UPDATE messages SET created_ts=? WHERE id=?", (stamp, mid))
        shown = self.sync("ann", "ben")
        self.assertEqual(asked, [m["id"] for m in shown["messages"]], "all shown once, in receipt order")
        self.assertEqual(1, len({m["at"] for m in shown["messages"]}))
        self.assertEqual((asked[-1:], 0), (shown["waiting_for_your_answer"], shown["waiting_behind_your_answer"]))
        self.assertEqual([("completed", "read")] * 2, [tuple(self.gw.sql(
            "SELECT state, code FROM messages WHERE id=?", (mid,))[0].values()) for mid in asked[:2]])
        self.assertEqual(asked[-1:], self.sync("ann", "ben")["waiting_for_your_answer"])
        api_call = P.C.api_call

        def lose_finish_response(base, method, path, *args, **kwargs):
            result = api_call(base, method, path, *args, **kwargs)
            if method == "POST" and path.endswith("/%s/finish" % asked[-1]):
                raise P.C.ApiError(0, "connection_failed", "Synthetic lost finish response")
            return result

        with patch.object(P.C, "api_call", side_effect=lose_finish_response):
            failed, _ = self.y("ann", "friend", "send", "ben", "--text", "One answer", expect=2)
        self.assertEqual("connection_failed", failed["error"])
        self.assertEqual([], self.y("ann-agent", "job", "list", self.ids["ann"])["jobs"])
        answer = self.y("ann", "friend", "send", "ben", "--text", "One answer")
        self.assertEqual((asked[-1], True), (answer["answered"], answer["duplicate"]),
                         "a lost finish response retries the same held message")
        self.assertEqual("One answer", self.y("ben", "status", asked[-1])["result"]["text"])
        self.assertEqual(["read", "read"], [self.y("ben", "status", mid)["code"] for mid in asked[:2]])

    def test_the_general_routine_leaves_a_linked_friend_alone(self):
        asked = self.y("ben", "chat", "ann", "are you there?")
        _, err = self.y("ann-agent", "job", "claim", self.ids["ann"], asked["message_id"], expect=2)
        self.assertIn("contact bot", err)
        self.y("ben", "friend", "send", "ann", "--text", "fyi")
        jobs = self.y("ann-agent", "job", "list", self.ids["ann"])["jobs"]
        self.assertEqual(["ben", "ben"], [j["contact_bot"]["alias"] for j in jobs], "every message is a job")
        self.assertEqual([], self.y("ann-agent", "job", "notices", self.ids["ann"])["notices"])
        self.sync("ann", "ben")
        self.y("ann", "friend", "send", "ben", "--text", "here")
        inbox = self.y("ben", "inbox")["items"]
        self.assertEqual(["ann"], [i["contact_bot"]["alias"] for i in inbox])
        unlinked = self.y("ann", "friend", "unlink", "ben")
        self.assertEqual([], unlinked["held_questions"])
        later = self.y("ben", "chat", "ann", "still there?")
        claimed = self.y("ann-agent", "job", "claim", self.ids["ann"], later["message_id"])
        self.assertEqual("still there?", claimed["untrusted_content"]["text"], "unlinked: the routine answers again")

    def test_full_conversations_make_room_for_new_ones(self):
        for i in range(8):                        # 3 turns per conversation, at most 2 conversations at a time
            self.y("ben", "friend", "send", "ann", "--text", "line %d" % i)
        self.assertEqual(["line %d" % i for i in range(8)], [m["text"] for m in self.sync("ann", "ben")["messages"]])


class HeldQuestionOrderTests(unittest.TestCase):
    """Use deliberately reversed IDs to make the same-second ordering regression deterministic."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="yobiru held questions ")
        self.addCleanup(tmp.cleanup)
        self.source, self.agent = P.State(Path(tmp.name) / "source"), P.State(Path(tmp.name) / "agent")
        self.ids = ["msg_" + c * 16 for c in "zyx"]
        self.bot, self.friend = "bot_" + "a" * 16, "bot_" + "b" * 16
        self.base = "http://127.0.0.1:1"
        self.source.save(P.CONTACT_BOTS, {"v": 1, "bots": {}, "links": {"ben": {
            "gateway": self.base, "as": self.bot, "bot_id": self.friend,
            "agent_home": str(self.agent.home)}}})
        for mid in self.ids:
            P.save_lease(self.agent, mid, {"bot_id": self.bot, "contact_bot": "ben", "lease_id": "lease-" + mid,
                                          "fence": 1, "job": {"created_at": "2026-01-01T00:00:00Z"}})
        self.args = argparse.Namespace(friend="ben", text="Synthetic answer", text_file=None, attach=None,
                                       new_message=False)

    def finished(self, *gone):
        """A stand-in for the gateway's finish: `read` succeeds, except for a job already final there."""
        def request(base, method, path, body, token):
            mid = path.split("/")[-2]
            if mid in gone:
                return {"ok": False, "http_status": 409, "error": "already_final"}
            self.assertEqual("read", body["outcome"])
            return {"ok": True, "message_id": mid, "state": "completed", "code": "read"}
        return request

    def test_same_second_legacy_leases_use_receipt_order_not_random_ids(self):
        # State serialization sorts object keys, and old leases have no local ordering field.
        self.assertEqual(list(reversed(self.ids)), list(self.agent.load("leases.json")["leases"]))
        with patch.object(P, "agent_home", return_value=(self.agent, "synthetic-token")), \
                patch.object(P, "call", return_value={"jobs": [{"job_id": mid} for mid in self.ids]}), \
                patch.object(P, "request", side_effect=self.finished()), \
                patch.object(P, "captured", return_value=(0, {"ok": True})) as captured, \
                patch.object(P, "out") as output:
            P.friend_send(self.source, self.args)
        self.assertEqual(self.ids[-1], captured.call_args.args[2].job, "the latest by receipt order is answered")
        self.assertEqual((self.ids[-1], self.ids[:2]), (output.call_args.args[0]["answered"],
                                                        output.call_args.args[0]["marked_read"]))
        self.assertEqual([self.ids[-1]], list(self.agent.load("leases.json")["leases"]))

    def test_an_unlisted_lease_already_final_there_is_forgotten(self):
        with patch.object(P, "agent_home", return_value=(self.agent, "synthetic-token")), \
                patch.object(P, "call", return_value={"jobs": [{"job_id": mid} for mid in self.ids[1:]]}), \
                patch.object(P, "request", side_effect=self.finished(self.ids[0])), \
                patch.object(P, "captured", return_value=(0, {"ok": True, "duplicate": True})) as captured, \
                patch.object(P, "out") as output:
            P.friend_send(self.source, self.args)
        self.assertEqual(self.ids[-1], captured.call_args.args[2].job)
        self.assertEqual(self.ids[:2], output.call_args.args[0]["marked_read"], "a finished job needs no read")
        self.assertNotIn(self.ids[0], self.agent.load("leases.json")["leases"])

    def test_single_held_question_retries_without_listing(self):
        for mid in self.ids[1:]:
            P.forget_lease(self.agent, mid)
        with patch.object(P, "agent_home", return_value=(self.agent, "synthetic-token")), \
                patch.object(P, "call", side_effect=AssertionError("one question needs no ordering lookup")), \
                patch.object(P, "captured", return_value=(0, {"ok": True, "duplicate": True})), \
                patch.object(P, "out") as output:
            P.friend_send(self.source, self.args)
        self.assertEqual(self.ids[0], output.call_args.args[0]["answered"])


class AvatarTests(ContactBotCase):
    def test_the_friends_own_picture_is_saved_for_the_contact_bot(self):
        self.assertIsNone(self.y("ann", "friend", "avatar", "ben")["avatar"], "no picture uploaded yet")
        picture = self.root / ("%s.png" % self.handles["ben"])
        picture.write_bytes(png(40, 40))
        self.y("ben", "bot", "avatar", self.handles["ben"], str(picture))
        listed = self.y("ann", "friend", "list")["friends"][0]["avatar"]
        self.assertTrue(listed.startswith(self.gw.url + "/@"), listed)
        saved = self.y("ann", "friend", "avatar", "ben")
        self.assertEqual("image/png", saved["media_type"])
        self.assertTrue(Path(saved["avatar"]).read_bytes().startswith(b"\x89PNG"), "the gateway's cleaned copy")

    def test_a_picture_is_taken_only_from_the_friends_gateway(self):
        card = dict(self.gw.ok("GET", "/@%s" % self.ids["ben"]))
        server = PictureServer(card, PNG)
        try:
            self.y("ann", "add", "%s/@%s" % (server.url, self.ids["ben"]), "--alias", "benpic")
            saved = self.y("ann", "friend", "avatar", "benpic")
            self.assertEqual((PNG, "image/png"), (Path(saved["avatar"]).read_bytes(), saved["media_type"]))
            server.card["avatar"] = "https://elsewhere.example/@%s/avatar/1" % self.ids["ben"]
            _, err = self.y("ann", "friend", "avatar", "benpic", expect=2)
            self.assertIn("outside its own gateway", err)
            server.card["avatar"] = server.url + "/@%s/avatar/1" % self.ids["ben"]
            server.picture = b"MZ" + bytes(100)
            _, err = self.y("ann", "friend", "avatar", "benpic", expect=2)
            self.assertIn("did not return a PNG", err)
        finally:
            server.stop()


class PictureServer(object):
    """A stand-in gateway that serves one card with an avatar, and the picture."""

    def __init__(self, card, picture):
        self.card, self.picture = card, picture
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if "/avatar/" in self.path:
                    body, ctype = owner.picture, "image/png"
                else:
                    body, ctype = json.dumps(owner.card).encode(), "application/json"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.card["avatar"] = self.url + "/@%s/avatar/0123456789abcdef" % card["bot_id"]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


if __name__ == "__main__":
    unittest.main()
