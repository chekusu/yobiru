"""Onboarding and doctor guidance per platform, sessions binding, and the `agents`/`peers` view.

Platform facts are injected in-process; the unpacked plugin runs with an isolated HOME and a fake tailscale.
Nothing installs Tailscale, changes its settings, or contacts another machine.
"""
import importlib.util
import json
import os
import stat
import sys
import unittest
from pathlib import Path

from support.pkg import REPO, Package, snapshot

spec = importlib.util.spec_from_file_location("yobiru_onboarding_under_test",
                                              str(REPO / "plugins" / "yobiru" / "scripts" / "yobiru.py"))
Y = importlib.util.module_from_spec(spec)
spec.loader.exec_module(Y)
UUID = "0192f0c4-1d2e-7a3b-8c4d-5e6f7a8b9c0d"
PKG = None


def setUpModule():
    global PKG
    PKG = Package()


def tearDownModule():
    PKG.cleanup()


def facts(platform="linux", **kw):
    base = {"platform": platform, "system": platform, "root": False, "sudo": True, "curl": True, "tun": True,
            "systemd": True, "systemd_user": True, "workspace": False, "container": False, "wsl": False}
    base.update(kw)
    return base


def gathered(ts, config=False):
    return {"python": {"version": "3.12.0"}, "tailscale": ts, "config_error": None, "config_exists": config,
            "cfg": None, "allow": [], "runtime": {"path": "/x/tailmsg.py", "present": False}, "receiver": {}}


MISSING = {"cmd": ["tailscale"], "found": None, "error": "tailscale not found", "probed": False}


def ts_state(backend=None, error=None, cmd=("tailscale",)):
    return {"cmd": list(cmd), "found": "/usr/bin/tailscale", "error": error, "probed": True, "backend": backend,
            "node": "alpha.example.ts.net"}


def step(steps, sid):
    return [s for s in steps if s["id"] == sid][0]


class PlatformGuidanceTests(unittest.TestCase):
    def install(self, **kw):
        return Y.tailscale_install_steps(facts(**kw))

    def test_linux_official_script_with_sudo_or_root(self):
        status, detail, cmds, docs, who = self.install()
        self.assertEqual("todo", status)
        self.assertEqual(["curl -fsSL https://tailscale.com/install.sh | sh"], cmds)
        self.assertIn("https://tailscale.com/docs/install/linux", docs)
        self.assertIn("sudo", detail)
        self.assertNotIn("through sudo", self.install(root=True, sudo=False)[1])

    def test_linux_without_privileges_or_tun_is_blocked_honestly(self):
        status, detail, _, _, who = self.install(sudo=False)
        self.assertEqual(("blocked", "administrator"), (status, who))
        status, detail, cmds, _, _ = self.install(tun=False, container=True)
        self.assertEqual("blocked", status)
        self.assertIn("no Tailscale IP", detail)
        self.assertNotIn("--tun=userspace-networking", " ".join(cmds))
        self.assertIn("supervisor", self.install(systemd=False)[1])

    def test_macos_and_windows_use_the_native_app(self):
        status, detail, cmds, docs, who = self.install(platform="darwin")
        self.assertIn("System Settings", detail)
        self.assertIn("https://tailscale.com/docs/install/mac", docs)
        self.assertEqual([], cmds)
        status, detail, cmds, _, _ = self.install(platform="darwin", mac_app_cli=True)
        self.assertIn(Y.MAC_TS_CLI, cmds[0])
        status, detail, cmds, docs, who = self.install(platform="win32")
        self.assertIn("administrator", detail)
        self.assertIn("https://tailscale.com/docs/install/windows", docs)
        status, detail, cmds, _, _ = self.install(platform="win32", win_cli=True)
        self.assertEqual(['yobiru setup --tailscale-cmd "C:\\Program Files\\Tailscale\\tailscale.exe" '
                          '--replace tailscale_cmd'], cmds)                  # valid in PowerShell and cmd

    def login(self, platform, backend="NeedsLogin", **kw):
        return step(Y.onboarding_steps(Path("/nonexistent"), gathered(ts_state(backend)), facts(platform, **kw)),
                    "tailscale-login")

    def test_sign_in_is_left_to_the_user_and_platform_aware(self):
        s = self.login("linux")
        self.assertEqual(["sudo tailscale up"], s["commands"])
        self.assertIn("user", s["who"])
        self.assertIn("no auth keys", s["detail"])
        self.assertEqual(["tailscale up"], self.login("linux", root=True)["commands"])
        self.assertIn("administrator", self.login("linux", sudo=False)["detail"])
        for plat in ("darwin", "win32"):
            s = self.login(plat)
            self.assertNotIn("sudo", " ".join(s["commands"]) + s["detail"])
            self.assertIn("Tailscale app", s["detail"])
        win = step(Y.onboarding_steps(Path("/nonexistent"),
                                      gathered(ts_state("NeedsLogin", cmd=(Y.WIN_TS_CLI,))), facts("win32")),
                   "tailscale-login")
        self.assertEqual(['& "C:\\Program Files\\Tailscale\\tailscale.exe" up'], win["commands"])
        self.assertIn("PowerShell", win["detail"])
        s = self.login("darwin", backend="NeedsMachineAuth")
        self.assertIn("administrator", s["who"])

    def test_permission_and_daemon_guidance(self):
        detail, cmds, who = Y.permission_guidance(facts("win32"), ["tailscale"])
        self.assertNotIn("sudo", detail + " ".join(cmds))
        detail, cmds, who = Y.permission_guidance(facts("linux"), ["tailscale"])
        self.assertIn('["sudo","-n","tailscale"]', cmds[0])
        detail, cmds, who = Y.permission_guidance(facts("linux", sudo=False), ["tailscale"])
        self.assertEqual(([], "administrator"), (cmds, who))
        self.assertEqual([], Y.permission_guidance(facts("linux", root=True), ["tailscale"])[1])
        steps = Y.onboarding_steps(Path("/nonexistent"),
                                   gathered(ts_state(error="failed to connect to local tailscaled")),
                                   facts("linux", root=True, sudo=False))
        self.assertEqual(["systemctl enable --now tailscaled"], step(steps, "tailscale-login")["commands"])
        steps = Y.onboarding_steps(Path("/nonexistent"),
                                   gathered(ts_state(error="failed to connect to local tailscaled")),
                                   facts("linux", sudo=False))
        self.assertEqual([], step(steps, "tailscale-login")["commands"])

    def test_states_are_classified(self):
        self.assertEqual("missing", Y.classify_tailscale(MISSING))
        for err, want in (("permission denied", "permission"), ("failed to connect to local tailscaled", "daemon_unavailable"),
                          ("weird", "error")):
            self.assertEqual(want, Y.classify_tailscale(ts_state(error=err)))
        for backend, want in (("Running", "running"), ("NeedsLogin", "logged_out"), ("Stopped", "stopped"),
                              ("NeedsMachineAuth", "needs_approval")):
            self.assertEqual(want, Y.classify_tailscale(ts_state(backend)))


class Base(unittest.TestCase):
    def setUp(self):
        self.user = PKG.new_home("home %s" % self.id().rsplit(".", 1)[-1])
        self.home = self.user / ".tailmsg"

    def run_json(self, *args, **kw):
        return PKG.run_json(list(args), self.user, **kw)

    def write_config(self, cfg):
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "config.json").write_text(json.dumps(cfg))


class FirstRunTests(Base):
    def test_onboard_works_without_config_or_tailscale_and_changes_nothing(self):
        before = snapshot(self.user, ignore_macos_caches=True)
        r, d = self.run_json("onboard", "--json")
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertFalse(d["changed"])
        ids = [s["id"] for s in d["steps"]]
        self.assertEqual(["python", "tailscale-install", "tailscale-login", "config", "runtime", "receiver", "verify"],
                         ids)
        self.assertEqual("tailscale-install", d["next"])
        self.assertEqual(before, snapshot(self.user, ignore_macos_caches=True))
        r = PKG.run(["onboard"], self.user)
        self.assertIn("no install hook", r.stdout)

    def test_doctor_explains_daemon_and_login_states(self):
        self.write_config({"allow": ["bravo"]})
        state = PKG.fake_state("daemon", status_error="failed to connect to local tailscaled; it doesn't appear to "
                                                      "be running", calls=str(PKG.root / "daemon-calls.log"))
        r, d = self.run_json("doctor", "--json", tailscale=True, extra={"FAKE_TS_STATE": str(state)})
        ts = [c for c in d["checks"] if c["check"] == "tailscale"][0]
        self.assertIn("daemon is not available", ts["detail"])
        state = PKG.fake_state("login", status={"BackendState": "NeedsLogin", "Self": {}, "Peer": {}})
        r, d = self.run_json("doctor", "--json", tailscale=True, extra={"FAKE_TS_STATE": str(state)})
        ts = [c for c in d["checks"] if c["check"] == "tailscale"][0]
        self.assertIn("logged out", ts["fix"])
        self.assertTrue(any(c["check"] == "next" for c in d["checks"]))
        calls = [json.loads(x) for x in (PKG.root / "daemon-calls.log").read_text().splitlines()]
        self.assertTrue(all(c == ["status", "--json"] for c in calls), calls)


class SessionBindingTests(Base):
    def setUp(self):
        super().setUp()
        self.write_config({"allow": ["bravo"], "default_agent": "codex", "agents": {"codex": {}, "grok": {}}})

    def test_bind_needs_an_exact_id_and_is_a_dry_run_first(self):
        r = PKG.run(["session", "bind", "--agent", "grok", "--display-name", "Desk"], self.user)
        self.assertEqual(2, r.returncode)
        self.assertIn("a name alone never selects one", r.stderr)
        r = PKG.run(["session", "bind", "--agent", "codex", "--session-id", "not-a-uuid"], self.user)
        self.assertEqual(2, r.returncode)
        r, d = self.run_json("session", "bind", "--agent", "grok", "--session-id", "chat-1", "--runtime", "grok",
                             "--json")
        self.assertFalse(d["written"])
        self.assertFalse((self.home / "sessions.json").exists())
        r = PKG.run(["session", "bind", "--agent", "nobody", "--session-id", "x"], self.user)
        self.assertEqual(2, r.returncode)

    def test_bind_preserves_unknown_keys_backs_up_and_guards_replacement(self):
        (self.home / "sessions.json").write_text(json.dumps({"v": 1, "future": [1], "agents": {
            "grok": {"runtime": "grok", "session_id": "chat-1", "custom": "kept"}}}))
        before = (self.home / "sessions.json").read_bytes()
        r, d = self.run_json("session", "bind", "--agent", "grok", "--session-id", "chat-2", "--apply", "--json")
        self.assertEqual(1, r.returncode)
        self.assertIn("--replace", d["errors"][0])
        self.assertEqual(before, (self.home / "sessions.json").read_bytes())
        r, d = self.run_json("session", "bind", "--agent", "grok", "--display-name", "Research desk", "--apply", "--json")
        self.assertTrue(d["written"], d)
        data = json.loads((self.home / "sessions.json").read_text())
        self.assertEqual(([1], "kept", "chat-1", "Research desk"),
                         (data["future"], data["agents"]["grok"]["custom"], data["agents"]["grok"]["session_id"],
                          data["agents"]["grok"]["display_name"]))
        self.assertEqual(before, Path(d["backup"]).read_bytes())
        self.assertEqual(0o600, stat.S_IMODE((self.home / "sessions.json").stat().st_mode))

    def test_codex_binding_cannot_conflict_with_the_bridge(self):
        (self.home / "codex-bridge.json").write_text(json.dumps({"dispatcher_thread_id": UUID}))
        other = "0192f0c4-1d2e-7a3b-8c4d-000000000000"
        r, d = self.run_json("session", "bind", "--agent", "codex", "--session-id", other, "--apply", "--json")
        self.assertEqual(1, r.returncode)
        self.assertIn("conflicts with the installed Codex bridge", d["errors"][0])
        r, d = self.run_json("session", "bind", "--agent", "codex", "--display-name", "Dispatcher", "--apply", "--json")
        self.assertTrue(d["written"], d)
        self.assertEqual(UUID, json.loads((self.home / "sessions.json").read_text())["agents"]["codex"]["session_id"])
        r, d = self.run_json("session", "list", "--json")
        codex = [a for a in d["agents"] if a["agent"] == "codex"][0]["session"]
        self.assertEqual((UUID, "codex-bridge", "Dispatcher", "not_supported"),
                         (codex["id"], codex["id_source"], codex["display_name"], codex["native_name_status"]))

    def test_onboarding_separates_binding_from_delivery(self):
        PKG.run(["session", "bind", "--agent", "grok", "--session-id", "chat-1", "--apply"], self.user)
        r, d = self.run_json("onboard", "--json", "--no-probe")
        delivery = step(d["steps"], "delivery")
        self.assertEqual("todo", delivery["status"])
        self.assertIn("delivery is configured separately", delivery["detail"])
        self.assertIn("grok", delivery["detail"])

    def test_peers_is_the_richer_agents_view(self):
        state = PKG.fake_state("peers-view")
        r1, d1 = self.run_json("agents", "--json", tailscale=True, extra={"FAKE_TS_STATE": str(state)})
        r2, d2 = self.run_json("peers", "--json", tailscale=True, extra={"FAKE_TS_STATE": str(state)})
        self.assertEqual(0, r1.returncode, r1.stderr)
        self.assertEqual([m["node"] for m in d1["machines"]], [m["node"] for m in d2["machines"]])
        me = d1["machines"][0]
        self.assertTrue(me["self"])
        self.assertIn(me["receiver"], ("unreachable",))
        self.assertIsNone(me["version"])                               # no running receiver: no version claimed
        self.assertEqual(Y.plugin_version(), me["local_plugin_version"])
        self.assertIn("local files", me["source"])
        self.assertFalse((self.home / "upgrade-notices.json").exists())


if __name__ == "__main__":
    unittest.main()
