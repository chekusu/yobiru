"""Schema 2 -> 3 on real schema-2 databases made by the schema-2 gateway (tests/fixtures/bot_gateway_v2.py): one
transaction, additive only, idempotent, refused by `serve` until it ran, and rolled back completely on failure.
Grants, follows, generations, queued work, evidence and request hashes keep their meaning; no decision, admission
time, budget or attachment is invented for old rows."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from support.pkg import REPO
from support.public_harness import legacy_gateway_prefix
from support.public_harness import PUBLIC, FriendsGateway, plain_env


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="yobiru migration 2-3 ")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.v2dir = self.root / "v2code"
        self.v2dir.mkdir()
        shutil.copy(str(REPO / "tests" / "fixtures" / "bot_gateway_v2.py"), str(self.v2dir / "bot_gateway.py"))
        shutil.copy(str(REPO / "tests" / "fixtures" / "bot_common_v2.py"), str(self.v2dir / "bot_common.py"))

    def v2_gateway(self):
        return FriendsGateway(self.root, prefix=legacy_gateway_prefix(self.v2dir / "bot_gateway.py"),
                              limits={"min_deadline_seconds": 1})

    def run_new(self, home, *args):
        return subprocess.run([sys.executable, str(PUBLIC / "bot_gateway.py"), "--gateway-home", str(home)] + list(args),
                              capture_output=True, text=True, env=plain_env(self.root), timeout=60)

    def test_a_schema_2_database_keeps_its_meaning(self):
        old = self.v2_gateway().start()
        target, source = old.owned_bot("target-bot")
        other, other_source = old.owned_bot("other-bot")
        self.assertEqual(201, old.follow(other_source, target)[0])
        wtoken, wid = old.native(target)
        ctoken = old.ok("POST", "/v1/owner/bots/%s/workers/%s/connector" % (target, wid), {}, old.owner_token)["token"]
        chk = old.ok("POST", "/v1/owner/bots/%s/workers/%s/check" % (target, wid), {}, old.owner_token)["check_id"]
        old.ok("POST", "/v1/connector/checks/%s/wake" % chk, {"step": "intent", "wake_id": "wake-0001"}, ctoken)
        old.ok("POST", "/v1/connector/checks/%s/wake" % chk, {"step": "result", "wake_id": "wake-0001",
                                                              "outcome": "accepted"}, ctoken)
        old.ok("POST", "/v1/worker/checks/%s" % chk, {}, wtoken)
        actor_id, token = old.actor()
        cid = old.conversation(target, token)
        queued = old.chat(cid, token, "asked before the upgrade")
        reviews = old.ok("GET", "/v1/owner/bots/%s/reviews" % target, None, old.owner_token)
        request_hash = [x for x in reviews["inbound"] if x["job_id"] == queued][0]["request_hash"]
        before = {"relation": old.relation(other_source, target)["capabilities"],
                  "evidence": [w for w in old.ok("GET", "/v1/owner/bots/%s" % target, None, old.owner_token)["workers"]
                               if w["worker_id"] == wid][0]["evidence"]["wake_tested_at"]}
        old.stop()
        refused = self.run_new(old.home, "serve", "--port", "0")
        self.assertEqual(1, refused.returncode)
        self.assertIn("schema 2", refused.stderr)
        self.assertIn("run: migrate", refused.stderr)
        migrated = self.run_new(old.home, "migrate")
        self.assertEqual(0, migrated.returncode, migrated.stderr)
        self.assertEqual((True, "8", "2", 2), tuple(json.loads(migrated.stdout)[k] for k in
                                                     ("migrated", "schema", "from_schema", "bots")))
        self.assertFalse(json.loads(self.run_new(old.home, "migrate").stdout)["migrated"], "idempotent")
        new = old
        new.prefix = [sys.executable, str(PUBLIC / "bot_gateway.py")]
        new.start()
        try:
            self.assertEqual(["faq", "leave_message", "chat"], before["relation"])
            self.assertEqual(["faq", "chat"], new.relation(other_source, target)["capabilities"],
                             "the same permissions: a left message is a direct message since schema 8")
            self.assertEqual([], new.sql("SELECT * FROM decisions"), "no decision is invented")
            row = new.sql("SELECT state, request_hash, admitted_ts, admission_decision, hop, automated, "
                          "content_attachment_bytes FROM messages WHERE id=?", (queued,))[0]
            self.assertEqual(("queued", request_hash, None, None, 0, 0, 0),
                             tuple(row[k] for k in ("state", "request_hash", "admitted_ts", "admission_decision", "hop",
                                                    "automated", "content_attachment_bytes")))
            binding = [w for w in new.ok("GET", "/v1/owner/bots/%s" % target, None, new.owner_token)["workers"]
                       if w["worker_id"] == wid][0]
            self.assertEqual(before["evidence"], binding["evidence"]["wake_tested_at"])
            self.assertEqual("unknown", binding["delivery"]["declared"]["tier"], "no declaration is invented")
            self.assertEqual(200, new.call("POST", "/v1/owner/messages/%s/review" % queued,
                                           {"action": "approve_inbound", "request_hash": request_hash},
                                           new.owner_token)[0], "an old request hash is still the one approved")
            status, job = new.claim_job(wtoken, queued)
            self.assertEqual((200, "asked before the upgrade"), (status, job["untrusted_content"]["text"]))
            self.assertTrue(new.ok("GET", "/@target-bot")["chat"]["binding_configured"])
        finally:
            new.stop()

    def test_a_failed_migration_rolls_back_to_schema_2(self):
        old = self.v2_gateway()
        db = old.db()
        try:
            db.execute("ALTER TABLE messages ADD COLUMN hop INTEGER")      # makes one migration statement fail
        finally:
            db.close()
        result = self.run_new(old.home, "migrate")
        self.assertEqual(1, result.returncode)
        self.assertIn("rolled back; schema 2 is unchanged", result.stderr)
        self.assertEqual("2", old.sql("SELECT value FROM meta WHERE key='schema_version'")[0]["value"])
        self.assertNotIn("trust_decision", [c["name"] for c in old.sql("PRAGMA table_info(contacts)")])
        self.assertEqual([], old.sql("SELECT name FROM sqlite_master WHERE name='decisions'"))


if __name__ == "__main__":
    unittest.main()
