"""Files contacts: an accepted contact the owner explicitly granted files (such as the owner's own paired scanner)
gets its own, larger per-caller limits for waiting messages, sending rate and attachment bytes. Strangers and
contacts without files keep the general limits. The gateway publishes what one message may carry, and a sealed
field holds a full 8 MiB of attachments.

Real bot_gateway.py subprocess, synthetic identities and files in a temporary directory, loopback.
"""
import hashlib
import tempfile
import unittest
from pathlib import Path

from support.public_harness import FriendsGateway

import bot_e2e as E  # noqa: E402

LIMITS = {"min_deadline_seconds": 1, "read_timeout_seconds": 5, "messages_per_minute_per_actor": 1000,
          "messages_per_hour_per_actor": 5000, "pending_per_contact": 2, "files_contact_pending": 5,
          "new_conversations_per_hour_per_bot": 1000, "new_contacts_per_hour_per_bot": 1000,
          "enroll_per_hour_per_ip": 1000, "enroll_per_hour": 1000, "bot_enroll_per_hour": 1000,
          "bot_enroll_per_hour_per_ip": 1000, "agent_messages_per_hour_per_bot": 1000,
          "attachment_bytes_per_day_per_caller": 500, "attachment_retained_bytes_per_caller": 300,
          "files_contact_attachment_bytes_per_day": 5000, "files_contact_attachment_retained_bytes": 5000}
POLICY = {"offered": ["faq", "chat", "files"], "visitor": ["faq", "chat"]}


class FilesContactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="yobiru files contact test ")
        cls.gw = FriendsGateway(Path(cls._tmp.name), limits=LIMITS).start()

    @classmethod
    def tearDownClass(cls):
        cls.gw.stop()
        cls._tmp.cleanup()

    def bot(self):
        bot_id = self.gw.bot("f%s" % hashlib.sha256(self.id().encode()).hexdigest()[:12])
        self.gw.policy(bot_id, **POLICY)
        return bot_id

    def admit_with_files(self, bot_id, actor_id):
        """What the Mokutori plugin does when the owner confirms a device: grant chat and files, then accept."""
        self.assertEqual(200, self.gw.contact(bot_id, actor_id, "grant", caps=["chat", "files"])[0])
        self.assertEqual(200, self.gw.contact(bot_id, actor_id, "trust")[0])

    def chat(self, cid, token, size=0):
        extra = {"attachments": [{"name": "page.md", "content": "x" * size}]} if size else {}
        return self.gw.send(cid, token, kind="chat", text="scan", **extra)

    def reply(self, wtoken, job, size):
        return self.gw.call("POST", "/v1/worker/messages/%s/finish" % job["job_id"],
                            {"lease_id": job["lease"]["lease_id"], "fence": job["lease"]["fence"],
                             "outcome": "completed", "reply": {"text": "received", "attachments": [
                                 {"name": "receipt.md", "content": "x" * size}]}}, wtoken)

    def test_a_stranger_keeps_the_general_waiting_limit(self):
        bot_id = self.bot()
        _, token = self.gw.actor()
        cid = self.gw.conversation(bot_id, token)
        for _ in range(2):
            self.assertEqual(202, self.chat(cid, token)[0])
        status, body = self.chat(cid, token)
        self.assertEqual((429, "too_many_pending"), (status, body.get("error")))

    def test_a_files_contact_may_keep_more_messages_waiting(self):
        bot_id = self.bot()
        actor_id, token = self.gw.actor()
        self.admit_with_files(bot_id, actor_id)
        cid = self.gw.conversation(bot_id, token)
        for _ in range(5):
            self.assertEqual(202, self.chat(cid, token)[0])
        status, body = self.chat(cid, token)
        self.assertEqual((429, "too_many_pending"), (status, body.get("error")))

    def test_a_chat_only_contact_keeps_the_general_limits(self):
        bot_id = self.bot()
        actor_id, token = self.gw.actor()
        self.assertEqual(200, self.gw.contact(bot_id, actor_id, "grant", caps=["chat"])[0])
        self.assertEqual(200, self.gw.contact(bot_id, actor_id, "trust")[0])
        cid = self.gw.conversation(bot_id, token)
        for _ in range(2):
            self.assertEqual(202, self.chat(cid, token)[0])
        self.assertEqual(429, self.chat(cid, token)[0])

    def test_a_files_contact_has_its_own_attachment_quota(self):
        bot_id = self.bot()
        actor_id, token = self.gw.actor()
        self.admit_with_files(bot_id, actor_id)
        cid = self.gw.conversation(bot_id, token)
        # 3 x 400 bytes is over the general 500 per day and 300 retained, within the files contact's 5000.
        for _ in range(3):
            self.assertEqual(202, self.chat(cid, token, size=400)[0])

    def test_attachment_replies_use_the_files_contacts_shared_retained_ceiling(self):
        bot_id = self.bot()
        actor_id, token = self.gw.actor()
        self.admit_with_files(bot_id, actor_id)
        wtoken, _ = self.gw.native(bot_id)
        cid = self.gw.conversation(bot_id, token)
        status, sent = self.chat(cid, token, size=400)
        self.assertEqual(202, status, sent)
        status, job = self.gw.claim_job(wtoken, sent["message_id"])
        self.assertEqual(200, status, job)
        status, reply = self.reply(wtoken, job, 1)
        self.assertEqual(200, status, reply)
        self.assertEqual("completed", reply["state"])
        self.assertEqual(200, self.reply(wtoken, job, 1)[0], "a duplicate reply is free")

        # Uploads and replies share the caller's 5000 retained bytes, including a released reply.
        status, sent = self.chat(cid, token, size=4599)
        self.assertEqual(202, status, sent)
        status, job = self.gw.claim_job(wtoken, sent["message_id"])
        self.assertEqual(200, status, job)
        status, reply = self.reply(wtoken, job, 1)
        self.assertEqual((429, "attachment_quota", "retained"),
                         (status, reply.get("error"), reply.get("scope")))

    def test_attachment_replies_need_an_explicit_files_grant_for_the_roomier_ceiling(self):
        bot_id = self.bot()
        self.gw.policy(bot_id, offered=["faq", "chat", "files"], visitor=["faq", "chat", "files"])
        actor_id, token = self.gw.actor()
        self.assertEqual(200, self.gw.contact(bot_id, actor_id, "trust")[0])
        wtoken, _ = self.gw.native(bot_id)
        cid = self.gw.conversation(bot_id, token)
        status, sent = self.chat(cid, token, size=300)
        self.assertEqual(202, status, sent)
        status, job = self.gw.claim_job(wtoken, sent["message_id"])
        self.assertEqual(200, status, job)
        status, reply = self.reply(wtoken, job, 1)
        self.assertEqual((429, "attachment_quota", "retained"),
                         (status, reply.get("error"), reply.get("scope")))

    def test_the_gateway_publishes_message_limits(self):
        limits = self.gw.ok("GET", "/.well-known/yobiru")["limits"]
        self.assertEqual(8, limits["attachment_max_files"])
        self.assertEqual(2, limits["pending_per_contact"])
        self.assertEqual(5, limits["files_contact"]["pending"])
        self.assertEqual(5000, limits["files_contact"]["attachment_bytes_per_day"])
        self.assertEqual(5000, limits["files_contact"]["attachment_retained_bytes"])
        self.assertNotIn("sealed_field_chars", limits)

    def test_published_files_byte_limits_respect_shared_bot_and_gateway_ceilings(self):
        cases = [("defaults", None, 256 * 1024 * 1024, 512 * 1024 * 1024),
                 ("shared", dict(LIMITS, attachment_bytes_per_day_per_bot=2000,
                                 attachment_retained_bytes_per_bot=3500, attachment_retained_bytes_total=2500),
                  2000, 2500)]
        for name, configured, daily, retained in cases:
            with self.subTest(name=name):
                gw = FriendsGateway(Path(self._tmp.name), name="limits-" + name, limits=configured).start()
                try:
                    limits = gw.ok("GET", "/.well-known/yobiru")["limits"]["files_contact"]
                    self.assertEqual(daily, limits["attachment_bytes_per_day"])
                    self.assertEqual(retained, limits["attachment_retained_bytes"])
                finally:
                    gw.stop()

    def test_a_sealed_field_holds_a_full_eight_mebibyte_message(self):
        sender, recipient = E.generate(), E.generate()
        plaintext = b"x" * (11 * 1024 * 1024)  # 8 MiB of attachments as JSON with base64 data
        envelope = E.seal(plaintext, sender[0], [recipient[1]], {"field": "files"})
        self.assertGreater(len(envelope["ct"]), 1 << 22)
        self.assertIsNone(E.envelope_error(envelope, 2 * 8 * 1024 * 1024 + 8 * 464))
        self.assertEqual(plaintext, E.open_sealed(envelope, recipient[0], sender[1], {"field": "files"}))


if __name__ == "__main__":
    unittest.main()
