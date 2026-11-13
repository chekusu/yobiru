"""Long-lived task projection stays visible after immutable request outcomes."""
import contextlib
import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path

from support.pkg import REPO

spec = importlib.util.spec_from_file_location("task_core", REPO / "tailmsg.py")
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)


class TaskLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "receiver"
        self.ctx = core.Ctx(self.home)
        self.mid = "0011223344556677"
        self.env = {"v": 1, "id": self.mid, "from": "sender.example.ts.net", "to": "receiver.example.ts.net",
                    "ts": core.iso(core.now_utc()), "kind": "request", "text": "do the thing",
                    "request": {"timeout_seconds": 900, "reply_port": 8123}}
        self.rec = {"envelope": self.env, "received": {"at": self.env["ts"],
                    "peer": {"node": "sender.example.ts.net"}, "agent_status": "known"}}

    def test_blocked_attempt_remains_open_and_resolve_is_explicit(self):
        core.accept_request(self.ctx, self.rec)
        core.respond_request(self.ctx, self.mid, "blocked", "reply permission is disabled")
        open_tasks = core.task_snapshots(self.home, open_only=True)
        self.assertEqual(1, len(open_tasks))
        self.assertEqual(("open", "needs_attention", True),
                         (open_tasks[0]["resolution"], open_tasks[0]["attention"], open_tasks[0]["new_result"]))
        task_id = open_tasks[0]["task_id"]
        self.assertEqual("blocked", core.request_snapshot(self.home, self.mid)["state"])
        resolved = core.resolve_task(self.ctx, task_id)
        self.assertEqual((task_id, "resolved"), (resolved["task_id"], resolved["resolution"]))
        self.assertEqual([], core.task_snapshots(self.home, open_only=True))
        self.assertEqual("blocked", core.request_snapshot(self.home, self.mid)["state"])

    def test_resolve_keeps_its_note_until_the_task_reopens(self):
        core.accept_request(self.ctx, self.rec)
        core.respond_request(self.ctx, self.mid, "blocked", "needs user authorization")
        task_id = core.task_snapshots(self.home)[0]["task_id"]
        resolved = core.resolve_task(self.ctx, task_id, by="guoyu", note="closed while switching routes")
        self.assertEqual("closed while switching routes", resolved["resolution_note"])
        task = core.task_snapshots(self.home)[0]
        self.assertEqual(("resolved", "guoyu", "closed while switching routes"),
                         (task["resolution"], task["resolved_by"], task["resolution_note"]))
        core.authorize_task(self.ctx, task_id, {"actions": ["build"]}, expected_generation=0)
        task = core.task_snapshots(self.home)[0]
        self.assertEqual(("open", None, None), (task["resolution"], task["resolved_by"], task["resolution_note"]))
        self.assertEqual(500, len(core.resolve_task(self.ctx, task_id, note="x" * 600)["resolution_note"]))
        self.assertEqual("x" * 500, core.task_snapshots(self.home)[0]["resolution_note"])
        self.assertIsNone(core.resolve_task(self.ctx, task_id)["resolution_note"])
        self.assertIsNone(core.task_snapshots(self.home)[0]["resolution_note"])

    def test_an_older_ledger_gains_the_note_column(self):
        core.accept_request(self.ctx, self.rec)
        core.respond_request(self.ctx, self.mid, "blocked", "reply permission is disabled")
        task_id = core.task_snapshots(self.home)[0]["task_id"]
        with contextlib.closing(sqlite3.connect(str(self.home / "requests" / "ledger.sqlite3"))) as db:
            columns = [row[1] for row in db.execute("PRAGMA table_info(tasks)") if row[1] != "resolution_note"]
            db.executescript("ALTER TABLE tasks RENAME TO tasks_with_note; "
                             "CREATE TABLE tasks AS SELECT %s FROM tasks_with_note; DROP TABLE tasks_with_note;"
                             % ",".join(columns))
            self.assertNotIn("resolution_note", [row[1] for row in db.execute("PRAGMA table_info(tasks)")])
        self.assertIsNone(core.task_snapshots(self.home)[0]["resolution_note"])
        core.resolve_task(self.ctx, task_id, note="kept after the upgrade")
        self.assertEqual("kept after the upgrade", core.task_snapshots(self.home)[0]["resolution_note"])

    def test_client_ref_reuses_task_and_rejects_different_content(self):
        first = dict(self.env)
        core.save_outgoing_request(self.ctx, first, "receiver.example.ts.net", 9123, "idea-001122")
        second = dict(self.env, id="8899aabbccddeeff", ts=core.iso(core.now_utc()))
        core.save_outgoing_request(self.ctx, second, "receiver.example.ts.net", 9123, "idea-001122")
        rows = core.task_snapshots(self.home)
        self.assertEqual(1, len(rows))
        self.assertEqual(2, rows[0]["attempt_count"])
        changed = dict(self.env, id="ffeeddccbbaa9988", text="different task", ts=core.iso(core.now_utc()))
        with self.assertRaisesRegex(ValueError, "different task"):
            core.save_outgoing_request(self.ctx, changed, "receiver.example.ts.net", 9123, "idea-001122")

    def test_trusted_authorization_generation_and_internal_attempt(self):
        core.accept_request(self.ctx, self.rec)
        core.respond_request(self.ctx, self.mid, "blocked", "needs user authorization")
        task_id = core.task_snapshots(self.home)[0]["task_id"]
        auth = core.authorize_task(self.ctx, task_id, {"actions": ["build"]}, target_agent="codex",
                                   target_host="mac-studio", expected_generation=0)
        self.assertEqual((1, "active"), (auth["generation"], auth["status"]))
        with self.assertRaisesRegex(ValueError, "untrusted"):
            core.authorize_task(self.ctx, task_id, {"actions": ["build"]}, issuer_kind="agent",
                                expected_generation=1)
        attempt = core.create_task_attempt(self.ctx, task_id, auth["authorization_id"], expected_generation=1,
                                           operation_key="build-1", operation_digest="digest", action="build")
        self.assertEqual((2, "pending"), (attempt["attempt_no"], attempt["state"]))
        self.assertEqual("blocked", core.request_snapshot(self.home, self.mid)["state"])
        self.assertEqual("pending", core.task_snapshots(self.home)[0]["latest_state"])
        core.transition_task_attempt(self.ctx, attempt["attempt_id"], "running", expected_generation=1)
        task = core.task_snapshots(self.home)[0]
        self.assertEqual(("running", "open", "none"), (task["latest_state"], task["resolution"], task["attention"]))
        with self.assertRaisesRegex(ValueError, "generation mismatch"):
            core.create_task_attempt(self.ctx, task_id, auth["authorization_id"], expected_generation=0,
                                     operation_key="build-2", operation_digest="d")
        self.assertEqual("needs_attention", core.task_snapshots(self.home)[0]["attention"])

    def test_authorization_revoke_and_operation_idempotency(self):
        core.accept_request(self.ctx, self.rec)
        task_id = core.task_snapshots(self.home)[0]["task_id"]
        auth = core.authorize_task(self.ctx, task_id, {"actions": ["test"]})
        first = core.create_task_attempt(self.ctx, task_id, auth["authorization_id"], expected_generation=1,
                                         operation_key="test-1", operation_digest="same", action="test")
        again = core.create_task_attempt(self.ctx, task_id, auth["authorization_id"], expected_generation=1,
                                         operation_key="test-1", operation_digest="same", action="test")
        self.assertTrue(again["idempotent"])
        self.assertEqual(first["attempt_id"], again["attempt_id"])
        with self.assertRaisesRegex(ValueError, "different attempt"):
            core.create_task_attempt(self.ctx, task_id, auth["authorization_id"], expected_generation=1,
                                     operation_key="test-1", operation_digest="changed", action="test")
        revoked = core.revoke_task_authorization(self.ctx, auth["authorization_id"], expected_generation=1)
        self.assertEqual("revoked", revoked["status"])
        with self.assertRaisesRegex(ValueError, "generation mismatch"):
            core.create_task_attempt(self.ctx, task_id, auth["authorization_id"], expected_generation=2,
                                     operation_key="test-2", operation_digest="d")

    def test_late_old_request_event_does_not_overwrite_new_attempt(self):
        core.accept_request(self.ctx, self.rec)
        task_id = core.task_snapshots(self.home)[0]["task_id"]
        auth = core.authorize_task(self.ctx, task_id, {"actions": ["build"]})
        attempt = core.create_task_attempt(self.ctx, task_id, auth["authorization_id"], expected_generation=1,
                                           operation_key="build-1", operation_digest="d", action="build")
        core.transition_task_attempt(self.ctx, attempt["attempt_id"], "running", expected_generation=1)
        # A late lifecycle event from the generation-zero request remains in its own attempt history.
        with core.request_db(self.ctx) as db:
            db.execute("BEGIN IMMEDIATE")
            core.task_sync(db, "incoming", self.mid, "timed_out")
        snapshot = core.task_snapshots(self.home)[0]
        self.assertEqual(("running", "open"), (snapshot["latest_state"], snapshot["resolution"]))

    def test_expired_authorization_is_persisted_and_keeps_attention_open(self):
        core.accept_request(self.ctx, self.rec)
        task_id = core.task_snapshots(self.home)[0]["task_id"]
        auth = core.authorize_task(self.ctx, task_id, {"actions": ["build"]})
        with core.request_db(self.ctx) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE task_authorizations SET expires_at=? WHERE authorization_id=?",
                       ("2000-01-01T00:00:00Z", auth["authorization_id"]))
        with self.assertRaisesRegex(ValueError, "expired"):
            core.create_task_attempt(self.ctx, task_id, auth["authorization_id"], expected_generation=1,
                                     operation_key="expired", operation_digest="d", action="build")
        self.assertEqual("expired", core.authorization_snapshot(self.home, auth["authorization_id"])["status"])
        self.assertEqual("needs_attention", core.task_snapshots(self.home)[0]["attention"])


if __name__ == "__main__":
    unittest.main()
