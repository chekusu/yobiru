"""Service Python paths, using temporary Homebrew/venv layouts, never a real install."""
import contextlib
import importlib.util
import io
import os
import plistlib
import shutil
import subprocess
import tempfile
import unittest
import venv
from pathlib import Path
from unittest import mock

from support.pkg import REPO

spec = importlib.util.spec_from_file_location(
    "yobiru_service_python_under_test", REPO / "plugins/yobiru/scripts/yobiru.py")
Y = importlib.util.module_from_spec(spec)
spec.loader.exec_module(Y)


class ServicePythonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="yobiru service python ")
        self.addCleanup(self.tmp.cleanup)
        # macOS temporary-directory aliases may differ from the venv's canonical sys.prefix.
        self.root = Path(self.tmp.name).resolve()
        self.prefix = self.root / "brew"
        self.keg = self.prefix / "Cellar/python@3.13/3.13.1"
        self.relative = Path("Frameworks/Python.framework/Versions/3.13/bin/python3.13")
        self.executable = self.keg / self.relative
        self.make_python(self.executable, "old")
        self.home = self.root / "data"

    def make_python(self, path, version):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nprintf '%s\\n' " + version + "\n")
        path.chmod(0o755)

    def link(self, path, target):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(target)
        return path

    def templates(self, executable, prefix=None):
        """Use the real CLI entry points, including launchd serialization."""
        result = []
        with mock.patch.object(Y.sys, "executable", str(executable)), \
                mock.patch.object(Y.sys, "prefix", str(prefix or self.keg)), \
                mock.patch.object(Y.sys, "base_prefix", str(self.keg)):
            for command in (["service-template", "launchd"], ["update", "reminder-template", "launchd"]):
                out = io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(0, Y.main(["--home", str(self.home)] + command))
                result.append(plistlib.loads(out.getvalue().encode())["ProgramArguments"])
        return result

    def assert_interpreter(self, expected, executable=None, prefix=None):
        for args in self.templates(executable or self.executable, prefix=prefix):
            self.assertEqual(str(expected), args[0])

    def test_existing_stable_symlink_survives_upgrade_and_cleanup(self):
        stable = self.link(self.prefix / "bin/python3", self.executable)
        templates = self.templates(stable) + self.templates(self.executable)
        for args in templates:
            self.assertEqual(str(stable), args[0])
        replacement = self.prefix / "Cellar/python@3.13/3.13.2" / self.relative
        self.make_python(replacement, "new")
        stable.unlink()
        stable.symlink_to(replacement)
        shutil.rmtree(self.keg)
        for args in templates:
            self.assertEqual("new", subprocess.check_output(args, text=True).strip())

    def test_already_versioned_executable_finds_matching_stable_bin(self):
        stable = self.link(self.prefix / "bin/python3", self.executable)
        self.assert_interpreter(stable)

    def test_formula_opt_path_when_prefix_python_is_another_interpreter(self):
        other = self.prefix / "bin/python3"
        self.make_python(other, "unrelated")
        opt = self.link(self.prefix / "opt/python@3.13", self.keg)
        self.assert_interpreter(opt / self.relative)

    def test_minor_version_link_is_used_if_python3_is_not_a_match(self):
        self.make_python(self.prefix / "bin/python3", "unrelated")
        stable = self.link(self.prefix / "bin/python3.13", self.executable)
        self.assert_interpreter(stable)

    def test_missing_broken_non_executable_or_mismatched_links_keep_current_python(self):
        self.assert_interpreter(self.executable)
        stable = self.link(self.prefix / "bin/python3", self.root / "missing")
        self.assert_interpreter(self.executable)
        stable.unlink()
        self.make_python(stable, "unrelated")
        self.assert_interpreter(self.executable)
        stable.unlink()
        stable.symlink_to(self.executable)
        self.executable.chmod(0o644)
        self.assert_interpreter(self.executable)

    def test_unrelated_path_python_is_never_selected(self):
        path_python = self.root / "path/bin/python3"
        self.make_python(path_python, "unrelated")
        with mock.patch.dict(os.environ, {"PATH": str(path_python.parent)}):
            self.assert_interpreter(self.executable)

    def test_virtual_environment_entrypoint_is_preserved(self):
        stable = self.link(self.prefix / "bin/python3", self.executable)
        venv = self.root / "venv"
        entry = self.link(venv / "bin/python3", stable)
        self.assert_interpreter(entry, executable=entry, prefix=venv)
        # Even a venv physically inside a Cellar keg must not be replaced.
        self.assert_interpreter(self.executable, prefix=venv)

    def test_non_homebrew_symlink_is_preserved(self):
        entry = self.link(self.root / "other/bin/python3", self.executable)
        self.assert_interpreter(entry, executable=entry)

    def test_real_virtual_environment_remains_active_in_saved_templates(self):
        directory = self.root / "real venv"
        venv.EnvBuilder(with_pip=False, symlinks=True).create(str(directory))
        executable = directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        for command in (["service-template", "launchd"], ["update", "reminder-template", "launchd"]):
            result = subprocess.run([str(executable), Y.__file__, "--home", str(self.home)] + command,
                                    capture_output=True, check=True)
            chosen = plistlib.loads(result.stdout)["ProgramArguments"][0]
            prefix = subprocess.check_output([chosen, "-c", "import sys; print(sys.prefix)"], text=True).strip()
            self.assertEqual(str(directory), prefix)

    def test_explicit_python_override_is_unchanged(self):
        explicit = str(self.root / "chosen python")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(0, Y.main(["--home", str(self.home), "service-template", "launchd",
                                        "--python", explicit]))
        self.assertEqual(explicit, plistlib.loads(out.getvalue().encode())["ProgramArguments"][0])

    def test_all_service_formats_use_stable_entrypoint(self):
        stable = self.link(self.prefix / "bin/python3", self.executable)
        commands = [["service-template", kind] for kind in ("systemd", "systemd-user", "windows")]
        commands += [["update", "reminder-template", kind] for kind in ("systemd-user", "cron")]
        with mock.patch.object(Y.sys, "executable", str(stable)):
            for command in commands:
                with self.subTest(command=command):
                    out = io.StringIO()
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                        self.assertEqual(0, Y.main(["--home", str(self.home)] + command))
                    self.assertIn(str(stable), out.getvalue())
                    self.assertNotIn(str(self.executable), out.getvalue())
