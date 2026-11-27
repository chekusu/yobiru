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

FAKE = r'''import json, os, re, sys, time, uuid
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
actual_sid = str(uuid.uuid4()) if mode == "assigned_session" else sid
name = args[args.index("--name") + 1]
listed_name = re.sub(r"[\s\ufeff]+", " ", name).strip()
(root / "agents.json").write_text(json.dumps([{"id": actual_sid[:8], "sessionId": actual_sid,
    "name": listed_name, "cwd": str(root), "status": "idle"}]))
if mode == "timeout_seen": time.sleep(2)
if mode == "nonzero_seen": sys.exit(9)
print("Starting background service…")
print("backgrounded · \x1b[36m" + actual_sid[:8] + "\x1b[39m · " + name)
print("claude attach " + actual_sid[:8])
print("SYNTHETIC PRIVATE OUTPUT MUST NOT ENTER STATE")
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
        (self.data / "config.json").write_text(json.dumps({"allow": ["sender.example.ts.net"]}))
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
               "received": {"at": "2026-01-01T00:00:00Z", "agent": "claude", "wake": {"effective": "now"},
                            "peer": {"node": "sender.example.ts.net", "ip": "100.64.0.9"}}}
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
        self.assertNotIn("--model", self.launches()[0])
        self.assertNotIn("UNTRUSTED BODY", " ".join(self.launches()[0]))

    def test_configured_model_is_forwarded_as_one_exact_option(self):
        models = ("sonnet", "claude-sonnet-4-6", "claude-opus-4-6[1m]",
                  "anthropic/claude-sonnet-4.6",
                  "arn:aws:bedrock:us-east-1:123456789012:inference-profile/anthropic.claude-sonnet-4-6")
        (self.data / "inbox" / (self.mid + ".json")).unlink()
        for index, model in enumerate(models):
            with self.subTest(model=model):
                self.record("%016x" % (index + 1))
                result = bridge.Bridge(dict(self.cfg, model=model)).step()
                self.assertEqual("dispatched", result["state"])
                args = self.launches()[-1]
                self.assertEqual(1, args.count("--model"))
                option = args.index("--model")
                self.assertEqual(model, args[option + 1])
                self.assertLess(option + 1, args.index("--"))
        self.assertEqual(len(models), len(self.launches()))
        self.assertFalse(any("--model" in args for args in self.calls() if "--bg" not in args))

    def test_invalid_configured_model_fails_before_calls_or_writes(self):
        invalid = (None, "", True, 42, [], {}, " sonnet", "sonnet ", "son net",
                   "--help", "-sonnet", "/sonnet", ".sonnet", "[1m]",
                   "a\tb", "a\nb", "a\rb", "a\0b", "sonnet\u00a0", "sönnet", "a" * 257)
        before = snapshot(self.root)
        for model in invalid:
            with self.subTest(model=model):
                with self.assertRaisesRegex(bridge.BridgeError, "model"):
                    bridge.Bridge(dict(self.cfg, model=model))
                self.assertEqual(before, snapshot(self.root))
                self.assertEqual([], self.calls())
        self.assertEqual("a", bridge.validate_config(dict(self.cfg, model="a"))["model"])
        self.assertEqual("a" * 256, bridge.validate_config(dict(self.cfg, model="a" * 256))["model"])
        self.assertFalse(any(c and c[0] in ("stop", "kill", "rm") for c in self.calls()))
        self.assertNotIn("acked_at", json.loads((self.data / "inbox" / (self.mid + ".json")).read_text()))

    def test_disallowed_tools_are_forwarded_before_the_prompt(self):
        tools = ["mcp__iphone_use", "Bash(git push:*)", "mcp__server__*", "WebFetch(domain:example.com)"]
        result = bridge.Bridge(dict(self.cfg, disallowed_tools=tools)).step()
        self.assertEqual("dispatched", result["state"])
        args = self.launches()[-1]
        option = args.index("--disallowedTools")
        self.assertEqual(1, args.count("--disallowedTools"))
        self.assertEqual(tools, args[option + 1:args.index("--")])
        for value in (None, []):
            with self.subTest(value=value):
                (self.data / "inbox" / (self.mid + ".json")).unlink()
                self.mid = "%016x" % (len(self.launches()) + 1)
                self.record(self.mid)
                cfg = dict(self.cfg) if value is None else dict(self.cfg, disallowed_tools=value)
                self.assertEqual("dispatched", bridge.Bridge(cfg).step()["state"])
                self.assertNotIn("--disallowedTools", self.launches()[-1])

    def test_invalid_disallowed_tools_fail_before_calls_or_writes(self):
        invalid = ("mcp__iphone_use", None, {}, [""], ["-x"], ["--model"], ["a b"], ["Bash("], ["Bash(x)(y)"],
                   ["Bash(\n)"], [1], ["Edit", "Edit"],
                   ["t%d" % i for i in range(65)], ["Edit,Write"], ["Bash(a\u0007)"])
        before = snapshot(self.root)
        for tools in invalid:
            with self.subTest(tools=tools), self.assertRaisesRegex(bridge.BridgeError, "disallowed_tools"):
                bridge.Bridge(dict(self.cfg, disallowed_tools=tools))
        self.assertEqual(before, snapshot(self.root))
        self.assertEqual([], self.calls())
        ok = ["t%d" % i for i in range(64)]
        self.assertEqual(ok, bridge.validate_config(dict(self.cfg, disallowed_tools=ok))["disallowed_tools"])

    def test_hourly_launch_cap_holds_messages_then_sends_them_in_one_batch(self):
        cfg = dict(self.cfg, max_launches_per_hour=2)
        self.assertEqual("dispatched", bridge.Bridge(cfg).step()["state"])
        self.record("1111111111111111")
        self.assertEqual("dispatched", bridge.Bridge(cfg).step()["state"])
        held = ("2222222222222222", "3333333333333333")
        for mid in held:
            self.record(mid)
        before = self.calls()
        result = bridge.Bridge(cfg).step()
        self.assertEqual("launch_limit", result["state"])
        self.assertFalse(result["launched"])
        self.assertEqual(2, result["pending"])
        self.assertEqual(2, result["max_launches_per_hour"])
        oldest = min(run["created_at"] for run in self.b.load_state()["runs"].values())
        self.assertEqual(dt.datetime.fromtimestamp(oldest + 3600, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                         result["next_launch_after"])
        # Held back before any call: no auth check, no launch, no intent, messages untouched.
        self.assertEqual(before, self.calls())
        self.assertEqual(2, len(self.b.load_state()["runs"]))
        for mid in held:
            self.assertNotIn("acked_at", json.loads((self.data / "inbox" / (mid + ".json")).read_text()))
        status = bridge.Bridge(cfg).status()
        self.assertEqual((2, 2), (status["launches_last_hour"], status["max_launches_per_hour"]))
        self.assertEqual(sorted(held), sorted(status["pending"]))
        # An hour after the oldest launch one place frees up, and every held message goes out together.
        with patch.object(bridge.time, "time", return_value=oldest + 3600.5):
            result = bridge.Bridge(cfg).step()
        self.assertEqual("dispatched", result["state"])
        self.assertEqual(sorted(held), sorted(result["ids"]))
        self.assertEqual(3, len(self.launches()))

    def test_hourly_launch_cap_counts_uncertain_launches_and_validates(self):
        cfg = dict(self.cfg, max_launches_per_hour=1)
        self.mode("nonzero")
        self.assertEqual("uncertain", bridge.Bridge(cfg).step()["state"])
        state = self.b.load_state()
        run = next(iter(state["runs"].values()))
        run.update(phase="dispatched")
        self.b.save(state)
        self.mode("success")
        self.record("1111111111111111")
        self.assertEqual("launch_limit", bridge.Bridge(cfg).step()["state"])
        self.assertEqual(1, len(self.launches()))
        self.assertEqual(20, bridge.validate_config(dict(self.cfg))["max_launches_per_hour"])
        self.assertEqual(600, bridge.validate_config(dict(self.cfg, max_launches_per_hour=600))["max_launches_per_hour"])
        for value in (0, 601, -1, "20", None, True):
            with self.subTest(value=value), self.assertRaisesRegex(bridge.BridgeError, "max_launches_per_hour"):
                bridge.validate_config(dict(self.cfg, max_launches_per_hour=value))

    def test_background_assigned_session_is_observed_and_next_wake_dispatches(self):
        self.mode("assigned_session")
        real = self.b.agents
        def observe():
            # Receipt and launch bindings must survive a crash before the lookup.
            run = next(iter(self.b.load_state()["runs"].values()))
            agent = json.loads((self.root / "agents.json").read_text())[0]
            self.assertEqual(agent["id"], run["launch_background_id"])
            self.assertEqual(agent["name"], run["name"])
            self.assertEqual(agent["cwd"], run["workdir"])
            return real()
        with patch.object(self.b, "agents", side_effect=observe):
            result = self.b.step()
        self.assertEqual("dispatched", result["state"])
        agent = json.loads((self.root / "agents.json").read_text())[0]
        self.assertNotEqual(result["session_id"], agent["sessionId"])
        run = self.b.load_state()["runs"][result["session_id"]]
        self.assertEqual(agent["id"], run["background_id"])
        self.assertEqual(agent["sessionId"], run["observed_session_id"])
        self.assertNotIn("backgrounded", json.dumps(run))
        self.assertNotIn("SYNTHETIC PRIVATE OUTPUT", json.dumps(run))
        self.assertEqual("idle", bridge.Bridge(self.cfg).step()["state"])
        self.record("fedcba9876543210")
        self.assertEqual("dispatched", bridge.Bridge(self.cfg).step()["state"])
        self.assertEqual(2, len(self.launches()))

    def test_background_receipt_survives_interrupted_reconciliation(self):
        self.mode("assigned_session")
        with patch.object(self.b, "agents", side_effect=SystemExit("simulated crash before lookup")):
            with self.assertRaises(SystemExit):
                self.b.step()
        run = next(iter(self.b.load_state()["runs"].values()))
        self.assertEqual("uncertain", run["phase"])
        self.assertTrue(run["launch_background_id"])
        self.assertEqual("idle", bridge.Bridge(self.cfg).step()["state"])
        self.assertEqual(1, len(self.launches()))

    def test_custom_agent_whitespace_keeps_routing_identity_and_receipt_matching(self):
        self.mode("assigned_session")
        for custom_agent in ("team  assistant", "team\u00a0assistant", "team\ufeffassistant", "team\u3000assistant"):
            with self.subTest(agent=custom_agent):
                cfg = dict(self.cfg, agent=custom_agent)
                b = bridge.Bridge(cfg)
                bridge.atomic_json(self.home / "state.json", bridge.empty_state(cfg["installation_id"]))
                (self.root / "calls.jsonl").unlink(missing_ok=True)
                for mid in (self.mid, "fedcba9876543210"):
                    # Keep the original agent identity in the inbox; only the CLI
                    # display label may use collapsed whitespace.
                    rec = self.record(mid)
                    rec["received"]["agent"] = custom_agent
                    (self.data / "inbox" / (mid + ".json")).write_text(json.dumps(rec))
                    result = b.step()
                    self.assertEqual("dispatched", result["state"])
                    self.assertEqual([mid], result["ids"])
                    run = b.load_state()["runs"][result["session_id"]]
                    self.assertEqual("Yobiru team assistant " + mid[:8], run["name"])
                    self.assertEqual(custom_agent, b.cfg["agent"])
                    self.assertEqual(custom_agent, json.loads((self.data / "inbox" / (mid + ".json")).read_text())["received"]["agent"])
                    self.assertEqual("idle", bridge.Bridge(cfg).step()["state"])
                self.assertEqual(2, len(self.launches()))

    def test_background_receipt_requires_unique_id_and_saved_launch_bindings(self):
        sid = str(uuid.uuid4())
        actual_sid = str(uuid.uuid4())
        short = actual_sid[:8]
        run = {"ids": [self.mid], "phase": "uncertain", "name": "Yobiru claude " + self.mid[:8],
               "workdir": str(self.root), "launch_background_id": short}
        agent = {"id": short, "sessionId": actual_sid, "name": run["name"], "cwd": run["workdir"]}
        cases = [(None, {}), ([], {}), ([dict(agent, id="deadbeef")], {}), ([dict(agent, cwd=str(self.data))], {}),
                 ([dict(agent, name="another session")], {}), ([agent, dict(agent)], {}),
                 ([agent], {"launch_background_id": None}), ([agent], {"name": None}),
                 ([agent], {"workdir": None}),
                 ([dict(agent, sessionId=sid), dict(agent, sessionId=sid)], {})]
        for agents, changes in cases:
            with self.subTest(agents=agents, changes=changes):
                state = bridge.empty_state(self.cfg["installation_id"])
                state["runs"][sid] = dict(run, **changes)
                self.b.save(state)
                with patch.object(self.b, "agents", return_value=agents):
                    self.assertEqual("uncertain", self.b.step()["state"])
                self.assertEqual([], self.launches())
        # A config change cannot substitute for the workdir/name saved at launch.
        state["runs"][sid] = dict(run)
        self.b.save(state)
        changed = bridge.Bridge(dict(self.cfg, workdir=str(self.data), agent="other"))
        with patch.object(changed, "agents", return_value=[agent]):
            changed.reconcile(state)
        self.assertEqual("dispatched", state["runs"][sid]["phase"])
        self.assertNotIn("reason", state["runs"][sid])

    def test_background_receipt_reconciles_after_delayed_agent_visibility(self):
        self.mode("assigned_session")
        with patch.object(self.b, "agents", return_value=[]):
            self.assertEqual("uncertain", self.b.step()["state"])
        self.assertEqual("idle", bridge.Bridge(self.cfg).step()["state"])
        self.assertNotIn("reason", next(iter(self.b.load_state()["runs"].values())))
        self.assertEqual(1, len(self.launches()))

    def test_background_receipt_can_reconcile_without_listed_session_uuid(self):
        self.mode("assigned_session")
        real = self.b.agents
        def without_session_id():
            return [{k: v for k, v in agent.items() if k != "sessionId"} for agent in real()]
        with patch.object(self.b, "agents", side_effect=without_session_id):
            self.assertEqual("dispatched", self.b.step()["state"])
        self.assertEqual(1, len(self.launches()))

    def test_malformed_or_ambiguous_launch_output_cannot_bind_assigned_session(self):
        self.mode("assigned_session")
        real = self.b.call
        for output in ("", "not a launch receipt deadbeef", "backgrounded · invalid-id",
                       "backgrounded · deadbeef\nbackgrounded · cafebabe"):
            with self.subTest(output=output):
                def call(args, timeout=15):
                    code, out = real(args, timeout)
                    return (code, output) if "--bg" in args else (code, out)
                with patch.object(self.b, "call", side_effect=call):
                    self.assertEqual("uncertain", self.b.step()["state"])
                self.assertEqual("uncertain", bridge.Bridge(self.cfg).step()["state"])
                self.assertEqual(1, len(self.launches()))
                bridge.atomic_json(self.home / "state.json", bridge.empty_state(self.cfg["installation_id"]))
                (self.root / "calls.jsonl").unlink()

    def test_fifo_config_or_state_status_fails_promptly_without_writes(self):
        config = self.home / "config.json"
        config.write_text(json.dumps(self.cfg))
        for path in (config, self.home / "state.json"):
            with self.subTest(path=path.name):
                original = path.read_bytes()
                path.unlink()
                os.mkfifo(path, 0o600)
                def inventory():
                    return sorted((str(p.relative_to(self.root)), p.lstat().st_mode, p.lstat().st_size)
                                  for p in self.root.rglob("*"))
                before = inventory()
                result = subprocess.run([sys.executable, "-B", str(REPO / "claude_bridge.py"),
                                         "--config", str(config), "status"],
                                        capture_output=True, text=True, timeout=3)
                self.assertEqual(2, result.returncode, result.stdout + result.stderr)
                self.assertIn("bounded regular file", result.stderr)
                self.assertEqual(before, inventory())
                self.assertEqual([], self.calls())
                self.assertFalse((self.home / "bridge.lock").exists())
                path.unlink()
                path.write_bytes(original)

    def test_current_access_revocation_blocks_old_queue_then_restores_same_id(self):
        for policy in ({"deny": ["sender.example.ts.net"]}, {"allow": []}, {"allow": "invalid"}):
            with self.subTest(policy=policy):
                (self.data / "config.json").write_text(json.dumps(policy))
                result = self.b.step()
                self.assertEqual("idle", result["state"])
                self.assertTrue(result["refused"][0]["reason"].startswith("access_policy:"))
                self.assertEqual([], self.calls())
                self.assertEqual({}, self.b.load_state()["runs"])
        (self.data / "config.json").write_text(json.dumps({"allow": ["sender.example.ts.net"]}))
        result = self.b.step()
        self.assertEqual("dispatched", result["state"])
        self.assertEqual([self.mid], result["ids"])
        self.assertEqual(1, len(self.launches()))

    def test_missing_or_mismatched_verified_peer_cannot_wake(self):
        for peer in (None, {"node": "different.example.ts.net"}):
            rec = self.record(self.mid)
            rec["received"]["peer"] = peer
            (self.data / "inbox" / (self.mid + ".json")).write_text(json.dumps(rec))
            result = self.b.step()
            self.assertEqual("idle", result["state"])
            self.assertEqual("access_policy:sender_mismatch", result["refused"][0]["reason"])
            self.assertEqual([], self.calls())

    def test_access_revoked_during_auth_check_is_rechecked_before_intent(self):
        real = self.b.call
        def revoke_after_auth(args, timeout=15):
            result = real(args, timeout)
            if args == ["auth", "status"]:
                (self.data / "config.json").write_text(json.dumps({"allow": []}))
            return result
        with patch.object(self.b, "call", side_effect=revoke_after_auth):
            self.assertEqual("idle", self.b.step()["state"])
        self.assertEqual([], self.launches())
        self.assertEqual({}, self.b.load_state()["runs"])

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

    def test_daemon_logs_only_when_the_outcome_changes(self):
        results = iter([{"state": "idle", "refused": []}] * 3 + [{"state": "auth_required", "launched": False}] * 2
                       + [{"state": "idle", "refused": []}])
        class Stop(Exception):
            pass
        def step():
            try:
                return next(results)
            except StopIteration:
                raise Stop()
        printed = []
        with patch.object(self.b, "_step", side_effect=step), patch("time.sleep"), \
                patch("builtins.print", side_effect=lambda line, **_: printed.append(json.loads(line))):
            with self.assertRaises(Stop):
                self.b.daemon()
        self.assertEqual(["idle", "auth_required", "idle"], [line["state"] for line in printed])
        for line in printed:
            dt.datetime.strptime(line["at"], "%Y-%m-%dT%H:%M:%SZ")


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
                                       workdir=str(self.root), permission_mode=None, model=None, apply=False)

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

    def test_disallowed_tools_are_saved_preserved_on_upgrade_and_cleared(self):
        self.args.disallow_tool = ["mcp__iphone_use", "Bash(git push:*)", "mcp__iphone_use"]
        self.assertEqual(["mcp__iphone_use", "Bash(git push:*)"], installer.install(self.args)["disallowed_tools"])
        self.args.apply = True
        installer.install(self.args)
        self.assertEqual(["mcp__iphone_use", "Bash(git push:*)"],
                         bridge.read_json(self.target / "config.json")["disallowed_tools"])
        self.args.disallow_tool = None
        installer.install(self.args)
        self.assertEqual(["mcp__iphone_use", "Bash(git push:*)"],
                         bridge.read_json(self.target / "config.json")["disallowed_tools"])
        self.args.no_disallowed_tools = True
        self.assertEqual([], installer.install(self.args)["disallowed_tools"])
        self.assertNotIn("disallowed_tools", bridge.read_json(self.target / "config.json"))
        self.args.no_disallowed_tools, self.args.disallow_tool = False, ["bad tool"]
        with self.assertRaisesRegex(bridge.BridgeError, "disallowed_tools"):
            installer.install(self.args)
        self.assertNotIn("disallowed_tools", bridge.read_json(self.target / "config.json"))

    def test_stable_links_for_claude_and_python_are_kept_across_updates(self):
        versions = self.root / "versions"
        def release(number):
            path = versions / number / "claude"
            path.parent.mkdir(parents=True)
            path.write_text("#!/bin/sh\nexit 0\n")
            path.chmod(0o700)
            return path
        link = self.root / "bin" / "claude"
        link.parent.mkdir()
        link.symlink_to(release("1.0"))
        python = self.root / "bin" / "python3"
        python.symlink_to(sys.executable)
        self.args.claude_path, self.args.apply = str(link), True
        with patch.object(installer.sys, "executable", str(python)):
            plan = installer.install(self.args)
        self.assertEqual((str(link), str(python)), (plan["claude_path"], plan["python"]))
        self.assertNotIn("previous_claude_path", plan)
        cfg = bridge.read_json(self.target / "config.json")
        self.assertEqual((str(link), str(python)), (cfg["claude_path"], cfg["python"]))
        adapter = bridge.read_json(self.data / "config.json")["agents"]["claude"]["wake_adapter"]
        self.assertEqual(str(python), adapter["argv"][0])
        plist = plistlib.loads(next((self.target / "services").glob("*.plist")).read_bytes())
        self.assertEqual(str(python), plist["ProgramArguments"][0])
        # `claude update`: a new versioned file behind the same link, the old one removed.
        link.unlink()
        link.symlink_to(release("2.0"))
        (versions / "1.0" / "claude").unlink()
        self.args.claude_path = None
        with patch.object(installer.sys, "executable", str(python)):
            installer.install(self.args)
        self.assertEqual(str(link), bridge.read_json(self.target / "config.json")["claude_path"])

    def test_explicit_claude_path_replaces_a_removed_executable_but_workdir_stays_bound(self):
        old = self.root / "versions" / "1.0" / "claude"
        old.parent.mkdir(parents=True)
        old.write_text("#!/bin/sh\nexit 0\n")
        old.chmod(0o700)
        self.args.claude_path, self.args.apply = str(old), True
        installer.install(self.args)
        old.unlink()
        self.args.claude_path = None
        with self.assertRaisesRegex(bridge.BridgeError, "--claude-path"):
            installer.install(self.args)
        self.args.claude_path, self.args.apply = sys.executable, False
        plan = installer.install(self.args)
        self.assertEqual((sys.executable, str(old)), (plan["claude_path"], plan["previous_claude_path"]))
        self.args.apply = True
        installer.install(self.args)
        self.assertEqual(sys.executable, bridge.read_json(self.target / "config.json")["claude_path"])
        other = self.root / "other"
        other.mkdir()
        self.args.workdir = str(other)
        before = snapshot(self.root)
        with self.assertRaisesRegex(bridge.BridgeError, "binding differs"):
            installer.install(self.args)
        self.assertEqual(before, snapshot(self.root))

    def test_cli_disallow_tool_flags_are_exclusive(self):
        with self.assertRaises(SystemExit), patch("sys.stderr"):
            installer.main(["--tailmsg-home", str(self.data), "--disallow-tool", "Edit", "--no-disallowed-tools"])

    def test_service_paths_preserve_literal_environment_and_unit_specifiers(self):
        unusual = self.root / "cash${HOME}%i"
        unusual.mkdir()
        self.args.bridge_home = str(unusual / "bridge")
        self.args.workdir = str(unusual)
        self.args.apply = True
        installer.install(self.args)
        target = unusual / "bridge"
        cfg = bridge.read_json(target / "config.json")
        unit = next((target / "services").glob("*.service")).read_text()
        working = next(line for line in unit.splitlines() if line.startswith("WorkingDirectory="))
        command = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
        self.assertIn('cash${HOME}%%i', working)
        self.assertIn('cash$${HOME}%%i', command)
        plist = plistlib.loads(next((target / "services").glob("*.plist")).read_bytes())
        self.assertIn(cfg["entry"], plist["ProgramArguments"])
        self.assertEqual(str(unusual), plist["WorkingDirectory"])

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

    def test_inherited_global_adapters_are_preserved_in_preview_and_apply(self):
        for inherited in ({"wake_adapter": {"type": "exec", "argv": ["/custom/global/bridge"]}},
                          {"on_message": "/custom/legacy/bridge"}):
            original = dict(self.original, default_agent="claude", **inherited)
            (self.data / "config.json").write_text(json.dumps(original))
            for apply in (False, True):
                with self.subTest(inherited=inherited, apply=apply):
                    self.args.apply = apply
                    before = snapshot(self.root)
                    with self.assertRaises(bridge.BridgeError): installer.install(self.args)
                    self.assertEqual(before, snapshot(self.root))
                    self.assertFalse(self.target.exists())

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

    def test_empty_existing_bridge_config_is_not_treated_as_a_fresh_install(self):
        self.target.mkdir(mode=0o700)
        (self.target / "config.json").write_text("{}")
        before = snapshot(self.root)
        self.args.apply = True
        with self.assertRaises(bridge.BridgeError): installer.install(self.args)
        self.assertEqual(before, snapshot(self.root))

    def test_upgrade_refuses_while_writer_active(self):
        self.args.apply = True
        installer.install(self.args)
        with bridge.writer_lock(self.target), self.assertRaises(bridge.BridgeError):
            installer.install(self.args)
