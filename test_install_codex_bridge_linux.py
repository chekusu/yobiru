"""Offline checks of the Linux user-systemd installer.

The installer runs as a subprocess with an isolated HOME, a fake `systemctl` that only records calls and
simulates unit state, a fake `codex` executable and temporary AF_UNIX sockets. No real systemd manager,
Codex runtime, receiver or user file is touched.
"""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
import uuid

INSTALLER = Path(__file__).resolve().with_name("install_codex_bridge_linux.py")
SERVICE = "tailmsg-codex-bridge.service"
FAKE_SYSTEMCTL = r"""#!%(python)s
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
state_path = os.path.join(here, "systemd-state.json")
state = json.load(open(state_path))
args = sys.argv[1:]
with open(os.path.join(here, "systemctl.jsonl"), "a") as log:
    log.write(json.dumps(args) + "\n")
assert args[0] == "--user", args
args = [a for a in args[1:] if a != "--quiet"]
unit = state.setdefault("units", {}).setdefault(args[-1] if len(args) > 1 else "", {"active": False, "enabled": False})
code = 0
if args[0] == "show-environment":
    code = 1 if state.get("manager_down") else 0
elif args[0] == "is-active":
    code = 0 if unit["active"] else 3
elif args[0] == "is-enabled":
    code = 0 if unit["enabled"] else 1
elif args[0] == "disable":
    if state.get("disable_fails"):
        sys.stderr.write("Failed to disable unit\n")
        code = 1
    else:
        unit["enabled"] = False
        unit["active"] = unit["active"] and "--now" not in args
elif args[0] == "enable":
    unit["enabled"] = True
elif args[0] in ("start", "restart"):
    unit["active"] = True
    unit["starts"] = unit.get("starts", 0) + 1
elif args[0] == "try-restart":
    code = 0 if unit["active"] else 0
json.dump(state, open(state_path, "w"))
sys.exit(code)
"""


def snapshot(path):
    out = {}
    for item in sorted(Path(path).rglob("*")):
        rel = item.relative_to(path).as_posix()
        if item.is_symlink():
            out[rel] = "link:" + os.readlink(str(item))
        elif item.is_dir():
            out[rel] = "dir"
        elif stat.S_ISSOCK(item.stat().st_mode):
            out[rel] = "socket"
        else:
            out[rel] = hashlib.sha256(item.read_bytes()).hexdigest()
    return out


def python_env(values):
    """Keep isolated test settings and the selected Linux interpreter's loader path."""
    env = dict(values)
    if sys.platform.startswith("linux") and os.environ.get("LD_LIBRARY_PATH"):
        env.setdefault("LD_LIBRARY_PATH", os.environ["LD_LIBRARY_PATH"])
    return env


class LinuxInstallerTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="yobiru-linux-")).resolve()
        self.addCleanup(shutil.rmtree, str(self.root), True)
        os.chmod(str(self.root), 0o700)
        self.fakes = self.root / "fakes"
        self.fakes.mkdir()
        systemctl = self.fakes / "systemctl"
        systemctl.write_text(FAKE_SYSTEMCTL % {"python": sys.executable})
        systemctl.chmod(0o755)
        self.set_systemd()
        self.cli = self.fakes / "codex"
        self.cli.write_text("#!/bin/sh\nexit 0\n")
        self.cli.chmod(0o755)
        self.user = self.root / "user"
        self.user.mkdir()
        self.home = self.user / "data 100% $HOME"
        self.home.mkdir()
        (self.home / "tailmsg.py").write_text("# existing receiver\n")
        self.original = {"port": 7766, "allow": ["peer-node"], "default_agent": "codex",
                         "wake_adapter": {"type": "webhook", "url_file": "/nonexistent/url"},
                         "agents": {"codex": {"wake_max": "now"},
                                    "helper": {"wake_adapter": {"type": "exec", "argv": ["/bin/true"]}}},
                         "future_field": {"kept": True}}
        self.write_config(self.original)
        os.chmod(str(self.home / "config.json"), 0o640)
        self.codex_home = self.user / "codex home"
        self.codex_home.mkdir()
        # macOS temp homes can exceed the Unix socket path limit once nested.
        # Keep the socket short while retaining the long, spaced data/config paths.
        self.socket_dir = Path(tempfile.mkdtemp(prefix="yobiru-sock-", dir="/tmp")).resolve()
        self.addCleanup(shutil.rmtree, str(self.socket_dir), True)
        self.socket_path = self.socket_dir / "app.sock"
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(self.socket_path))
        self.addCleanup(self.listener.close)
        self.remote = "unix://" + str(self.socket_path)
        self.thread = str(uuid.uuid4())
        self.unit = self.user / ".config" / "systemd" / "user" / SERVICE

    def set_systemd(self, **state):
        (self.fakes / "systemd-state.json").write_text(json.dumps(state))

    def systemd(self):
        return json.loads((self.fakes / "systemd-state.json").read_text())

    def unit_state(self):
        return self.systemd().get("units", {}).get(SERVICE, {"active": False, "enabled": False})

    def calls(self):
        log = self.fakes / "systemctl.jsonl"
        return [json.loads(x)[1:] for x in log.read_text().splitlines()] if log.exists() else []

    def clear_calls(self):
        log = self.fakes / "systemctl.jsonl"
        if log.exists():
            log.unlink()

    def write_config(self, config, home=None):
        (home or self.home).joinpath("config.json").write_text(json.dumps(config, indent=2))

    def run_installer(self, *args, env=None, home=None, path_dirs=None):
        environment = python_env({"HOME": str(self.user), "PATH": os.pathsep.join([str(self.fakes)] + (path_dirs or []) +
                                                                                   ["/usr/bin", "/bin"]),
                                  "LANG": "C.UTF-8"})
        environment.update(env or {})
        cmd = [sys.executable, str(INSTALLER), "--tailmsg-home", str(home or self.home)] + list(args)
        return subprocess.run(cmd, env=environment, capture_output=True, text=True, timeout=60)

    def install(self, *extra, **kw):
        args = ["--thread-id", self.thread, "--codex-remote", self.remote, "--codex-home", str(self.codex_home)]
        result = self.run_installer(*(args + list(extra)), **kw)
        return result

    def ok(self, result):
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def refused(self, result, needle):
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn(needle, result.stderr)

    def bridge_config(self, home=None):
        return json.loads(((home or self.home) / "codex-bridge.json").read_text())

    def create_queue(self, *thread_ids):
        path = self.home / "codex-bridge" / "bridge.sqlite3"
        path.parent.mkdir(exist_ok=True)
        with contextlib.closing(sqlite3.connect(str(path))) as db:
            db.execute("CREATE TABLE messages (id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, state TEXT NOT NULL)")
            for index, thread_id in enumerate(thread_ids):
                db.execute("INSERT INTO messages VALUES (?,?,?)", ("message_%04d" % index, thread_id, "uncertain"))
            db.commit()
        return path

    def exec_start(self):
        line = [x for x in self.unit.read_text().splitlines() if x.startswith("ExecStart=")][0]
        return systemd_split(line[len("ExecStart="):])

    # ------------------------------------------------------------ dry run and --no-start
    def test_dry_run_writes_nothing_and_never_calls_systemctl(self):
        before = snapshot(self.user)
        plan = self.ok(self.install("--dry-run"))
        self.assertTrue(plan["dry_run"])
        self.assertEqual(self.thread, plan["dispatcher_thread_id"])
        self.assertEqual(self.remote, plan["codex_remote"])
        self.assertEqual(str(self.codex_home), plan["codex_home"])
        self.assertIn("ExecStart=", plan["unit"])
        self.assertEqual(["daemon-reload", "enable " + SERVICE, "restart " + SERVICE, "try-restart tailmsg.service"],
                         plan["would_run_systemctl_user"])
        self.assertEqual(before, snapshot(self.user))
        self.assertEqual([], self.calls())

    def test_no_start_install_is_file_only_with_escaped_generic_paths(self):
        result = self.ok(self.install("--no-start"))
        self.assertFalse(result["started"])
        self.assertIn("systemctl --user restart " + SERVICE, result["next"])   # also reloads an upgrade
        self.assertNotIn("--now", result["next"])
        self.assertEqual([], self.calls())
        installed = self.home / "codex-local" / "codex_bridge.py"
        config_path = self.home / "codex-bridge.json"
        self.assertEqual([os.path.abspath(sys.executable), str(installed), "--config", str(config_path), "daemon"],
                         self.exec_start())
        unit = self.unit.read_text()
        self.assertIn("100%% $$HOME", unit)
        self.assertIn("WantedBy=default.target", unit)
        self.assertNotIn(str(INSTALLER.parent), unit)          # never runs from the source/plugin directory
        config = json.loads((self.home / "config.json").read_text())
        expected = json.loads(json.dumps(self.original))
        expected["agents"]["codex"]["wake_adapter"] = {
            "type": "exec", "argv": [os.path.abspath(sys.executable), str(installed), "--config", str(config_path), "wake"]}
        self.assertEqual(expected, config)                      # webhook, other agents and unknown keys kept
        self.assertEqual(0o640, stat.S_IMODE((self.home / "config.json").stat().st_mode))
        bridge = self.bridge_config()
        self.assertEqual((self.thread, self.remote, str(self.cli), str(self.codex_home), False),
                         (bridge["dispatcher_thread_id"], bridge["codex_remote"], bridge["codex_path"],
                          bridge["codex_home"], bridge["allow_replies"]))
        self.assertNotIn("excluded_kinds", bridge)
        self.assertFalse((self.home / "codex-bridge" / "bridge.sqlite3").exists())
        self.assertEqual([], [p.name for p in self.home.rglob("*.tmp")])

    def test_cli_discovered_on_path_or_given_explicitly(self):
        other = self.root / "explicit cli"
        other.mkdir()
        explicit = other / "codex"
        explicit.write_text("#!/bin/sh\nexit 0\n")
        explicit.chmod(0o755)
        self.ok(self.install("--no-start", "--codex-path", str(explicit)))
        self.assertEqual(str(explicit), self.bridge_config()["codex_path"])
        self.assertIn('"PATH=%s:/usr/local/bin' % other, self.unit.read_text())

    # ------------------------------------------------------------ refusals before any write
    def test_invalid_inputs_are_rejected_before_writes_or_service_calls(self):
        before = snapshot(self.user)
        cases = [
            ((["--thread-id", "dispatcher-name", "--codex-remote", self.remote]), "UUID"),
            ((["--thread-id", self.thread]), "--codex-remote"),
            ((["--thread-id", self.thread, "--codex-remote", "ws://127.0.0.1:4500"]), "unix:///"),
            ((["--thread-id", self.thread, "--codex-remote", "unix://"]), "unix:///"),
            ((["--thread-id", self.thread, "--codex-remote", self.remote, "--codex-path", "/nonexistent/codex"]),
             "not an executable"),
            ((["--thread-id", self.thread, "--codex-remote", self.remote, "--codex-home", str(self.root / "nope")]),
             "Codex home"),
            ((["--thread-id", self.thread, "--codex-remote", "unix://" + str(self.home / "config.json"),
               "--codex-home", str(self.codex_home)]),
             "not a Unix socket"),
        ]
        for args, needle in cases:
            with self.subTest(args=args):
                self.refused(self.run_installer(*args), needle)
        empty = self.root / "no-codex"
        empty.mkdir()
        result = subprocess.run([sys.executable, str(INSTALLER), "--tailmsg-home", str(self.home), "--thread-id",
                                 self.thread, "--codex-remote", self.remote, "--dry-run"],
                                env=python_env({"HOME": str(self.user), "PATH": str(empty)}), capture_output=True, text=True)
        self.refused(result, "--codex-path")
        self.assertEqual(before, snapshot(self.user))
        self.assertEqual([], self.calls())

    def test_unsafe_targets_and_foreign_adapter_are_refused(self):
        real = self.root / "elsewhere.json"
        real.write_text(json.dumps(self.original))
        (self.home / "config.json").unlink()
        (self.home / "config.json").symlink_to(real)
        self.refused(self.install("--no-start"), "not a regular file")
        (self.home / "config.json").unlink()
        config = json.loads(json.dumps(self.original))
        config["agents"]["codex"]["wake_adapter"] = {"type": "exec", "argv": ["/other/adapter"]}
        self.write_config(config)
        before = snapshot(self.user)
        self.refused(self.install("--no-start"), "different codex wake adapter")
        self.write_config(self.original)
        (self.home / "codex-local").symlink_to(self.root)
        self.refused(self.install("--no-start"), "real directory")
        (self.home / "codex-local").unlink()
        self.write_config(config)
        self.assertEqual(before, snapshot(self.user))
        self.assertEqual([], self.calls())

    def test_unreachable_user_manager_is_refused_before_writes(self):
        self.set_systemd(manager_down=True)
        before = snapshot(self.user)
        self.refused(self.install(), "user systemd manager is not reachable")
        self.assertEqual(before, snapshot(self.user))
        self.assertEqual([["show-environment"]], self.calls())

    def test_socket_must_be_this_users_and_missing_socket_is_only_a_note(self):
        self.listener.close()
        os.unlink(str(self.socket_path))
        plan = self.ok(self.install("--dry-run"))
        self.assertTrue(any("does not exist yet" in note for note in plan["notes"]))

    # ------------------------------------------------------------ fixed thread and queue
    def test_queue_and_config_guard_the_fixed_dispatcher(self):
        other = str(uuid.uuid4())
        for threads in ((other,), (self.thread, other)):
            with self.subTest(threads=threads):
                queue = self.create_queue(*threads)
                before = snapshot(self.user)
                for extra in (["--dry-run"], ["--no-start"]):
                    self.refused(self.install(*extra), "persistent dispatcher ID")
                self.assertEqual(before, snapshot(self.user))
                queue.unlink()
        self.ok(self.install("--no-start"))
        before = snapshot(self.user)
        self.thread = other
        self.refused(self.install("--no-start"), "persistent dispatcher ID")
        self.assertEqual(before, snapshot(self.user))
        self.assertEqual([], self.calls())

    def test_second_data_home_cannot_take_over_the_user_unit(self):
        self.ok(self.install("--no-start"))
        second = self.user / "second home"
        second.mkdir()
        (second / "tailmsg.py").write_text("# receiver\n")
        self.write_config(self.original, home=second)
        unit_before = self.unit.read_bytes()
        self.thread = str(uuid.uuid4())
        self.refused(self.install("--no-start", home=second), "another data home")
        self.assertEqual(unit_before, self.unit.read_bytes())
        self.assertFalse((second / "codex-bridge.json").exists())

    # ------------------------------------------------------------ managed install, upgrade, rollback
    def test_managed_install_upgrade_and_rollback_preserve_state_and_data(self):
        first = self.ok(self.install())
        self.assertTrue(first["started"])
        self.assertEqual([["show-environment"], ["is-active", "--quiet", SERVICE], ["is-enabled", "--quiet", SERVICE],
                          ["daemon-reload"], ["enable", SERVICE], ["restart", SERVICE],
                          ["try-restart", "tailmsg.service"]], self.calls())
        self.assertEqual({"active": True, "enabled": True}, {k: self.unit_state()[k] for k in ("active", "enabled")})
        queue = self.create_queue(self.thread)
        (self.home / "inbox").mkdir()
        inbox = self.home / "inbox" / "message_0001.json"
        inbox.write_text("{}")
        v1 = {p: (self.home / p).read_bytes() for p in ("codex-bridge.json", "config.json", "codex-local/codex_bridge.py")}
        v1_unit = self.unit.read_bytes()
        source = self.root / "source v2"
        shutil.copytree(str(INSTALLER.parent), str(source),
                        ignore=lambda d, names: [n for n in names if n not in (
                            "install_codex_bridge_linux.py", "install_codex_bridge.py", "codex_bridge.py",
                            "codex_queue_client.py", "codex_resident.py", "CODEX_DISPATCHER.md")])
        with open(str(source / "codex_bridge.py"), "a") as f:
            f.write("# version two\n")
        self.clear_calls()
        upgrade = subprocess.run([sys.executable, str(source / "install_codex_bridge_linux.py"), "--tailmsg-home",
                                  str(self.home), "--thread-id", self.thread, "--codex-remote", self.remote],
                                 env=python_env({"HOME": str(self.user), "PATH": str(self.fakes) + ":/usr/bin:/bin"}),
                                 capture_output=True, text=True)
        second = self.ok(upgrade)
        self.assertIn(["restart", SERVICE], self.calls())
        self.assertEqual(2, self.unit_state()["starts"])
        self.assertTrue((self.home / "codex-local/codex_bridge.py").read_text().endswith("# version two\n"))
        self.clear_calls()
        self.ok(self.run_installer("--rollback", second["backup"]))
        for rel, data in v1.items():
            self.assertEqual(data, (self.home / rel).read_bytes(), rel)
        self.assertEqual(v1_unit, self.unit.read_bytes())
        self.assertEqual(["disable", "--now", SERVICE], self.calls()[3])
        self.assertEqual({"active": True, "enabled": True}, {k: self.unit_state()[k] for k in ("active", "enabled")})
        self.clear_calls()
        self.ok(self.run_installer("--rollback", first["backup"]))
        self.assertEqual(json.loads(json.dumps(self.original)), json.loads((self.home / "config.json").read_text()))
        self.assertFalse(self.unit.exists())
        self.assertFalse((self.home / "codex-bridge.json").exists())
        self.assertFalse((self.home / "codex-local/codex_bridge.py").exists())
        self.assertEqual({"active": False, "enabled": False}, {k: self.unit_state()[k] for k in ("active", "enabled")})
        self.assertNotIn(["start", SERVICE], self.calls())
        self.assertTrue(queue.exists() and inbox.exists())
        with contextlib.closing(sqlite3.connect(str(queue))) as db:
            self.assertEqual([("uncertain",)], db.execute("SELECT state FROM messages").fetchall())

    def test_upgrade_installs_new_code_and_never_touches_queue_rows(self):
        self.ok(self.install("--no-start"))
        queue = self.home / "codex-bridge" / "bridge.sqlite3"
        with contextlib.closing(sqlite3.connect(str(queue))) as db:
            db.execute("""CREATE TABLE messages (id TEXT PRIMARY KEY, path TEXT NOT NULL, wake TEXT NOT NULL,
                state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL,
                received_at REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL, error TEXT,
                thread_id TEXT NOT NULL, host_id TEXT NOT NULL)""")
            for index, state in enumerate(("enqueued", "delivered", "uncertain", "sending", "skipped")):
                db.execute("INSERT INTO messages VALUES (?,?,'now',?,1,0,0,0,0,NULL,?,'local')",
                           ("message_%04d" % index, "/x/%d.json" % index, state, self.thread))
            db.commit()
        before = queue.read_bytes()
        self.ok(self.install("--no-start"))
        self.assertEqual(before, queue.read_bytes())       # the installer reads the queue, never migrates it
        for name in ("codex_bridge.py", "codex_queue_client.py", "codex_resident.py"):
            self.assertEqual((INSTALLER.parent / name).read_bytes(), (self.home / "codex-local" / name).read_bytes())
        self.assertEqual([], self.calls())                 # files only: active after the service restarts

    def test_rollback_keeps_files_when_the_bridge_cannot_be_stopped(self):
        receipt = self.ok(self.install())
        before = snapshot(self.user)
        self.set_systemd(disable_fails=True, units={SERVICE: {"active": True, "enabled": True}})
        self.clear_calls()
        self.refused(self.run_installer("--rollback", receipt["backup"]), "nothing was restored")
        self.assertEqual(before, snapshot(self.user))
        self.assertNotIn(["daemon-reload"], self.calls())

    def test_rollback_fails_closed_for_an_active_unit_without_its_file(self):
        receipt = self.ok(self.install())
        self.unit.unlink()                                      # still loaded and running in the manager
        before = snapshot(self.user)
        self.clear_calls()
        self.refused(self.run_installer("--rollback", receipt["backup"]), "nothing was restored")
        self.assertEqual(before, snapshot(self.user))
        self.assertTrue(self.unit_state()["active"])
        self.assertFalse(any(call[0] in ("disable", "daemon-reload", "start", "enable") for call in self.calls()))

    def test_no_start_and_dry_run_rollback_never_call_systemctl(self):
        receipt = self.ok(self.install("--no-start"))
        before = snapshot(self.user)
        plan = self.ok(self.run_installer("--rollback", receipt["backup"], "--dry-run"))
        self.assertIn(str(self.home / "config.json"), plan["would_restore"])
        self.assertEqual(before, snapshot(self.user))
        self.ok(self.run_installer("--rollback", receipt["backup"], "--no-start"))
        self.assertFalse(self.unit.exists())
        self.assertEqual([], self.calls())

    def test_rollback_validates_the_backup_and_unit_owner(self):
        first = self.ok(self.install("--no-start"))
        upgrade = self.ok(self.install("--no-start"))
        self.ok(self.run_installer("--rollback", first["backup"], "--no-start"))
        second = self.user / "second home"
        second.mkdir()
        (second / "tailmsg.py").write_text("# receiver\n")
        self.write_config(self.original, home=second)
        self.ok(self.install("--no-start", home=second))
        unit_before = self.unit.read_bytes()
        self.refused(self.run_installer("--rollback", upgrade["backup"], "--no-start"), "another data home")
        self.assertEqual(unit_before, self.unit.read_bytes())
        outside = self.root / "codex-bridge-linux-forged"
        shutil.copytree(upgrade["backup"], str(outside))
        self.refused(self.run_installer("--rollback", str(outside), "--dry-run"), "not a Linux bridge backup")
        manifest = Path(upgrade["backup"]) / "manifest.json"
        forged = json.loads(manifest.read_text())
        forged["targets"][0]["path"] = str(self.user / ".bashrc")
        manifest.write_text(json.dumps(forged))
        self.refused(self.run_installer("--rollback", upgrade["backup"], "--dry-run"), "unexpected target")
        self.assertEqual([], self.calls())

    def test_rollback_accepts_pre_resident_manifest_layout(self):
        first = self.ok(self.install("--no-start"))
        manifest_path = Path(first["backup"]) / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["targets"] = [entry for entry in manifest["targets"]
                               if not entry["path"].endswith("/codex_resident.py")]
        manifest_path.write_text(json.dumps(manifest))
        self.ok(self.run_installer("--rollback", first["backup"], "--dry-run"))
        self.ok(self.run_installer("--rollback", first["backup"], "--no-start"))
        self.assertEqual(self.original, json.loads((self.home / "config.json").read_text()))
        self.assertFalse(self.unit.exists())
        self.assertEqual([], self.calls())

    # ------------------------------------------------------------ pinned settings on upgrade
    def test_upgrade_from_another_ambient_account_keeps_pinned_settings(self):
        self.ok(self.install("--no-start", "--allow-replies"))
        ambient = self.root / "ambient"
        ambient.mkdir()
        (ambient / "codex").write_text("#!/bin/sh\nexit 0\n")
        (ambient / "codex").chmod(0o755)
        other_home = self.root / "other account"
        other_home.mkdir()
        self.ok(self.run_installer("--thread-id", self.thread, "--codex-remote", self.remote, "--no-start",
                                   env={"CODEX_HOME": str(other_home)}, path_dirs=[str(ambient)]))
        result = self.run_installer("--thread-id", self.thread, "--codex-remote", self.remote, "--dry-run",
                                    env={"CODEX_HOME": str(other_home),
                                         "PATH": str(ambient) + ":" + str(self.fakes) + ":/usr/bin:/bin"})
        plan = self.ok(result)
        self.assertEqual((str(self.codex_home), str(self.cli), True),
                         (plan["codex_home"], plan["codex_path"], plan["allow_replies"]))
        config = self.bridge_config()
        self.assertEqual((str(self.codex_home), str(self.cli), True),
                         (config["codex_home"], config["codex_path"], config["allow_replies"]))
        self.ok(self.install("--no-start", "--no-allow-replies"))
        self.assertFalse(self.bridge_config()["allow_replies"])

    def test_customized_instructions_survive_upgrade_and_rollback(self):
        self.ok(self.install("--no-start"))
        local = self.home / "codex-local" / "CODEX_DISPATCHER.md"
        self.assertEqual((INSTALLER.parent / "CODEX_DISPATCHER.md").read_bytes(), local.read_bytes())
        custom = "my own workflow, edited by the user\n".encode()
        local.write_bytes(custom)
        source = self.root / "source v2"
        source.mkdir()
        for name in ("install_codex_bridge_linux.py", "install_codex_bridge.py", "codex_bridge.py",
                     "codex_queue_client.py", "codex_resident.py", "CODEX_DISPATCHER.md"):
            shutil.copy2(str(INSTALLER.parent / name), str(source / name))
        with open(str(source / "CODEX_DISPATCHER.md"), "a") as f:
            f.write("\nnew template paragraph\n")
        with open(str(source / "codex_bridge.py"), "a") as f:
            f.write("# version two\n")
        env = python_env({"HOME": str(self.user), "PATH": str(self.fakes) + ":/usr/bin:/bin"})
        base = [sys.executable, str(source / "install_codex_bridge_linux.py"), "--tailmsg-home", str(self.home),
                "--thread-id", self.thread, "--codex-remote", self.remote, "--no-start"]
        plan = self.ok(subprocess.run(base + ["--dry-run"], env=env, capture_output=True, text=True))
        self.assertTrue(any("kept the installed dispatcher instructions" in n for n in plan["notes"]))
        upgrade = self.ok(subprocess.run(base, env=env, capture_output=True, text=True))
        self.assertEqual(custom, local.read_bytes())
        self.assertTrue((self.home / "codex-local/codex_bridge.py").read_text().endswith("# version two\n"))
        receipt = json.loads((self.home / "codex-bridge" / "installation.json").read_text())
        self.assertEqual(hashlib.sha256(custom).hexdigest(), receipt["files_sha256"]["CODEX_DISPATCHER.md"])
        self.ok(self.run_installer("--rollback", upgrade["backup"], "--no-start"))
        self.assertEqual(custom, local.read_bytes())
        self.assertFalse((self.home / "codex-local/codex_bridge.py").read_text().endswith("# version two\n"))

    def test_upgrade_preserves_routing_and_tuning_and_validates_them(self):
        self.ok(self.install("--no-start"))
        handler = self.fakes / "handler"
        handler.write_text("#!/bin/sh\nexit 0\n")
        handler.chmod(0o755)
        custom = {"excluded_kinds": ["routed-kind"], "excluded_kind_adapter": [str(handler), "--fixed"],
                  "excluded_kind_timeout_seconds": 20, "poll_seconds": 5,
                  "dispatcher_instructions": str(self.user / "my instructions.md")}
        path = self.home / "codex-bridge.json"
        config = json.loads(path.read_text())
        config.update(custom)
        path.write_text(json.dumps(config))
        plan = self.ok(self.install("--dry-run"))
        self.assertEqual((["routed-kind"], custom["dispatcher_instructions"]),
                         (plan["excluded_kinds"], plan["dispatcher_instructions"]))
        self.ok(self.install("--no-start"))
        config = self.bridge_config()
        for key, value in custom.items():
            self.assertEqual(value, config[key], key)
        handler.unlink()
        plan = self.ok(self.install("--dry-run"))
        self.assertTrue(any("excluded_kind_adapter" in note for note in plan["notes"]))
        config["excluded_kinds"] = "routed-kind"
        path.write_text(json.dumps(config))
        before = snapshot(self.user)
        for extra in ("--dry-run", "--no-start"):
            self.refused(self.install(extra), "excluded_kinds")
        self.assertEqual(before, snapshot(self.user))
        self.assertEqual([], self.calls())

    def test_upgrade_keeps_session_names_and_reports_the_dispatcher(self):
        self.ok(self.install("--no-start"))
        sessions = self.home / "sessions.json"
        sessions.write_text(json.dumps({"v": 1, "future": True, "agents": {
            "codex": {"runtime": "codex", "session_id": self.thread, "display_name": "Dispatcher"},
            "helper": {"runtime": "grok", "session_id": "chat-7", "confirmed_title": "Weekly"}}}))
        before = sessions.read_bytes()
        self.ok(self.install("--no-start"))
        self.assertEqual(before, sessions.read_bytes())                 # names and unknown keys survive
        core = load_core()
        bound, problems = core.session_bindings(self.home, ["codex", "helper"])
        self.assertEqual((self.thread, "codex-bridge", "Dispatcher"),
                         (bound["codex"]["session_id"], bound["codex"]["id_source"], bound["codex"]["display_name"]))
        self.assertEqual([], problems)

    def test_default_codex_home_is_pinned_against_later_ambient_homes(self):
        default = self.user / ".codex"
        default.mkdir()
        self.ok(self.run_installer("--thread-id", self.thread, "--codex-remote", self.remote, "--no-start"))
        self.assertEqual(str(default), self.bridge_config()["codex_home"])
        self.ok(self.run_installer("--thread-id", self.thread, "--codex-remote", self.remote, "--no-start",
                                   env={"CODEX_HOME": str(self.codex_home)}))
        self.assertEqual(str(default), self.bridge_config()["codex_home"])
        default.rmdir()
        (self.home / "codex-bridge.json").unlink()
        self.refused(self.run_installer("--thread-id", self.thread, "--codex-remote", self.remote, "--dry-run"),
                     "--codex-home")

    def test_first_install_uses_ambient_codex_home_only_without_pinned_config(self):
        result = self.run_installer("--thread-id", self.thread, "--codex-remote", self.remote, "--dry-run",
                                    env={"CODEX_HOME": str(self.codex_home)})
        plan = self.ok(result)
        self.assertEqual((str(self.codex_home), "$CODEX_HOME"), (plan["codex_home"], plan["codex_home_source"]))
        self.assertFalse(plan["allow_replies"])


def load_core():
    import importlib.util
    spec = importlib.util.spec_from_file_location("tailmsg_for_bridge_tests", str(INSTALLER.with_name("tailmsg.py")))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def systemd_split(text):
    """Enough of systemd's ExecStart= parsing for these tests: quoted words, C escapes, %% and $$."""
    words, i = [], 0
    while i < len(text):
        if text[i] == " ":
            i += 1
            continue
        assert text[i] == '"', text
        i += 1
        word = ""
        while text[i] != '"':
            if text[i] == "\\":
                i += 1
            word += text[i]
            i += 1
        i += 1
        words.append(word.replace("%%", "%").replace("$$", "$"))
    return words


if __name__ == "__main__":
    unittest.main()
