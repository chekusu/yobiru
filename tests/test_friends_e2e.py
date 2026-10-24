"""End-to-end encryption of Friends message bodies (gateway schema 5).

The primitives are checked against RFC 7748 and RFC 8439 test vectors (and against the `cryptography` package when
it is installed). Through the CLI, with a separate synthetic home per role: a bot's key is made on the owner device
and handed to its native host next to the binding credential; a caller pins it from the card; chat text, attachments,
drafts, released replies and owner messages reach the gateway only sealed, and each side opens them locally. The
gateway refuses plain replies to sealed messages, bodies sealed for a stale key, and (when the owner requires it)
plain messages; a body the gateway moves to another conversation, or a key it swaps, is reported, never shown.

Real bot_gateway.py subprocesses with synthetic homes on loopback.
"""
import base64
import itertools
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from support.public_harness import C, FriendsGateway, cli

import bot_e2e as E  # noqa: E402  (on sys.path through the harness)

LIMITS = {"min_deadline_seconds": 1, "read_timeout_seconds": 5, "messages_per_minute_per_actor": 1000,
          "messages_per_hour_per_actor": 5000, "pending_per_contact": 100, "new_conversations_per_hour_per_bot": 1000,
          "new_contacts_per_hour_per_bot": 1000, "enroll_per_hour_per_ip": 1000, "enroll_per_hour": 1000,
          "bot_enroll_per_hour": 1000, "bot_enroll_per_hour_per_ip": 1000, "agent_messages_per_hour_per_bot": 1000}

COUNTER = itertools.count(1)


class PrimitiveTests(unittest.TestCase):
    def test_x25519_rfc7748(self):
        k = bytes.fromhex("a546e36bf0527c9d3b16154b82465edd62144c0ac1fc5a18506a2244ba449ac4")
        u = bytes.fromhex("e6db6867583030db3594c1a424b15f7c726624ec26b3353b10a903a6d0ab1c4c")
        self.assertEqual("c3da55379de9c6908e94ea4df28d084f32eccf03491c71f754b4075577a28552", E.x25519(k, u).hex())
        a = bytes.fromhex("77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a")
        b = bytes.fromhex("5dab087e624a8a4b79e17f8b83800ee66f3bb1292618b6fd1c2f8b27ff88e0eb")
        self.assertEqual("8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a", E.public_key(a).hex())
        self.assertEqual("de9edb7d7b7dc1b4d35b61c2ece435373f8343c85b78674dadfc7e146f882b4f", E.public_key(b).hex())
        shared = "4a5d9d5ba4ce2de1728e3bf480350f25e07e21c947d19e3376f09b3c1e161742"
        self.assertEqual(shared, E.x25519(a, E.public_key(b)).hex())
        self.assertEqual(shared, E.x25519(b, E.public_key(a)).hex())
        self.assertEqual("low-order key", E.key_error(E.b64e(bytes(32))))

    def test_chacha20_poly1305_rfc8439(self):
        key = bytes(range(0x80, 0xa0))
        nonce = bytes.fromhex("070000004041424344454647")
        aad = bytes.fromhex("50515253c0c1c2c3c4c5c6c7")
        text = (b"Ladies and Gentlemen of the class of '99: If I could offer you only one tip for the future, "
                b"sunscreen would be it.")
        sealed = E.py_aead_encrypt(key, nonce, text, aad)
        self.assertEqual("d31a8d34648e60db7b86afbc53ef7ec2", sealed[:16].hex())
        self.assertEqual("1ae10b594f09e26a7e902ecbd0600691", sealed[-16:].hex())
        self.assertEqual(text, E.py_aead_decrypt(key, nonce, sealed, aad))
        self.assertEqual(sealed, E.aead_encrypt(key, nonce, text, aad), "the optional fast path gives the same bytes")
        broken = bytearray(sealed)
        broken[5] ^= 1
        with self.assertRaises(E.E2EError):
            E.py_aead_decrypt(key, nonce, bytes(broken), aad)
        with self.assertRaises(E.E2EError):
            E.aead_decrypt(key, nonce, sealed, aad + b"x")
        for n in (0, 1, 63, 64, 65, 200):
            data = os.urandom(n)
            self.assertEqual(E.py_aead_encrypt(key, nonce, data), E.aead_encrypt(key, nonce, data))

    def test_envelopes_authenticate_the_sender_and_bind_the_context(self):
        alice, bob, mallory = E.generate(), E.generate(), E.generate()
        env = E.seal(b"hello", alice[0], [bob[1], alice[1]], "ctx")
        self.assertIsNone(E.envelope_error(env, 100))
        self.assertEqual(b"hello", E.open_sealed(env, bob[0], alice[1], "ctx"))
        self.assertEqual(b"hello", E.open_sealed(env, alice[0], alice[1], "ctx"), "the sender reads its own copy")
        for args in ((env, bob[0], mallory[1], "ctx"), (env, bob[0], alice[1], "other"),
                     (env, mallory[0], alice[1], "ctx")):
            with self.assertRaises(E.E2EError):
                E.open_sealed(*args)
        # mallory knows bob's public key but not alice's secret: her envelope cannot pass as alice's
        forged = E.seal(b"pay me", mallory[0], [bob[1]], "ctx")
        forged["from"] = E.kid(alice[1])
        with self.assertRaises(E.E2EError):
            E.open_sealed(forged, bob[0], alice[1], "ctx")
        seed = E.seed_for(alice[0], "op", "hash")
        self.assertEqual(E.seal(b"x", alice[0], [bob[1]], "c", seed), E.seal(b"x", alice[0], [bob[1]], "c", seed))
        self.assertNotEqual(E.seal(b"x", alice[0], [bob[1]], "c"), E.seal(b"x", alice[0], [bob[1]], "c"))
        self.assertIn("exactly", E.envelope_error(dict(env, extra=1), 100))
        self.assertIn("too large", E.envelope_error(env, 1))
        self.assertIsNotNone(E.envelope_error(dict(env, to=[]), 100))


class E2ECase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="yobiru e2e ")
        cls.root = Path(cls._tmp.name)
        cls.gw = FriendsGateway(cls.root, limits=LIMITS).start()

    @classmethod
    def tearDownClass(cls):
        cls.gw.stop()
        cls._tmp.cleanup()

    def setUp(self):
        self.n = next(COUNTER)
        self.homes = {r: self.root / ("%s-%d" % (r, self.n)) for r in ("owner", "caller", "host")}
        self.files = self.root / ("files-%d" % self.n)
        self.files.mkdir()
        self.handle = "e2e-bot-%d" % self.n

    def y(self, role, *args, expect=0):
        code, obj, err = cli(self.homes[role], *args)
        self.assertEqual(expect, code, "%s %s\n%s\n%s" % (role, args, obj, err))
        return obj if expect == 0 else (obj, err)

    def bot_with_host(self, files=True):
        self.y("owner", "owner", "login", self.gw.url, "--token-file", str(self.gw.home / "owner.token"))
        made = self.y("owner", "bot", "create", "--handle", self.handle, "--name", "Sealed bot")
        self.assertEqual(E.ALG, made["e2e"]["alg"], "a new bot gets an end-to-end key on the owner device")
        self.y("owner", "bot", "publish", self.handle)
        if files:
            self.y("owner", "bot", "edit", self.handle, "--offered", "faq,leave_message,chat,files",
                   "--visitor-capabilities", "faq,leave_message,chat,files")
        token_file = self.root / ("native-%d.token" % self.n)
        bound = self.y("owner", "worker", "bind", self.handle, "--adapter", "native", "--token-file", str(token_file))
        key_file = Path(bound["e2e_key_file"])
        self.assertEqual((str(token_file) + ".e2e", 0o600), (str(key_file), stat.S_IMODE(key_file.stat().st_mode)))
        logged = self.y("host", "worker", "login", self.gw.url, "--token-file", str(token_file))
        self.assertEqual(str(key_file), logged["e2e_key_imported_from"])
        return made["bot_id"], made["e2e"]

    def stored(self, mid):
        return self.gw.sql("SELECT content, draft, result FROM messages WHERE id=?", (mid,))[0]

    def assert_opaque(self, row, *plain):
        dump = json.dumps(row)
        for text in plain:
            self.assertNotIn(text, dump, "the gateway stores no plaintext")
            self.assertNotIn(base64.b64encode(text.encode()).decode()[:12], dump)


class SealedFlowTests(E2ECase):
    def test_chat_with_files_review_reply_and_owner_message_are_sealed_end_to_end(self):
        bot_id, key = self.bot_with_host()
        connected = self.y("caller", "connect", "%s/@%s" % (self.gw.url, self.handle), "--name", "Sealed caller")
        self.assertEqual(("pinned", key["key"]), (connected["bot"]["e2e_pin"], connected["bot"]["e2e"]["key"]))
        (self.files / "question.md").write_text("secret question file\n")
        self.y("caller", "attachments", "allow", str(self.files))
        sent = self.y("caller", "chat", self.handle, "the launch code is 4711",
                      "--attach", str(self.files / "question.md"))
        self.assertTrue(sent["e2e"].startswith("on:"), sent)
        mid = sent["message_id"]
        self.assert_opaque(self.stored(mid), "4711", "secret question file")
        content = json.loads(self.stored(mid)["content"])
        self.assertEqual({"sealed", "sealed_files", "files"}, set(content))
        self.assertEqual({"count": 1, "bytes": len("secret question file\n")}, content["files"])
        self.assertEqual([E.kid(key["key"])], [k for k in E.recipients_of(content["sealed"]) if k == key["kid"]])

        self.y("owner", "review", "approve-inbound", self.handle, mid)
        revealed = self.y("owner", "review", "reveal", self.handle, mid)
        self.assertEqual("the launch code is 4711", revealed["content"]["text"])
        self.assertTrue(revealed["content"]["e2e"]["opened"])
        job = self.y("host", "job", "claim", bot_id, mid)
        self.assertEqual("the launch code is 4711", job["untrusted_content"]["text"])
        self.assertEqual("secret question file\n", job["untrusted_content"]["attachments"][0]["content"])
        self.assertEqual(job["from"]["principal_id"], job["untrusted_content"]["e2e"]["sealed_by"])

        drafted = self.y("host", "job", "reply", bot_id, mid, "--text", "noted: 4711 received")
        self.assertEqual("review", drafted["state"])
        self.assert_opaque(self.stored(mid), "4711", "noted")
        replies = [x for x in self.y("owner", "review", "list", self.handle)["replies"] if x["job_id"] == mid]
        self.assertEqual("noted: 4711 received", replies[0]["draft"]["text"])
        self.assertEqual(bot_id, replies[0]["draft"]["e2e"]["sealed_by"])
        self.y("owner", "review", "approve-reply", self.handle, mid, "--draft-hash", replies[0]["draft_hash"])
        status = self.y("caller", "status", mid)
        self.assertEqual(("completed", "noted: 4711 received"), (status["state"], status["result"]["text"]))
        self.assertEqual("the launch code is 4711", status["content"]["text"], "the caller reads its own copy")
        self.assertTrue(self.y("caller", "verify", mid)["verified"], "the release decision covers the sealed reply")

        cid = status["conversation_id"]
        self.y("owner", "owner", "reply", cid, "owner here: keep 4711 private")
        history = self.y("caller", "history", self.handle)
        owner_msgs = [m for m in history["messages"] if m["author"] == "owner"]
        self.assertEqual("owner here: keep 4711 private", owner_msgs[0]["content"]["text"])
        self.assertEqual(bot_id, owner_msgs[0]["content"]["e2e"]["sealed_by"])
        for row in self.gw.sql("SELECT content, draft, result FROM messages WHERE conversation_id=?", (cid,)):
            self.assert_opaque(row, "4711", "owner here")

    def test_a_follow_up_turn_carries_the_sealed_transcript(self):
        bot_id, _ = self.bot_with_host(files=False)
        self.y("caller", "connect", "%s/@%s" % (self.gw.url, self.handle))
        first = self.y("caller", "chat", self.handle, "first turn: blue")["message_id"]
        self.y("owner", "review", "approve-inbound", self.handle, first)
        self.y("host", "job", "claim", bot_id, first)
        self.y("host", "job", "reply", bot_id, first, "--text", "blue noted")
        draft = self.y("owner", "review", "list", self.handle)["replies"][0]
        self.y("owner", "review", "approve-reply", self.handle, first, "--draft-hash", draft["draft_hash"])
        second = self.y("caller", "chat", self.handle, "second turn: what colour?")["message_id"]
        self.y("owner", "review", "approve-inbound", self.handle, second)
        job = self.y("host", "job", "claim", bot_id, second)
        self.assertEqual([("caller", "first turn: blue"), ("bot", "blue noted")],
                         [(t["role"], t["text"]) for t in job["transcript"]])
        self.assertTrue(all(t["e2e"]["opened"] for t in job["transcript"]))

    def test_the_owners_decline_note_is_sealed_for_the_caller(self):
        bot_id, _ = self.bot_with_host(files=False)
        self.y("caller", "connect", "%s/@%s" % (self.gw.url, self.handle))
        mid = self.y("caller", "chat", self.handle, "may I?")["message_id"]
        self.y("owner", "review", "reject-inbound", self.handle, mid, "--text", "not now, sorry")
        self.assert_opaque(self.stored(mid), "not now")
        status = self.y("caller", "status", mid)
        self.assertEqual(("declined", "not now, sorry"), (status["state"], status["result"]["text"]))

    def test_the_deterministic_responder_opens_and_seals_too(self):
        self.y("owner", "owner", "login", self.gw.url, "--token-file", str(self.gw.home / "owner.token"))
        bot_id = self.y("owner", "bot", "create", "--handle", self.handle, "--name", "Sealed desk")["bot_id"]
        self.y("owner", "bot", "publish", self.handle)
        token_file = self.root / ("worker-%d.token" % self.n)
        self.y("owner", "worker", "bind", self.handle, "--token-file", str(token_file))
        self.y("host", "worker", "login", self.gw.url, "--token-file", str(token_file))
        self.y("host", "responder", "init", bot_id)
        self.y("caller", "connect", "%s/@%s" % (self.gw.url, self.handle))
        mid = self.y("caller", "chat", self.handle, "call me at 555-0100?", "--faq")["message_id"]
        run = self.y("host", "worker", "run", bot_id, "--once")
        self.assertEqual(["completed"], [p["outcome"] for p in run["processed"]], run)
        self.assert_opaque(self.stored(mid), "555-0100", "send the owner a message")
        status = self.y("caller", "status", mid)
        self.assertIn("send the owner a message", status["result"]["text"])
        self.assertTrue(status["result"]["e2e"]["opened"])

    def test_a_self_enrolled_bot_and_a_visitor_get_keys_at_enrollment(self):
        made = self.y("caller", "enroll-bot", self.gw.url, "--handle", self.handle, "--name", "Self enrolled")
        creds = json.loads((self.homes["caller"] / "public" / "credentials.json").read_text())["gateways"][self.gw.url]
        mine = creds["bot_keys"][made["bot_id"]][-1]["public"]
        shown = self.gw.ok("GET", "/v1/owner/bots/%s" % made["bot_id"], None, creds["owner"]["token"])["e2e"]
        self.assertEqual(mine, shown["key"])
        self.y("host", "enroll", self.gw.url, "--name", "Keyed visitor")
        actor = json.loads((self.homes["host"] / "public" / "credentials.json").read_text())["gateways"][
            self.gw.url]["actor"]
        me = self.gw.ok("GET", "/v1/actor", None, actor["token"])
        self.assertEqual(actor["e2e_keys"][-1]["public"], me["e2e"]["key"])
        status = self.y("caller", "e2e", "status")
        self.assertEqual([made["bot_id"]], [k["principal"] for k in status["gateways"][0]["my_keys"]])


class GatewayGuardTests(E2ECase):
    def test_plain_replies_stale_keys_and_required_e2e_are_refused(self):
        bot_id, key = self.bot_with_host(files=False)
        self.y("caller", "connect", "%s/@%s" % (self.gw.url, self.handle))
        mid = self.y("caller", "chat", self.handle, "sealed hello")["message_id"]
        self.y("owner", "review", "approve-inbound", self.handle, mid)
        wtoken, _ = self.gw.native(bot_id, replace=True)
        status, job = self.gw.claim_job(wtoken, mid)
        self.assertEqual(200, status, job)
        status, obj = self.gw.reply_job(wtoken, job, "a plain reply")
        self.assertEqual((409, "e2e_required"), (status, obj["error"]), "a sealed message never gets a plain answer")
        # a body sealed for some other key than the bot's current one
        _, actor = self.gw.actor()
        cid = self.gw.conversation(bot_id, actor)
        stranger = E.generate()
        env = E.seal(b'{"text":"x"}', stranger[0], [stranger[1]], "yobiru-e2e/1")
        status, obj = self.gw.call("POST", "/v1/conversations/%s/messages" % cid,
                                   {"op_key": "sealed-wrong-1", "kind": "chat", "sealed": env}, actor)
        self.assertEqual((409, "sealed_recipient_missing"), (status, obj["error"]))
        status, obj = self.gw.call("POST", "/v1/conversations/%s/messages" % cid,
                                   {"op_key": "sealed-bad-1", "kind": "chat", "sealed": {"v": 1}}, actor)
        self.assertEqual((400, "invalid_sealed"), (status, obj["error"]))
        self.assertTrue(self.y("owner", "bot", "edit", self.handle, "--require-e2e", "on")["policy"]["require_e2e"])
        status, obj = self.gw.send(cid, actor, kind="chat", text="plain text")
        self.assertEqual((403, "e2e_required"), (status, obj["error"]))
        ok = E.seal(b'{"text":"x"}', stranger[0], [key["key"]], "yobiru-e2e/1")
        status, obj = self.gw.call("POST", "/v1/conversations/%s/messages" % cid,
                                   {"op_key": "sealed-ok-1", "kind": "chat", "sealed": ok}, actor)
        self.assertEqual(202, status, obj)

    def test_a_bot_without_a_key_keeps_working_in_plain_text(self):
        legacy = self.gw.bot("e2e-legacy-%d" % self.n)
        self.assertIsNone(self.gw.ok("GET", "/v1/bots/%s" % legacy)["e2e"])
        connected = self.y("caller", "connect", "%s/@e2e-legacy-%d" % (self.gw.url, self.n))
        self.assertNotIn("e2e_pin", connected["bot"])
        sent = self.y("caller", "chat", "e2e-legacy-%d" % self.n, "plain as before")
        self.assertTrue(sent["e2e"].startswith("off:"), sent)
        self.assertEqual("plain as before", json.loads(self.stored(sent["message_id"])["content"])["text"])
        _, actor = self.gw.actor()
        cid = self.gw.conversation(legacy, actor)
        env = E.seal(b"{}", E.generate()[0], [E.generate()[1]], "yobiru-e2e/1")
        status, obj = self.gw.call("POST", "/v1/conversations/%s/messages" % cid,
                                   {"op_key": "sealed-legacy", "kind": "chat", "sealed": env}, actor)
        self.assertEqual((409, "e2e_unavailable"), (status, obj["error"]))


class TamperTests(E2ECase):
    def test_a_moved_body_or_a_swapped_key_is_reported_not_shown(self):
        bot_id, _ = self.bot_with_host(files=False)
        self.y("caller", "connect", "%s/@%s" % (self.gw.url, self.handle))
        first = self.y("caller", "chat", self.handle, "for conversation one")
        second = self.y("caller", "chat", self.handle, "for conversation two", "--new-conversation")
        self.assertNotEqual(first["conversation_id"], second["conversation_id"])
        # the gateway (or someone with its database) copies a sealed body into another conversation
        row = self.stored(first["message_id"])
        db = self.gw.db()
        try:
            db.execute("UPDATE messages SET content=? WHERE id=?", (row["content"], second["message_id"]))
        finally:
            db.close()
        moved = self.y("caller", "status", second["message_id"])
        self.assertIsNone(moved["content"]["text"])
        self.assertEqual((False, "unverified"), (moved["content"]["e2e"]["opened"], moved["content"]["e2e"]["error"]))
        self.assertIn("not to this view", moved["content"]["e2e"]["detail"])
        # or presents the caller's own sealed message as the bot's answer to it
        db = self.gw.db()
        try:
            db.execute("UPDATE messages SET result=content, state='completed', finished_ts=created_ts WHERE id=?",
                       (first["message_id"],))
        finally:
            db.close()
        echoed = self.y("caller", "status", first["message_id"], "--no-ack")
        self.assertIsNone(echoed["result"]["text"])
        self.assertIn("shown where a reply belongs", echoed["result"]["e2e"]["detail"])

        # the owner rotates the key: the caller's pinned key no longer reaches the bot until it trusts the new one
        self.y("owner", "e2e", "enable", self.handle, "--rotate")
        refused, _ = self.y("caller", "chat", self.handle, "after rotation", expect=2)
        self.assertEqual("sealed_recipient_missing", refused["error"])
        self.assertIn("e2e trust", refused["note"])
        refreshed = self.y("caller", "contacts", "--refresh")["contacts"]
        self.assertEqual(["key_changed"], [c["e2e"] for c in refreshed])
        trusted = self.y("caller", "e2e", "trust", self.handle)
        self.assertEqual("pinned", trusted["pinned"])
        resent = self.y("caller", "resend", refused["op_key"])   # sealed again, now for the trusted key
        self.assertTrue(resent["e2e"].startswith("on:"))
        sent = self.y("caller", "chat", self.handle, "after trust", "--new-conversation")
        self.assertTrue(sent["e2e"].startswith("on:"))
        # the host imported the old key only: it reports what it cannot open instead of showing anything
        self.y("owner", "review", "approve-inbound", self.handle, sent["message_id"])
        job = self.y("host", "job", "claim", bot_id, sent["message_id"])
        self.assertEqual("not_sealed_for_this_device", job["untrusted_content"]["e2e"]["error"])
        export = self.root / ("rotated-%d.e2e" % self.n)
        self.y("owner", "e2e", "export", bot_id, "--file", str(export))
        self.y("host", "e2e", "import", "--file", str(export))
        shown = self.y("host", "job", "show", bot_id, sent["message_id"])
        self.assertEqual("after trust", shown["untrusted_content"]["text"])
        status = self.y("host", "e2e", "status")
        self.assertEqual(1, status["gateways"][0]["my_keys"][0]["older_keys"])


if __name__ == "__main__":
    unittest.main()
