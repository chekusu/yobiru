"""Synthetic Claude only: durable launch/recovery and installation preservation."""
import argparse
import copy
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

from support.pkg import REPO, snapshot, Package

sys.path.insert(0, str(REPO))
import claude_bridge as bridge
import install_claude_bridge as installer

FAKE = r'''import json, os, sys, time
from pathlib import Path
root = Path.cwd()
args = sys.argv[1:]
with (root / "calls.jsonl").open("a") as stream:
    stream.write(json.dumps(args) + "\n")
mode = (root / "mode").read_text() if (root / "mode").exists() else "success"
if args[:2] == ["auth", "status"]:
    print(json.dumps({"loggedIn": mode != "auth"})); sys.exit(0)
if args[:1] == ["agents"]:
    if mode == "agents_error": sys.exit(7)
    print((root / "agents.json").read_text() if (root / "agents.json").exists() else "[]"); sys.exit(0)
sid = args[args.index("--session-id") + 1]
state = json.loads((root / "bridge" / "state.json").read_text())
assert state["runs"][sid]["phase"] == "intent", "intent was not durable before launch"
assert "--bg" in args and "--remote-control" in args and "-p" not in args
assert args[args.index("--permission-mode") + 1] == "manual"
if mode in ("nonzero", "timeout"): 
    if mode == "timeout": time.sleep(2)
    sys.exit(9)
(root / "agents.json").write_text(json.dumps([{"id": sid[:8], "sessionId": sid, "status": "idle"}]))
if mode == "timeout_seen": time.sleep(2)
if mode == "nonzero_seen": sys.exit(9)
print("backgrounded " + sid[:8])
'''


class BridgeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home = self.root / "bridge"
        self.home.mkdir(mode=0o700)
        (self.home / "wake").mkdir()
        self.data = self.root / "data"
        (self.data / "inbox").mkdir(parents=True)
        self.fake = self.root / "fake-claude"
        self.fake.write_text("#!" + sys.executable + "\n" + FAKE)
        self.fake.chmod(0o700)
        self.instructions = self.root / "instructions.md"
        self.instructions.write_text("Trusted dispatcher instruction")
        self.cfg = {"version": 1, "installation_id": str(uuid.uuid4()), "bridge_home": str(self.home),
                    "tailmsg_home": str(self.data), "agent": "claude", "claude_path": str(self.fake),
                    "workdir": str(self.root), "plugin_root": str(REPO / "plugins/yobiru"),
                    "instructions": str(self.instructions), "python": sys.executable,
                    "entry": str(REPO / "claude_bridge.py"), "command_timeout": 1, "batch_delay_seconds": 0}
        bridge.atomic_json(self.home / "state.json", bridge.empty_state(self.cfg["installation_id"]))
        self.b = bridge.Bridge(self.cfg)
        self.mid = "0123456789abcdef"
        self.record(self.mid)

    def record(self, mid, **changes):
        rec = {"envelope": {"id": mid, "from": "sender.example.ts.net", "to": "receiver.example.ts.net",
                            "kind": "text", "text": "UNTRUSTED BODY MUST NOT ENTER LAUNCH PROMPT"},
               "received": {"at": "2026-01-01T00:00:00Z", "agent": "claude", "wake": {"effective": "now"}}}
        rec.update(changes)
        (self.data / "inbox" / (mid + ".json")).write_text(json.dumps(rec))
        return rec

    def calls(self):
        path = self.root / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def launches(self):
        return [c for c in self.calls() if "--bg" in c]

    def mode(self, value):
        (self.root / "mode").write_text(value)

    def test_durable_intent_visible_manual_session_and_duplicate_restart(self):
        result = self.b.step()
        self.assertEqual("dispatched", result["state"])
        self.assertFalse(result["message_handled"])
        self.assertEqual("idle", bridge.Bridge(self.cfg).step()["state"])
        self.assertEqual(1, len(self.launches()))
        self.assertNotIn("UNTRUSTED BODY", " ".join(self.launches()[0]))
        self.assertFalse(any(c and c[0] in ("stop", "kill", "rm") for c in self.calls()))
        self.assertNotIn("acked_at", json.loads((self.data / "inbox" / (self.mid + ".json")).read_text()))

    def test_auth_failure_records_no_launch_intent_then_can_recover(self):
        self.mode("auth")
        self.assertEqual("auth_required", self.b.step()["state"])
        self.assertEqual({}, self.b.load_state()["runs"])
        self.assertEqual([], self.launches())
        self.mode("success")
        self.assertEqual("dispatched", self.b.step()["state"])

    def test_timeout_or_nonzero_without_exact_session_never_relaunch(self):
        for mode in ("timeout", "nonzero"):
            with self.subTest(mode=mode):
                self.mode(mode)
                self.assertEqual("uncertain", self.b.step()["state"])
                self.mode("success")
                self.assertEqual("uncertain", bridge.Bridge(self.cfg).step()["state"])
                self.assertEqual(1, len(self.launches()))
                # Explicit fixture reset is not a product operation.
                bridge.atomic_json(self.home / "state.json", bridge.empty_state(self.cfg["installation_id"]))
                (self.root / "calls.jsonl").unlink()

    def test_timeout_with_exact_session_reconciles_without_second_launch(self):
        self.mode("timeout_seen")
        result = self.b.step()
        self.assertEqual("dispatched", result["state"])
        self.assertEqual("timeout", result["launch_exit"])
        self.assertEqual("idle", bridge.Bridge(self.cfg).step()["state"])
        self.assertEqual(1, len(self.launches()))

    def test_crash_after_intent_before_spawn_stays_uncertain(self):
        real = self.b.call
        def crash(args, timeout=15):
            if "--bg" in args: raise SystemExit("simulated crash")
            return real(args, timeout)
        with patch.object(self.b, "call", side_effect=crash), self.assertRaises(SystemExit):
            self.b.step()
        self.assertEqual("intent", next(iter(self.b.load_state()["runs"].values()))["phase"])
        self.assertEqual("uncertain", bridge.Bridge(self.cfg).step()["state"])
        self.assertEqual([], self.launches())

    def test_crash_after_creation_reconciles_persisted_uuid_on_restart(self):
        real = self.b.save
        calls = []
        def fail_second(state):
            calls.append(1)
            if len(calls) == 2: raise OSError("simulated disk failure after launch")
            real(state)
        with patch.object(self.b, "save", side_effect=fail_second), self.assertRaises(OSError):
            self.b.step()
        self.assertEqual("intent", next(iter(self.b.load_state()["runs"].values()))["phase"])
        self.assertEqual("idle", bridge.Bridge(self.cfg).step()["state"])
        self.assertEqual(1, len(self.launches()))

    def test_corrupt_missing_and_wrong_identity_state_fail_closed(self):
        for data in (b"{broken", b"{}", json.dumps(bridge.empty_state(str(uuid.uuid4()))).encode()):
            (self.home / "state.json").write_bytes(data)
            before = snapshot(self.root)
            with self.assertRaises((bridge.BridgeError, ValueError)):
                self.b.status()
            with self.assertRaises((bridge.BridgeError, ValueError)):
                self.b.step()
            self.assertEqual([], self.launches())
            self.assertEqual(data, (self.home / "state.json").read_bytes())
        (self.home / "state.json").unlink()
        with self.assertRaises(bridge.BridgeError): self.b.step()
        self.assertFalse((self.home / "state.json").exists())

    def test_read_only_status_does_not_create_home_lock_or_call_claude(self):
        before = snapshot(self.root)
        self.assertFalse(self.b.status()["changed"])
        self.assertEqual(before, snapshot(self.root))
        cfg = dict(self.cfg, bridge_home=str(self.root / "absent"))
        with self.assertRaises(bridge.BridgeError): bridge.Bridge(cfg).status()
        self.assertFalse((self.root / "absent").exists())
        self.assertEqual([], self.calls())

    def test_writer_lock_excludes_another_dispatcher(self):
        with bridge.writer_lock(self.home):
            with self.assertRaises(bridge.BridgeError): self.b.step()
        self.assertEqual([], self.calls())

    def test_real_request_ledger_refuses_expired_final_or_running_requests(self):
        spec = importlib.util.spec_from_file_location("claude_test_core", str(REPO / "tailmsg.py"))
        core = importlib.util.module_from_spec(spec); spec.loader.exec_module(core)
        ctx = core.Ctx(self.data)
        for index, state in enumerate(("expired", "completed", "running")):
            mid = "%016x" % (index + 1)
            ts = core.now_utc() - dt.timedelta(seconds=20 if state == "expired" else 0)
            env = {"v": 1, "id": mid, "from": "sender.example.ts.net", "to": "receiver.example.ts.net",
                   "ts": core.iso(ts), "kind": "request", "text": "synthetic", "agent": "claude",
                   "request": {"timeout_seconds": 10, "reply_port": 8123}}
            rec = self.record(mid, envelope=env)
            rec["received"]["peer"] = {"node": env["from"]}
            core.accept_request(ctx, rec)
            if state != "expired": core.respond_request(ctx, mid, state, "synthetic")
        (self.data / "inbox" / (self.mid + ".json")).unlink()
        result = self.b.step()
        self.assertEqual("idle", result["state"])
        self.assertEqual(3, len(result["refused"]))
        self.assertEqual([], self.launches())


class InstallTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.data = self.root / "data"
        self.data.mkdir()
        self.target = self.root / "bridge"
        self.original = {"port": 8123, "allow": ["trusted"], "custom": {"keep": True},
                         "agents": {"codex": {"wake_max": "none"}}}
        (self.data / "config.json").write_text(json.dumps(self.original))
        self.args = argparse.Namespace(plugin_root=str(REPO / "plugins/yobiru"), tailmsg_home=str(self.data),
                                       bridge_home=str(self.target), agent=None, claude_path=sys.executable,
                                       workdir=str(self.root), permission_mode=None, apply=False)

    def test_preview_writes_nothing_and_apply_freezes_plugin_without_starting_anything(self):
        before = snapshot(self.root)
        plan = installer.install(self.args)
        self.assertFalse(plan["applied"])
        self.assertFalse(plan["starts_service"])
        self.assertEqual(before, snapshot(self.root))
        self.args.apply = True
        result = installer.install(self.args)
        cfg = bridge.read_json(self.target / "config.json")
        self.assertEqual("manual", cfg["permission_mode"])
        self.assertTrue(Path(cfg["entry"]).is_file())
        self.assertTrue((Path(cfg["plugin_root"]) / ".claude-plugin/plugin.json").is_file())
        self.assertEqual(self.original, bridge.read_json(Path(result["backup"]) / "tailmsg-config.json"))
        current = bridge.read_json(self.data / "config.json")
        self.assertEqual(self.original["custom"], current["custom"])
        self.assertEqual(self.original["agents"]["codex"], current["agents"]["codex"])
        self.assertEqual({}, bridge.Bridge(cfg).status()["runs"])
        for path in (self.target / "services").glob("*.plist"):
            self.assertIn("daemon", plistlib.loads(path.read_bytes())["ProgramArguments"])
        self.assertFalse((self.root / "calls.jsonl").exists())

    def test_upgrade_preserves_launch_state_local_instructions_and_backup(self):
        self.args.apply = True
        installer.install(self.args)
        cfg = bridge.read_json(self.target / "config.json")
        state = bridge.empty_state(cfg["installation_id"])
        state["runs"][str(uuid.uuid4())] = {"ids": ["0123456789abcdef"], "phase": "uncertain"}
        bridge.atomic_json(self.target / "state.json", state)
        (self.target / "CLAUDE_DISPATCHER.md").write_text("custom owner instructions")
        installer.install(self.args)
        self.assertEqual(state, bridge.read_json(self.target / "state.json"))
        self.assertEqual("custom owner instructions", (self.target / "CLAUDE_DISPATCHER.md").read_text())
        self.assertEqual(2, len(list((self.target / "backups").iterdir())))

    def test_existing_adapter_and_corrupt_bridge_state_are_not_replaced(self):
        original = copy.deepcopy(self.original)
        original["agents"]["claude"] = {"wake_adapter": {"type": "exec", "argv": ["/custom/live/bridge"]}}
        (self.data / "config.json").write_text(json.dumps(original))
        self.args.apply = True
        with self.assertRaises(bridge.BridgeError): installer.install(self.args)
        self.assertFalse(self.target.exists())
        self.assertEqual(original, bridge.read_json(self.data / "config.json"))
        (self.data / "config.json").write_text(json.dumps(self.original))
        installer.install(self.args)
        (self.target / "state.json").write_text("broken")
        before = snapshot(self.root)
        with self.assertRaises(bridge.BridgeError): installer.install(self.args)
        self.assertEqual(before, snapshot(self.root))

    def test_packaged_cli_installs_and_status_runs_without_source_checkout(self):
        package = Package()
        self.addCleanup(package.cleanup)
        result = subprocess.run([sys.executable, str(package.entry), "--home", str(self.data), "claude-bridge", "install",
                                 "--bridge-home", str(self.target), "--claude-path", sys.executable,
                                 "--workdir", str(self.root), "--apply"], capture_output=True, text=True, timeout=20)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        cfg = bridge.read_json(self.target / "config.json")
        package.cleanup()
        result = subprocess.run([sys.executable, "-B", cfg["entry"], "--config", str(self.target / "config.json"), "status"],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertFalse(json.loads(result.stdout)["changed"])

    def test_invalid_agent_is_refused_and_unicode_equivalent_agent_is_preserved(self):
        self.args.agent = " bad "
        with self.assertRaises(bridge.BridgeError): installer.install(self.args)
        self.assertFalse(self.target.exists())
        original = copy.deepcopy(self.original)
        original["agents"]["e\u0301"] = {"wake_max": "batch"}
        (self.data / "config.json").write_text(json.dumps(original))
        self.args.agent, self.args.apply = "é", True
        installer.install(self.args)
        current = bridge.read_json(self.data / "config.json")
        self.assertEqual(2, len(current["agents"]))
        self.assertEqual("batch", current["agents"]["e\u0301"]["wake_max"])
        self.assertIn("wake_adapter", current["agents"]["e\u0301"])

    def test_upgrade_refuses_while_writer_active(self):
        self.args.apply = True
        installer.install(self.args)
        with bridge.writer_lock(self.target), self.assertRaises(bridge.BridgeError):
            installer.install(self.args)
