"""Policy setup/migration through the real packaged CLI, with no network or user state access."""
import json
import stat
import unittest
from pathlib import Path

from support.pkg import Package, snapshot

PKG = None


def setUpModule():
    global PKG
    PKG = Package()


def tearDownModule():
    PKG.cleanup()


class PolicyCliTests(unittest.TestCase):
    def setUp(self):
        self.user = PKG.new_home("policy " + self.id().rsplit(".", 1)[-1])
        self.home = self.user / ".tailmsg"

    def write_config(self, raw):
        self.home.mkdir(parents=True, exist_ok=True)
        p = self.home / "config.json"
        p.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n")
        return p

    def run_json(self, *args):
        return PKG.run_json(list(args) + ["--json"], self.user)

    def config(self):
        return json.loads((self.home / "config.json").read_text())

    def test_policy_show_before_setup_is_read_only_and_has_no_network_dependency(self):
        before = snapshot(self.user, ignore_macos_caches=True)
        r, out = self.run_json("policy", "show")
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertEqual({"access_policy": "tailnet", "allow": [], "deny": []}, out["policy"])
        self.assertFalse(out["config_exists"])
        self.assertFalse(out["changed"])
        self.assertEqual(before, snapshot(self.user, ignore_macos_caches=True))

    def test_legacy_policy_show_and_diagnostics_preserve_restrictions(self):
        p = self.write_config({"allow": ["laptop"]})
        before = snapshot(self.user, ignore_macos_caches=True)
        r, out = self.run_json("policy", "show")
        self.assertEqual("allowlist", out["policy"]["access_policy"])
        self.assertTrue(out["legacy_allowlist"])
        r, out = self.run_json("doctor", "--no-probe")
        checks = {c["check"]: c for c in out["checks"]}
        self.assertIn("allowlist", checks["access-policy"]["detail"])
        self.assertIn("--mode tailnet", checks["policy-migration"]["fix"])
        r, out = self.run_json("onboard", "--no-probe")
        migrate = next(s for s in out["steps"] if s["id"] == "policy-migration")
        self.assertEqual("optional", migrate["status"])
        self.assertNotEqual("policy-migration", out["next"])
        self.assertEqual(before, snapshot(self.user, ignore_macos_caches=True))
        self.assertEqual({"allow": ["laptop"]}, json.loads(p.read_text()))

    def test_migration_dry_run_then_backup_preserves_wake_sessions_and_other_data(self):
        original = {"allow": ["laptop"], "deny": ["retired"], "self_agent": "codex", "default_agent": "codex",
                    "agents": {"codex": {"wake_adapter": {"type": "exec", "argv": ["/bin/true"]},
                                          "wake_max": "batch"}},
                    "wake_policy": {"default_max": "batch", "peers": {"laptop": "now"}},
                    "quiet_hours": {"start": "22:00", "end": "08:00"}, "future_key": [1, "kept"]}
        config = self.write_config(original)
        (self.home / "sessions.json").write_text('{"v":1,"agents":{"codex":{"session_id":"fixed-session"}}}')
        (self.home / "codex-bridge.json").write_text('{"allow_replies":false,"dispatcher_thread_id":"fixed-session"}')
        before = snapshot(self.user, ignore_macos_caches=True)
        old_bytes = config.read_bytes()
        r, out = self.run_json("policy", "set", "--mode", "tailnet")
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertTrue(out["dry_run"])
        self.assertFalse(out["written"])
        self.assertEqual(before, snapshot(self.user, ignore_macos_caches=True))
        r, out = self.run_json("policy", "set", "--mode", "tailnet", "--apply")
        self.assertTrue(out["written"], out)
        expected = dict(original, access_policy="tailnet")
        self.assertEqual(expected, self.config())
        self.assertEqual(old_bytes, Path(out["backup"]).read_bytes())
        self.assertEqual(0o600, stat.S_IMODE(Path(out["backup"]).stat().st_mode))
        after = snapshot(self.user, ignore_macos_caches=True)
        for name in (".tailmsg/sessions.json", ".tailmsg/codex-bridge.json"):
            self.assertEqual(before[name], after[name])
        r, out = self.run_json("policy", "set", "--mode", "tailnet", "--apply")
        self.assertEqual("none", out["action"])
        self.assertFalse(out["written"])
        self.assertIsNone(out["backup"])

    def test_set_lists_replace_explicitly_deduplicate_and_clear(self):
        self.write_config({"access_policy": "tailnet", "allow": ["old"], "deny": ["old-denied"]})
        r, out = self.run_json("policy", "set", "--mode", "allowlist", "--allow", "laptop",
                               "--allow", "LAPTOP", "--deny", "tag:untrusted", "--deny", "user:guest@example.test",
                               "--apply")
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertEqual({"access_policy": "allowlist", "allow": ["laptop"],
                          "deny": ["tag:untrusted", "user:guest@example.test"]}, self.config())
        r, out = self.run_json("policy", "set", "--mode", "tailnet", "--clear-deny", "--apply")
        self.assertEqual([], self.config()["deny"])
        self.assertEqual(["laptop"], self.config()["allow"])
        r, out = self.run_json("policy", "set", "--mode", "allowlist", "--clear-allow", "--apply")
        self.assertEqual([], self.config()["allow"])
        self.assertIn("rejects incoming", " ".join(out["notes"]))

    def test_invalid_rules_modes_and_missing_config_do_not_write(self):
        r = PKG.run(["policy", "set", "--mode", "tailnet", "--apply"], self.user)
        self.assertEqual(2, r.returncode)
        self.assertFalse(self.home.exists())
        self.write_config({"allow": ["laptop"]})
        before = snapshot(self.user, ignore_macos_caches=True)
        for args in (["--mode", "open"], ["--mode", "tailnet", "--deny", "*"],
                     ["--mode", "tailnet", "--deny", "bad peer"],
                     ["--mode", "allowlist", "--allow", "../peer"],
                     ["--mode", "tailnet", "--deny", "peer", "--clear-deny"]):
            r = PKG.run(["policy", "set"] + args + ["--apply"], self.user)
            self.assertEqual(2, r.returncode, (args, r.stdout, r.stderr))
        self.assertEqual(before, snapshot(self.user, ignore_macos_caches=True))

    def test_malformed_existing_policy_is_not_silently_repaired(self):
        for raw in ({"allow": "laptop"}, {"access_policy": "typo"}, {"deny": "retired"}):
            p = self.write_config(raw)
            before = snapshot(self.user, ignore_macos_caches=True)
            r = PKG.run(["policy", "set", "--mode", "tailnet", "--apply"], self.user)
            self.assertEqual(2, r.returncode, (raw, r.stderr))
            self.assertEqual(before, snapshot(self.user, ignore_macos_caches=True))

    def test_setup_explicit_send_only_and_existing_modes_remain_restricted(self):
        r, out = self.run_json("setup", "--send-only", "--apply")
        self.assertTrue(out["written"])
        self.assertEqual("allowlist", self.config()["access_policy"])
        self.assertEqual([], self.config()["allow"])
        for raw in ({"allow": ["laptop"]}, {"access_policy": "allowlist", "allow": ["laptop"]},
                    {"access_policy": "tailnet", "allow": ["laptop"]}):
            self.write_config(raw)
            r, out = self.run_json("setup", "--allow", "new-peer", "--agent", "helper", "--apply")
            self.assertEqual(0, r.returncode, r.stderr)
            self.assertEqual(raw.get("access_policy"), self.config().get("access_policy"))
            self.assertEqual(["laptop", "new-peer"], self.config()["allow"])

    def test_setup_adding_allow_preserves_implicit_tailnet_mode(self):
        self.write_config({"port": 7766})
        r, out = self.run_json("setup", "--allow", "laptop", "--apply")
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertEqual("tailnet", self.config()["access_policy"])
        self.assertEqual(["laptop"], self.config()["allow"])

    def test_tailnet_diagnostics_do_not_require_allow_rules(self):
        self.write_config({"access_policy": "tailnet", "allow": [], "deny": ["retired"]})
        r, out = self.run_json("doctor", "--no-probe")
        policy = next(c for c in out["checks"] if c["check"] == "access-policy")
        self.assertEqual("ok", policy["status"])
        self.assertIn("retired", policy["detail"])
        r, out = self.run_json("onboard", "--no-probe")
        config = next(s for s in out["steps"] if s["id"] == "config")
        self.assertEqual("done", config["status"])
        self.assertFalse(any(s["id"] == "policy-migration" for s in out["steps"]))
        r, out = self.run_json("status", "--no-probe")
        self.assertEqual("tailnet", out["access_policy"])
        self.assertEqual(["retired"], out["deny"])
        self.assertFalse(out["legacy_allowlist"])


if __name__ == "__main__":
    unittest.main()
