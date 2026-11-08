"""An owner deletes one of their bots from the gateway for good, without asking the maintainer.

`POST /v1/owner/bots/{id}/delete` with the handle repeated removes, in one transaction, the bot, its page, every
credential and binding made for it, its follows, contacts and inbox in both directions and every conversation it
had as the bot or as a caller of other bots. The handle stays reserved to the owner (a share link never leads to
someone else's bot), only the owner credential may do it, and other bots keep everything that is not about it.

Real bot_gateway.py subprocesses with synthetic homes on loopback.
"""
import json
import tempfile
import unittest
from pathlib import Path

from support.public_harness import C, FriendsGateway, cli

LIMITS = {"min_deadline_seconds": 1, "read_timeout_seconds": 5, "enroll_per_hour_per_ip": 1000,
          "enroll_per_hour": 1000, "bot_enroll_per_hour": 1000, "bot_enroll_per_hour_per_ip": 1000,
          "new_contacts_per_hour_per_bot": 1000, "new_conversations_per_hour_per_bot": 1000,
          "bots_per_self_enrolled_owner": 2}


class BotDeleteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="yobiru bot delete ")
        cls.root = Path(cls._tmp.name)
        cls.gw = FriendsGateway(cls.root, limits=LIMITS)
        cfg = json.loads((cls.gw.home / "gateway.json").read_text())
        cfg["enrollment"] = {"visitors": True, "bots": True, "self_enrolled_publish": True}
        (cls.gw.home / "gateway.json").write_text(json.dumps(cfg))
        cls.gw.start()
        cls.n = 0

    @classmethod
    def tearDownClass(cls):
        cls.gw.stop()
        cls._tmp.cleanup()

    def handle(self, tag):
        BotDeleteTests.n += 1
        return "del-%s-%d" % (tag, self.n)

    def delete(self, bot_id, confirm, token):
        body = {} if confirm is None else {"confirm": confirm}
        return self.gw.call("POST", "/v1/owner/bots/%s/delete" % bot_id, body, token)

    def rows(self, statement, *args):
        return self.gw.sql(statement, args)

    def test_the_owner_deletes_a_bot_with_everything_about_it_in_both_directions(self):
        gw = self.gw
        alice_handle = self.handle("alice")
        alice, owner, source = gw.enroll_bot(alice_handle)
        gw.ok("POST", "/v1/owner/bots/%s/publish" % alice, {}, owner)
        gw.ok("PATCH", "/v1/owner/bots/%s" % alice, {"links": [{"label": "Site", "url": "https://example.com/a"}]},
              owner)
        wtoken, worker_id = gw.native(alice, owner=owner)
        bob, bob_source = gw.owned_bot(self.handle("bob"))
        carol, carol_source = gw.owned_bot(self.handle("carol"))

        # friends with bob both ways, a follower in carol, conversations in both directions and a visitor
        self.assertIn(gw.follow(source, bob)[0], (200, 201))
        self.assertIn(gw.follow(bob_source, alice)[0], (200, 201))
        self.assertIn(gw.follow(carol_source, alice)[0], (200, 201))
        to_bob = gw.conversation(bob, source)
        gw.chat(to_bob, source, "hello bob")
        from_bob = gw.conversation(alice, bob_source)
        gw.chat(from_bob, bob_source, "hello alice")
        visitor_id, visitor = gw.actor("Synthetic visitor")
        from_visitor = gw.conversation(alice, visitor)
        gw.chat(from_visitor, visitor, "hello from a visitor")
        # what bob has with someone else stays
        bob_visitor = gw.conversation(bob, visitor)
        kept = gw.chat(bob_visitor, visitor, "hello bob, from a visitor")
        self.assertIn(gw.follow(carol_source, bob)[0], (200, 201))

        # a wrong, missing or bare-id confirmation changes nothing
        for confirm in (None, "@" + alice_handle + "x", alice, 7):
            status, obj = self.delete(alice, confirm, owner)
            self.assertEqual(400, status, (confirm, obj))
        self.assertEqual("confirm_mismatch", self.delete(alice, "@nobody", owner)[1]["error"])
        self.assertEqual(1, len(self.rows("SELECT id FROM bots WHERE id=?", alice)))

        status, obj = self.delete(alice, "@" + alice_handle, owner)
        self.assertEqual(200, status, obj)
        self.assertEqual((True, alice, "@" + alice_handle, True), (obj["deleted"], obj["bot_id"], obj["handle"],
                                                                   obj["handle_reserved"]))
        self.assertEqual({"conversations": 3, "messages": 3, "follows": 3, "contacts": 3},
                         {k: obj["removed"][k] for k in ("conversations", "messages", "follows", "contacts")},
                         obj["removed"])
        self.assertGreaterEqual(obj["removed"]["credentials"], 2, "the source and the worker binding at least")

        # nothing about the bot is left on the gateway, in either direction
        for statement, args in (
                ("SELECT id FROM bots WHERE id=?", (alice,)),
                ("SELECT id FROM conversations WHERE bot_id=? OR actor_id=?", (alice, alice)),
                ("SELECT id FROM messages WHERE bot_id=? OR actor_id=?", (alice, alice)),
                ("SELECT id FROM messages WHERE conversation_id IN (?,?,?)", (to_bob, from_bob, from_visitor)),
                ("SELECT src FROM follows WHERE src=? OR dst=?", (alice, alice)),
                ("SELECT bot_id FROM contacts WHERE bot_id=? OR actor_id=?", (alice, alice)),
                ("SELECT bot_id FROM inbox_acceptances WHERE bot_id=? OR actor_id=?", (alice, alice)),
                ("SELECT id FROM decisions WHERE bot_id=? OR contact=?", (alice, alice)),
                ("SELECT hash FROM credentials WHERE subject IN (?,?)", (alice, worker_id)),
                ("SELECT id FROM workers WHERE bot_id=?", (alice,)),
                ("SELECT bot_id FROM bot_profiles WHERE bot_id=?", (alice,)),
                ("SELECT bot_id FROM bot_stats WHERE bot_id=?", (alice,)),
                ("SELECT bot_id FROM connector_changes WHERE bot_id=?", (alice,))):
            self.assertEqual([], self.rows(statement, *args), statement)
        self.assertEqual([alice_handle], [r["handle"] for r in self.rows(
            "SELECT handle FROM handles WHERE owner_id=(SELECT owner_id FROM bots WHERE id=?) OR handle=?",
            bob, alice_handle) if r["handle"] == alice_handle], "the handle stays reserved")

        # its credentials, page and share link are gone; others see no trace of it
        self.assertEqual(401, gw.call("GET", "/v1/actor", None, source)[0])
        self.assertEqual(401, gw.call("POST", "/v1/worker/claim", {}, wtoken)[0])
        self.assertEqual(404, gw.call("GET", "/v1/bots/@" + alice_handle)[0])
        self.assertEqual(404, gw.call("GET", "/v1/bots/%s" % alice)[0])
        self.assertEqual(404, gw.call("POST", "/v1/conversations/%s/messages" % from_visitor,
                                      {"op_key": "again-after-delete", "kind": "chat", "text": "still there?"},
                                      visitor)[0])
        self.assertEqual([], [c for c in gw.ok("GET", "/v1/owner/bots/%s/contacts" % bob, None,
                                                gw.owner_token)["contacts"] if c["actor_id"] == alice])
        self.assertEqual(404, gw.call("GET", "/v1/bots/%s/contact" % alice, None, bob_source)[0])

        # what bob and carol have with others is untouched
        self.assertEqual(1, len(self.rows("SELECT id FROM messages WHERE id=?", kept)))
        self.assertEqual(["active"], [r["state"] for r in self.rows("SELECT state FROM follows WHERE src=? AND dst=?",
                                                                     carol, bob)])
        self.assertEqual(visitor_id, self.rows("SELECT actor_id FROM conversations WHERE id=?", bob_visitor)[0]
                         ["actor_id"])

        # the owner keeps the account and the handle: the same handle names a new bot of theirs, nobody else's
        self.assertEqual([], gw.ok("GET", "/v1/owner/bots", None, owner)["bots"])
        _, rival, _ = gw.enroll_bot(self.handle("rival"))
        status, obj = gw.call("POST", "/v1/owner/bots", {"handle": alice_handle, "display_name": "Impostor"}, rival)
        self.assertEqual((409, "handle_unavailable"), (status, obj.get("error")))
        again = gw.ok("POST", "/v1/owner/bots", {"handle": alice_handle, "display_name": "Alice again"}, owner)
        self.assertNotEqual(alice, again["bot_id"], "a new bot, with nothing of the deleted one")
        self.assertEqual([], self.rows("SELECT src FROM follows WHERE src=? OR dst=?", again["bot_id"], again["bot_id"]))

    def test_only_the_owner_credential_can_delete(self):
        gw = self.gw
        h = self.handle("guarded")
        bot_id, owner, _ = gw.enroll_bot(h)
        manager = gw.ok("POST", "/v1/owner/bots/%s/managers" % bot_id, {"label": "second machine"}, owner)["token"]
        status, obj = self.delete(bot_id, "@" + h, manager)
        self.assertEqual((403, "bot_scoped"), (status, obj.get("error")), obj)
        _, other, _ = gw.enroll_bot(self.handle("other"))
        self.assertEqual(404, self.delete(bot_id, "@" + h, other)[0], "another owner never learns the bot exists")
        _, visitor = gw.actor()
        self.assertEqual(401, self.delete(bot_id, "@" + h, visitor)[0])
        self.assertEqual(401, self.delete(bot_id, "@" + h, None)[0])
        self.assertEqual(1, len(self.rows("SELECT id FROM bots WHERE id=?", bot_id)))
        # the manager credential goes with the bot
        self.assertEqual(200, self.delete(bot_id, h, owner)[0], "the handle without @ also confirms")
        self.assertEqual(401, gw.call("GET", "/v1/owner/bots", None, manager)[0])
        self.assertEqual(404, self.delete(bot_id, "@" + h, owner)[0], "deleting twice finds nothing")

    def test_a_deleted_bot_no_longer_counts_against_the_quota(self):
        gw = self.gw
        first, owner, _ = gw.enroll_bot(self.handle("quota"))
        second_handle = self.handle("quota")
        second = gw.ok("POST", "/v1/owner/bots", {"handle": second_handle, "display_name": "Second"}, owner)
        status, obj = gw.call("POST", "/v1/owner/bots", {"handle": self.handle("quota"), "display_name": "Third"},
                              owner)
        self.assertEqual((409, "too_many_bots"), (status, obj.get("error")))
        self.assertEqual(200, self.delete(second["bot_id"], "@" + second_handle, owner)[0])
        self.assertEqual(201, gw.call("POST", "/v1/owner/bots", {"handle": self.handle("quota"),
                                                                 "display_name": "Third"}, owner)[0])
        self.assertEqual(first, gw.ok("GET", "/v1/owner/bots/%s" % first, None, owner)["bot_id"])

    def test_the_cli_deletes_and_forgets_the_bot_on_this_device(self):
        home = self.root / ("cli-%d" % self.n)
        token_file = self.root / ("cli-owner-%d.token" % self.n)
        token_file.write_text(self.gw.owner_token + "\n")
        token_file.chmod(0o600)
        self.assertEqual(0, cli(home, "owner", "login", self.gw.url, "--token-file", str(token_file))[0])
        h = self.handle("cli")
        code, made, err = cli(home, "bot", "create", "--handle", h, "--name", "CLI bot")
        self.assertEqual(0, code, err)
        creds = json.loads((home / "public" / "credentials.json").read_text())["gateways"][self.gw.url]
        self.assertIn(made["bot_id"], creds["sources"])

        code, _, err = cli(home, "bot", "delete", h)
        self.assertEqual(2, code, "--confirm is required")
        code, obj, err = cli(home, "bot", "delete", h, "--confirm", "@someone-else")
        self.assertEqual((2, "confirm_mismatch"), (code, obj["error"]), err)
        self.assertEqual(1, len(self.rows("SELECT id FROM bots WHERE id=?", made["bot_id"])))

        code, obj, err = cli(home, "bot", "delete", "@" + h, "--confirm", "@" + h)
        self.assertEqual(0, code, err)
        self.assertTrue(obj["deleted"])
        self.assertIn("sources", obj["forgot_on_this_device"])
        creds = json.loads((home / "public" / "credentials.json").read_text())["gateways"][self.gw.url]
        self.assertNotIn(made["bot_id"], creds.get("sources") or {})
        self.assertNotEqual(made["bot_id"], creds.get("default_identity"))
        self.assertIn("owner", creds, "the owner credential stays")
        self.assertEqual([], self.rows("SELECT id FROM bots WHERE id=?", made["bot_id"]))


if __name__ == "__main__":
    unittest.main()
