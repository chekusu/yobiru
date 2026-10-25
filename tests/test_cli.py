"""The unpacked plugin archive, run from outside the checkout with an isolated HOME and PATH."""
import json
import os
import plistlib
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

from support.pkg import REPO, Package, free_port, snapshot

PKG = None
FALSE = shutil.which("false") or "/bin/false"  # wake-config checks the path; macOS only has /usr/bin/false


def setUpModule():
    global PKG
    PKG = Package()


def tearDownModule():
    PKG.cleanup()


class Base(unittest.TestCase):
    def setUp(self):
        self.user = PKG.new_home("home %s" % self.id().rsplit(".", 1)[-1])
        self.home = self.user / ".tailmsg"

    def write_config(self, cfg, home=None):
        home = home or self.home
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
        return home / "config.json"

    def config(self, home=None):
        return json.loads(((home or self.home) / "config.json").read_text())

    def yobiru(self, *args, **kw):
        return PKG.run(list(args), self.user, **kw)


class LocateTests(Base):
    def test_paths_resolve_inside_the_unpacked_archive(self):
        r, d = PKG.run_json(["paths"], self.user)
        self.assertEqual(0, r.returncode)
        # The launcher reports its own resolved location: on macOS the temporary /var/... is /private/var/...
        self.assertEqual(os.path.realpath(str(PKG.plugin)), d["plugin_root"])
        self.assertTrue(" " in d["plugin_root"])
        self.assertFalse(d["plugin_root"].startswith(str(REPO)))
        self.assertEqual(os.path.realpath(str(PKG.core)), d["core"])
        self.assertEqual(str(self.home), d["home"])
        self.assertEqual("default", d["home_source"])
        self.assertFalse(d["config_exists"])

    def test_launcher_works_through_a_symlink_and_from_any_directory(self):
        link_dir = PKG.root / "my bin dir"
        link_dir.mkdir(exist_ok=True)
        link = link_dir / "yobiru"
        if not link.exists():
            os.symlink(str(PKG.launcher), str(link))
        r, d = PKG.run_json(["paths"], self.user, launcher=link, cwd=PKG.root / "Plugin Cache")
        self.assertEqual(os.path.realpath(str(PKG.plugin)), d["plugin_root"])

    def test_python_entry_point_and_home_selection(self):
        import subprocess
        env = PKG.env(self.user, extra={"TAILMSG_HOME": str(self.user / "custom home")})
        r = subprocess.run([sys.executable, str(PKG.entry), "paths"], env=env, cwd=str(PKG.work),
                           capture_output=True, text=True)
        d = json.loads(r.stdout)
        self.assertEqual(str(self.user / "custom home"), d["home"])
        self.assertEqual("$TAILMSG_HOME", d["home_source"])
        r, d = PKG.run_json(["--home", str(self.user / "explicit"), "paths"], self.user)
        self.assertEqual("--home", d["home_source"])

    def test_documented_skill_relative_path_reaches_the_command(self):
        for skill in sorted((PKG.plugin / "skills").iterdir()):
            self.assertTrue((skill / ".." / ".." / "scripts" / "yobiru").resolve().is_file(), skill.name)

    def test_missing_python_fails_clearly(self):
        import subprocess
        bare = PKG.root / "bare bin"
        bare.mkdir(exist_ok=True)
        for tool in ("dirname", "readlink"):
            if not (bare / tool).exists():
                os.symlink(os.readlink(str(PKG.bin / tool)), str(bare / tool))
        r = subprocess.run(["/bin/sh", str(PKG.launcher), "doctor"], env={"HOME": str(self.user), "PATH": str(bare)},
                           capture_output=True, text=True)
        self.assertEqual(127, r.returncode)
        self.assertIn("Python 3.8+ is required", r.stderr)


class ReadOnlyTests(Base):
    def test_packaged_request_queries_need_no_network_and_create_no_ledger(self):
        self.write_config({})
        plugin_before, user_before = snapshot(PKG.plugin), snapshot(self.user, ignore_macos_caches=True)
        r = self.yobiru("request", "list", "--json")
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertEqual([], json.loads(r.stdout))
        r = self.yobiru("request", "status", "0011223344556677", "--json")
        self.assertNotEqual(0, r.returncode)
        self.assertIn("no tracked outgoing request", r.stderr)
        self.assertEqual(user_before, snapshot(self.user, ignore_macos_caches=True))
        self.assertEqual(plugin_before, snapshot(PKG.plugin))

    def test_install_and_read_only_commands_create_nothing(self):
        plugin_before, user_before = snapshot(PKG.plugin), snapshot(self.user, ignore_macos_caches=True)
        state = PKG.fake_state("ro", calls=str(PKG.root / "ro-calls.log"))
        for args in (["paths"], ["--help"], ["version"], ["status"], ["status", "--json"], ["doctor"],
                     ["doctor", "--json"], ["service-template", "systemd"], ["codex-bridge", "status"]):
            self.yobiru(*args, tailscale=True, extra={"FAKE_TS_STATE": str(state)})
        self.assertEqual(user_before, snapshot(self.user, ignore_macos_caches=True))
        self.assertEqual(plugin_before, snapshot(PKG.plugin))      # no __pycache__ or state in the plugin
        calls = [json.loads(x) for x in (PKG.root / "ro-calls.log").read_text().splitlines()]
        self.assertTrue(calls)
        self.assertTrue(all(c in (["status", "--json"], ["debug", "prefs"]) for c in calls), calls)

    def test_messaging_commands_refuse_without_config(self):
        for args in (["inbox"], ["send", "peer", "hi"], ["serve"]):
            r = self.yobiru(*args)
            self.assertEqual(2, r.returncode)
            self.assertIn("not set up", r.stderr)
        self.assertFalse(self.home.exists())

    def test_doctor_reports_missing_tailscale_and_config(self):
        r, d = PKG.run_json(["doctor", "--json"], self.user)
        self.assertEqual(1, r.returncode)
        checks = {c["check"]: c for c in d["checks"]}
        self.assertEqual("error", checks["tailscale"]["status"])
        self.assertIn("not found", checks["tailscale"]["detail"])
        self.assertEqual("error", checks["config"]["status"])
        self.assertFalse(d["changed"])

    def test_doctor_no_probe_marks_tailscale_unchecked(self):
        self.write_config({})
        r, d = PKG.run_json(["doctor", "--json", "--no-probe"], self.user, tailscale=True)
        ts = [c for c in d["checks"] if c["check"] == "tailscale"][0]
        self.assertEqual("info", ts["status"])
        self.assertIn("not checked", ts["detail"])

    def test_doctor_reports_bad_config_as_data(self):
        for raw in ('{"port": "oops"}', '{"allow": "laptop"}', "not json", "[1, 2]",
                    '{"agents": {"a/b": {}}}'):
            (self.home).mkdir(parents=True, exist_ok=True)
            (self.home / "config.json").write_text(raw)
            r, d = PKG.run_json(["doctor", "--json", "--no-probe"], self.user)
            self.assertEqual(1, r.returncode, raw)
            self.assertFalse(d["ok"])
            self.assertEqual("error", [c for c in d["checks"] if c["check"] == "config"][0]["status"], raw)

    def test_doctor_with_running_tailscale(self):
        self.write_config({"allow": ["laptop"], "bind": "127.0.0.1", "port": free_port()})
        state = PKG.fake_state("doc", shields_up=True)
        r, d = PKG.run_json(["doctor", "--json"], self.user, tailscale=True, extra={"FAKE_TS_STATE": str(state)})
        checks = {c["check"]: c for c in d["checks"]}
        self.assertEqual("ok", checks["tailscale"]["status"])
        self.assertEqual("error", checks["shields-up"]["status"])
        self.assertEqual("warn", checks["receiver"]["status"])


class SetupTests(Base):
    def test_fresh_setup_is_a_dry_run_until_apply(self):
        args = ["setup", "--allow", "laptop", "--agent", "codex", "--self-agent", "codex", "--default-agent", "codex"]
        r = self.yobiru(*args)
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertIn("dry run", r.stdout)
        self.assertFalse(self.home.exists())
        r = self.yobiru(*(args + ["--apply", "--json"]))
        res = json.loads(r.stdout)
        self.assertTrue(res["written"])
        self.assertEqual({"port": 7766, "access_policy": "allowlist", "allow": ["laptop"], "deny": [],
                          "agents": {"codex": {}}, "self_agent": "codex",
                          "default_agent": "codex"}, self.config())
        self.assertEqual(0o600, stat.S_IMODE((self.home / "config.json").stat().st_mode))
        self.assertEqual(["config.json"], sorted(p.name for p in self.home.iterdir()))

    def test_fresh_setup_defaults_to_tailnet_and_still_requires_apply(self):
        r = self.yobiru("setup", "--agent", "codex")
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertIn("tailnet", r.stdout)
        self.assertFalse(self.home.exists())
        r = self.yobiru("setup", "--agent", "codex", "--apply")
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertEqual("tailnet", self.config()["access_policy"])
        self.assertEqual([], self.config()["deny"])

    def test_existing_config_keeps_agents_adapters_and_unknown_keys(self):
        original = {"port": 7766, "allow": ["laptop"], "self_agent": "grok", "default_agent": "grok",
                    "on_message": ["/bin/true"], "future_field": {"x": 1},
                    "agents": {"grok": {"wake_adapter": {"type": "webhook", "url_file": "/nonexistent/u"}},
                               "私信联系人": {"wake_max": "batch"}}}
        path = self.write_config(original)
        before = path.read_bytes()
        os.chmod(str(path), 0o640)
        r = self.yobiru("setup", "--allow", "dev-server", "--allow", "LAPTOP", "--agent", "codex", "--apply", "--json")
        res = json.loads(r.stdout)
        self.assertTrue(res["written"], r.stdout)
        after = self.config()
        expected = json.loads(before)
        expected["allow"].append("dev-server")
        expected["agents"]["codex"] = {}
        self.assertEqual(expected, after)
        self.assertEqual(before, Path(res["backup"]).read_bytes())
        self.assertEqual(0o640, stat.S_IMODE(path.stat().st_mode))

    def test_identity_change_needs_replace(self):
        path = self.write_config({"allow": ["laptop"], "self_agent": "grok"})
        before = path.read_bytes()
        r = self.yobiru("setup", "--self-agent", "codex", "--apply")
        self.assertEqual(1, r.returncode)
        self.assertIn("--replace self_agent", r.stdout)
        self.assertEqual(before, path.read_bytes())
        r = self.yobiru("setup", "--self-agent", "codex", "--replace", "self_agent", "--apply")
        self.assertEqual(0, r.returncode, r.stdout)
        self.assertEqual("codex", self.config()["self_agent"])

    def test_equivalent_unicode_names_are_the_same_agent(self):
        decomposed = "café"
        self.write_config({"allow": ["laptop"], "self_agent": decomposed, "agents": {decomposed: {"wake_max": "batch"}}})
        r = self.yobiru("setup", "--agent", "café", "--self-agent", "café", "--apply", "--json")
        res = json.loads(r.stdout)
        self.assertEqual("none", res["action"], r.stdout)
        self.assertEqual([decomposed], list(self.config()["agents"]))

    def test_unsafe_names_are_rejected_before_any_write(self):
        for args in (["--agent", "a/b"], ["--agent", "x\ny"], ["--agent", " lead"], ["--allow", "evil peer"],
                     ["--allow", "../x"], ["--allow", "*"], ["--self-agent", ""]):
            r = self.yobiru(*(["setup", "--allow", "laptop"] + args + ["--apply"]))
            self.assertEqual(2, r.returncode, args)
        self.assertFalse(self.home.exists())


class WakeConfigTests(Base):
    def secret_file(self, name, mode=0o600):
        d = self.user / "secrets dir"
        d.mkdir(exist_ok=True)
        f = d / name
        f.write_text("value-not-printed\n")
        os.chmod(str(f), mode)
        return f

    def test_webhook_for_one_agent_preserves_the_others(self):
        self.write_config({"allow": ["laptop"], "default_agent": "codex",
                           "agents": {"codex": {"wake_adapter": {"type": "exec", "argv": ["/bin/true"]}},
                                      "grok": {}}})
        url, key = self.secret_file("url"), self.secret_file("key")
        r = self.yobiru("wake-config", "--agent", "grok", "--webhook", "--url-file", str(url), "--secret-file", str(key),
                        "--apply", "--json")
        res = json.loads(r.stdout)
        self.assertTrue(res["written"], r.stdout)
        agents = self.config()["agents"]
        self.assertEqual({"type": "webhook", "url_file": str(url), "secret_file": str(key)}, agents["grok"]["wake_adapter"])
        self.assertEqual({"wake_adapter": {"type": "exec", "argv": ["/bin/true"]}}, agents["codex"])
        self.assertNotIn("value-not-printed", r.stdout + r.stderr + (self.home / "config.json").read_text())

    def test_webhook_secret_files_must_exist_and_be_private(self):
        self.write_config({"allow": ["laptop"]})
        before = (self.home / "config.json").read_bytes()
        loose = self.secret_file("loose", 0o644)
        r = self.yobiru("wake-config", "--agent", "grok", "--webhook", "--url-file", str(loose), "--apply")
        self.assertEqual(1, r.returncode)
        self.assertIn("0600", r.stdout)
        r = self.yobiru("wake-config", "--agent", "grok", "--webhook", "--url-file", str(self.user / "missing"), "--apply")
        self.assertEqual(1, r.returncode)
        self.assertIn("does not exist", r.stdout)
        for raw in (["--url", "https://example.com/hook"], ["--secret", "s3cret"]):
            r = self.yobiru(*(["wake-config", "--agent", "grok", "--webhook"] + raw))
            self.assertEqual(2, r.returncode)                     # no way to pass a raw URL or key
        self.assertEqual(before, (self.home / "config.json").read_bytes())

    def test_inherited_top_level_adapter_is_not_replaced_silently(self):
        path = self.write_config({"allow": ["peer"], "default_agent": "codex", "on_message": ["/bin/true"]})
        before = path.read_bytes()
        r = self.yobiru("wake-config", "--agent", "codex", "--exec", FALSE, "--apply")
        self.assertEqual(1, r.returncode)
        self.assertIn("inherited top-level", r.stdout)
        self.assertEqual(before, path.read_bytes())
        r = self.yobiru("wake-config", "--agent", "codex", "--exec", FALSE, "--replace", "--apply")
        self.assertEqual(0, r.returncode, r.stdout)
        cfg = self.config()
        self.assertEqual({"type": "exec", "argv": [FALSE]}, cfg["agents"]["codex"]["wake_adapter"])
        self.assertEqual(["/bin/true"], cfg["on_message"])

    def test_own_adapter_and_cap_on_an_nfc_equivalent_key_need_replace(self):
        decomposed = "café"
        path = self.write_config({"allow": ["peer"], "agents": {decomposed: {
            "wake_adapter": {"type": "exec", "argv": ["/bin/true"]}, "wake_max": "batch"}}})
        before = path.read_bytes()
        r = self.yobiru("wake-config", "--agent", "café", "--exec", FALSE, "--wake-max", "now", "--apply")
        self.assertEqual(1, r.returncode)
        self.assertEqual(before, path.read_bytes())
        r = self.yobiru("wake-config", "--agent", "café", "--exec", FALSE, "--wake-max", "now",
                        "--replace", "--apply")
        self.assertEqual(0, r.returncode, r.stdout)
        agents = self.config()["agents"]
        self.assertEqual([decomposed], list(agents))
        self.assertEqual({"wake_adapter": {"type": "exec", "argv": [FALSE]}, "wake_max": "now"}, agents[decomposed])

    def test_remove_adapter_reports_the_real_fallback(self):
        self.write_config({"allow": ["peer"], "default_agent": "codex",
                           "wake_adapter": {"type": "exec", "argv": ["/bin/true"]},
                           "agents": {"codex": {"wake_adapter": {"type": "exec", "argv": ["/bin/false"]}},
                                      "helper": {"wake_adapter": {"type": "exec", "argv": ["/bin/false"]}}}})
        r, res = PKG.run_json(["wake-config", "--agent", "codex", "--remove-adapter", "--json"], self.user)
        self.assertIn("falls back to the top-level adapter: exec /bin/true", " ".join(res["changes"]))
        r, res = PKG.run_json(["wake-config", "--agent", "helper", "--remove-adapter", "--json"], self.user)
        self.assertIn("nobody is woken for @helper", " ".join(res["changes"]))
        self.assertFalse(res["written"])

    def test_policy_changes_need_replace_for_existing_values(self):
        self.write_config({"allow": ["peer"], "wake_policy": {"default_max": "batch", "peers": {"Laptop": "batch"}}})
        r = self.yobiru("wake-config", "--peer-max", "laptop=now", "--apply")
        self.assertEqual(1, r.returncode)
        r = self.yobiru("wake-config", "--peer-max", "laptop=now", "--quiet-hours", "23:00-08:00", "--tz", "UTC",
                        "--rate", "3", "--replace", "--apply")
        self.assertEqual(0, r.returncode, r.stdout)
        cfg = self.config()
        self.assertEqual({"default_max": "batch", "peers": {"Laptop": "now"}}, cfg["wake_policy"])
        self.assertEqual({"start": "23:00", "end": "08:00", "tz": "UTC"}, cfg["quiet_hours"])
        self.assertEqual(3, cfg["wake_rate"])

    def test_exec_adapter_needs_an_absolute_executable(self):
        self.write_config({"allow": ["peer"]})
        self.assertEqual(2, self.yobiru("wake-config", "--agent", "a", "--exec", "relative/notify").returncode)
        self.assertEqual(1, self.yobiru("wake-config", "--agent", "a", "--exec", "/nonexistent/notify").returncode)


class RuntimeAndServiceTests(Base):
    def test_install_runtime_never_replaces_a_different_copy_by_default(self):
        self.write_config({"allow": ["peer"]})
        target = self.home / "tailmsg.py"
        r = self.yobiru("install-runtime")
        self.assertEqual(0, r.returncode)
        self.assertFalse(target.exists())
        self.yobiru("install-runtime", "--apply")
        self.assertEqual(PKG.core.read_bytes(), target.read_bytes())
        target.write_text("# a different running receiver\n")
        r = self.yobiru("install-runtime", "--apply")
        self.assertEqual(1, r.returncode)
        self.assertEqual("# a different running receiver\n", target.read_text())
        r, res = PKG.run_json(["install-runtime", "--upgrade", "--apply", "--json"], self.user)
        self.assertTrue(res["written"])
        self.assertEqual(PKG.core.read_bytes(), target.read_bytes())
        self.assertEqual("# a different running receiver\n", Path(res["backup"]).read_text())

    def test_install_runtime_keeps_a_newer_runtime_unless_a_downgrade_is_allowed(self):
        """A development or later runtime (by SemVer) is never replaced by an older bundled copy by --upgrade
        alone: a beta plugin installed next to a development receiver must not silently downgrade it."""
        self.write_config({"allow": ["peer"]})
        target = self.home / "tailmsg.py"
        newer = 'PRODUCT_VERSION = "99.0.0-dev.1"\n# a development runtime with its own features\n'
        target.write_text(newer)
        r, res = PKG.run_json(["install-runtime", "--upgrade", "--apply", "--json"], self.user)
        self.assertEqual((1, "refused", False), (r.returncode, res["action"], res["written"]))
        self.assertEqual(("99.0.0-dev.1", PKG.product["version"]), (res["existing_version"], res["bundled_version"]))
        self.assertIn("--allow-downgrade", res["error"])
        self.assertEqual(newer, target.read_text())
        self.assertEqual([], sorted(p.name for p in self.home.glob("tailmsg.py.bak-*")), "nothing was written")
        r, res = PKG.run_json(["install-runtime", "--upgrade", "--allow-downgrade", "--apply", "--json"], self.user)
        self.assertEqual((0, "upgrade", True), (r.returncode, res["action"], res["written"]))
        self.assertEqual(PKG.core.read_bytes(), target.read_bytes())
        self.assertEqual(newer, Path(res["backup"]).read_text(), "the newer runtime is kept as the backup")
        target.write_text('PRODUCT_VERSION = "0.0.1"\n')                     # an older runtime needs no flag
        r, res = PKG.run_json(["install-runtime", "--upgrade", "--apply", "--json"], self.user)
        self.assertEqual((0, "upgrade", "0.0.1"), (r.returncode, res["action"], res["existing_version"]))

    def test_launchd_template_is_a_valid_plist(self):
        home = self.user / "data home"
        r = self.yobiru("--home", str(home), "service-template", "launchd")
        self.assertEqual(0, r.returncode)
        plist = plistlib.loads(r.stdout.encode("utf-8"))
        self.assertEqual("dev.tailmsg", plist["Label"])
        self.assertTrue(os.path.samefile(sys.executable, plist["ProgramArguments"][0]))
        self.assertEqual([str(home / "tailmsg.py"), "--home", str(home), "serve"],
                         plist["ProgramArguments"][1:])
        self.assertIn("install-runtime", r.stderr)
        self.assertIn("LaunchAgents", r.stderr)
        self.assertNotIn("EnvironmentVariables", plist, "no tailscale on PATH here: nothing to add")

    def test_launchd_template_adds_the_tailscale_directory_to_path(self):
        # launchd runs jobs with PATH=/usr/bin:/bin:/usr/sbin:/sbin; a bare `tailscale` found elsewhere
        # (the standalone macOS app installs /usr/local/bin/tailscale) must stay reachable from the service.
        r = self.yobiru("service-template", "launchd", tailscale=True)
        self.assertEqual(0, r.returncode, r.stderr)
        env = plistlib.loads(r.stdout.encode("utf-8"))["EnvironmentVariables"]
        self.assertEqual("/usr/bin:/bin:/usr/sbin:/sbin:" + str(PKG.fakebin), env["PATH"])
        self.write_config({"tailscale_cmd": ["/Applications/Tailscale.app/Contents/MacOS/Tailscale"]})
        r = self.yobiru("service-template", "launchd", tailscale=True)
        self.assertNotIn("EnvironmentVariables", plistlib.loads(r.stdout.encode("utf-8")),
                         "an absolute tailscale_cmd needs no PATH")

    def test_launchd_template_keeps_a_custom_tailscale_command_runnable(self):
        directory = self.user / "custom CLI"
        directory.mkdir()
        command = directory / "configured-tailscale"
        shutil.copyfile(str(PKG.fakebin / "tailscale"), str(command))
        command.chmod(0o755)
        state = PKG.fake_state("launchd-custom", ip4="127.0.0.42")
        argv = [command.name, "--state", str(state)]
        self.write_config({"tailscale_cmd": argv})
        before = snapshot(self.home)
        r = self.yobiru("service-template", "launchd", extra={
            "PATH": str(PKG.bin) + os.pathsep + str(directory) + os.pathsep + str(PKG.fakebin)})
        self.assertEqual(0, r.returncode, r.stderr)
        env = plistlib.loads(r.stdout.encode("utf-8"))["EnvironmentVariables"]
        self.assertEqual("/usr/bin:/bin:/usr/sbin:/sbin:" + str(directory), env["PATH"])
        self.assertEqual(before, snapshot(self.home), "template generation must stay read-only")
        # Exercise subprocess lookup using just the saved service environment, without the interactive PATH.
        result = subprocess.run(argv + ["ip", "-4"], env=dict(env, HOME=str(self.user)), cwd=str(PKG.work),
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("127.0.0.42", result.stdout.strip())

    def test_systemd_template_quotes_paths(self):
        home = self.user / "100% data $HOME"
        r = self.yobiru("--home", str(home), "service-template", "systemd-user")
        line = [x for x in r.stdout.splitlines() if x.startswith("ExecStart=")][0]
        self.assertIn('"%s"' % str(home / "tailmsg.py").replace("%", "%%").replace("$", "$$"), line)
        self.assertTrue(line.endswith(' "serve"'), line)

    def test_codex_bridge_needs_a_supported_host_and_an_explicit_socket(self):
        self.write_config({"allow": ["peer"]})
        r = self.yobiru("codex-bridge", "install", "--thread-id", str(uuid.uuid4()), "--dry-run")
        if sys.platform == "linux":
            self.assertEqual(2, r.returncode)
            self.assertIn("--codex-remote", r.stderr)
        elif sys.platform != "darwin":
            self.assertEqual(3, r.returncode)
            self.assertIn("host capability missing", r.stderr)
        r, res = PKG.run_json(["codex-bridge", "status"], self.user)
        self.assertFalse(res["installed"])


FAKE_SYSTEMCTL = """#!/bin/sh
printf '%s\\n' "$*" >> "$(dirname "$0")/systemctl.log"
exit 0
"""


@unittest.skipUnless(sys.platform == "linux", "Linux user-systemd bridge")
class LinuxCodexBridgeTests(Base):
    """`codex-bridge` through the unpacked plugin: forwarding, file-only installs and cache independence.
    A fake systemctl and fake codex sit on PATH and a temporary AF_UNIX socket stands in for the app-server."""

    def setUp(self):
        super().setUp()
        self.home = self.user / "data home 100% $X"
        self.write_config({"allow": ["peer"], "agents": {"grok": {"wake_adapter": {"type": "exec", "argv": ["/bin/true"]}}}})
        (self.home / "tailmsg.py").write_text("# receiver\n")
        self.fakes = self.user / "fake tools"
        self.fakes.mkdir()
        for name, text in (("systemctl", FAKE_SYSTEMCTL), ("codex", "#!/bin/sh\nexit 0\n")):
            (self.fakes / name).write_text(text)
            (self.fakes / name).chmod(0o755)
        self.codex_home = self.user / "codex home"
        self.codex_home.mkdir()
        sock_dir = Path(tempfile.mkdtemp(prefix="yobiru-cli-")).resolve()
        self.addCleanup(shutil.rmtree, str(sock_dir), True)
        os.chmod(str(sock_dir), 0o700)
        self.socket_path = sock_dir / "app.sock"
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(self.socket_path))
        self.listener.listen(4)
        self.addCleanup(self.listener.close)
        self.thread = str(uuid.uuid4())
        self.extra = {"PATH": "%s:%s" % (PKG.bin, self.fakes)}

    def bridge(self, *args):
        return self.yobiru("--home", str(self.home), "codex-bridge", *args, extra=self.extra)

    def install_args(self):
        return ["install", "--thread-id", self.thread, "--codex-remote", "unix://" + str(self.socket_path),
                "--codex-home", str(self.codex_home)]

    def systemctl_calls(self):
        log = self.fakes / "systemctl.log"
        return log.read_text().splitlines() if log.exists() else []

    def test_wrapper_forwards_to_the_linux_installer_without_writes(self):
        before, plugin_before = snapshot(self.user), snapshot(PKG.plugin)
        r = self.bridge(*(self.install_args() + ["--codex-path", str(self.fakes / "codex"), "--allow-replies", "--dry-run"]))
        self.assertEqual(0, r.returncode, r.stderr)
        plan = json.loads(r.stdout)
        self.assertEqual((self.thread, "unix://" + str(self.socket_path), str(self.fakes / "codex"),
                          str(self.codex_home), True, str(self.home)),
                         (plan["dispatcher_thread_id"], plan["codex_remote"], plan["codex_path"], plan["codex_home"],
                          plan["allow_replies"], plan["tailmsg_home"]))
        self.assertEqual(before, snapshot(self.user))
        self.assertEqual(plugin_before, snapshot(PKG.plugin))         # no __pycache__ in the plugin cache
        self.assertEqual([], self.systemctl_calls())
        r = self.bridge("install", "--thread-id", self.thread, "--codex-remote", "ws://127.0.0.1:4500", "--dry-run")
        self.assertEqual(1, r.returncode)
        self.assertIn("unix:///", r.stderr)

    def test_installed_bridge_runs_without_the_plugin_cache(self):
        r = self.bridge(*(self.install_args() + ["--no-start"]))
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertEqual([], self.systemctl_calls())
        cfg = self.config()
        adapter = cfg["agents"]["codex"]["wake_adapter"]["argv"]
        self.assertEqual(str(self.home / "codex-local" / "codex_bridge.py"), adapter[1])
        self.assertEqual({"wake_adapter": {"type": "exec", "argv": ["/bin/true"]}}, cfg["agents"]["grok"])
        unit = (self.user / ".config/systemd/user/tailmsg-codex-bridge.service").read_text()
        self.assertNotIn(str(PKG.cache), unit)                         # never runs from the plugin cache
        (self.home / "inbox").mkdir()
        moved = PKG.cache.with_name("moved away")
        os.rename(str(PKG.cache), str(moved))
        try:
            env = PKG.env(self.user)
            run = lambda *a: subprocess.run([sys.executable] + list(adapter[1:4]) + list(a), env=env,
                                            capture_output=True, text=True)
            status = run("status")
            self.assertEqual(0, status.returncode, status.stderr)
            self.assertEqual([], json.loads(status.stdout))
            wake = run("wake", str(self.home / "inbox" / "missing_0001.json"))
            self.assertEqual(1, wake.returncode)                       # rejected input, fixed reason only
            self.assertIn("codex_bridge: ", wake.stderr)
        finally:
            os.rename(str(moved), str(PKG.cache))

    def test_status_diagnoses_socket_and_rollback_is_file_only(self):
        r = self.bridge(*(self.install_args() + ["--no-start"]))
        receipt = json.loads(r.stdout)
        r, res = PKG.run_json(["--home", str(self.home), "codex-bridge", "status"], self.user, extra=self.extra)
        self.assertEqual(("unix_socket", "listening", True, False),
                         (res["transport"], res["socket"], res["codex_path_ok"], res["allow_replies"]))
        # A files-only install is not an active bridge: no daemon heartbeat, so nothing is reported healthy.
        self.assertEqual("unix_socket", res["diagnostics"]["capabilities"]["transport"])
        self.assertIn("daemon_not_confirmed", res["diagnostics"]["reasons"])
        self.assertTrue(any("daemon" in hint for hint in res["hints"]))
        self.listener.close()
        r, res = PKG.run_json(["--home", str(self.home), "codex-bridge", "status"], self.user, extra=self.extra)
        self.assertEqual("socket_stale", res["socket"])
        self.assertTrue(any("stale" in hint for hint in res["hints"]))
        r = self.bridge("rollback", receipt["backup"], "--no-start")
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertFalse((self.home / "codex-bridge.json").exists())
        self.assertNotIn("codex", self.config().get("agents", {}))
        self.assertEqual([], self.systemctl_calls())


class UnsafeInputTests(Base):
    def test_message_ids_cannot_reach_outside_the_inbox(self):
        path = self.write_config({"allow": ["peer"]})
        (self.home / "inbox").mkdir()
        before = path.read_bytes()
        for cmd in ("show", "ack", "fetch", "wake", "reply"):
            for bad in ("../config", "*", "", "a\nb", "/etc/passwd"):
                args = [cmd, bad] + (["text"] if cmd == "reply" else [])
                r = self.yobiru(*args)
                self.assertNotEqual(0, r.returncode, (cmd, bad))
        self.assertEqual(before, path.read_bytes())
        self.assertEqual([], list((self.home / "inbox").iterdir()))


if __name__ == "__main__":
    unittest.main()
