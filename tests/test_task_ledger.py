"""Long-lived task projection stays visible after immutable request outcomes."""
import importlib.util
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


if __name__ == "__main__":
    unittest.main()
