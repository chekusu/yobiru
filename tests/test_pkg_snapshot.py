"""Keep read-only snapshots sensitive to writes outside macOS runtime caches."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import pkg


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="yobiru snapshot ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def write(self, relative, contents="test"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
        return path

    def test_macos_home_snapshot_ignores_new_framework_cache_tree(self):
        with mock.patch.object(pkg.sys, "platform", "darwin"):
            before = pkg.snapshot(self.root, ignore_macos_caches=True)
            cache = self.write("Library/Caches/framework/nested/cache")
            self.assertEqual(before, pkg.snapshot(self.root, ignore_macos_caches=True))
            cache.write_text("changed runtime cache")
            self.assertEqual(before, pkg.snapshot(self.root, ignore_macos_caches=True))

    def test_macos_home_snapshot_still_detects_application_writes(self):
        with mock.patch.object(pkg.sys, "platform", "darwin"):
            self.write("Library/Caches/framework/cache")
            before = pkg.snapshot(self.root, ignore_macos_caches=True)
            for relative in (".tailmsg/config.json", "Library/Preferences/settings",
                             "Library/Caches-other/cache", "nested/Library/Caches/cache"):
                with self.subTest(relative=relative):
                    path = self.write(relative)
                    after = pkg.snapshot(self.root, ignore_macos_caches=True)
                    self.assertIn(relative, after)
                    self.assertNotEqual(before, after)
                    path.write_text("changed application data")
                    self.assertNotEqual(after, pkg.snapshot(self.root, ignore_macos_caches=True))

    def test_cache_exclusion_requires_macos_and_explicit_home_opt_in(self):
        self.write("Library/Caches/framework/cache")
        with mock.patch.object(pkg.sys, "platform", "darwin"):
            self.assertIn("Library/Caches/framework/cache", pkg.snapshot(self.root))
        with mock.patch.object(pkg.sys, "platform", "linux"):
            self.assertIn("Library/Caches/framework/cache",
                          pkg.snapshot(self.root, ignore_macos_caches=True))

    def test_macos_home_snapshot_ignores_caches_with_existing_launch_agents(self):
        agent = self.write("Library/LaunchAgents/synthetic.plist")
        with mock.patch.object(pkg.sys, "platform", "darwin"):
            before = pkg.snapshot(self.root, ignore_macos_caches=True)
            self.write("Library/Caches/framework/cache")
            self.assertEqual(before, pkg.snapshot(self.root, ignore_macos_caches=True))
            agent.write_text("changed launch agent")
            self.assertNotEqual(before, pkg.snapshot(self.root, ignore_macos_caches=True))

    def test_macos_home_snapshot_detects_new_empty_launch_agents_directory(self):
        self.write("Library/Caches/framework/cache")
        with mock.patch.object(pkg.sys, "platform", "darwin"):
            before = pkg.snapshot(self.root, ignore_macos_caches=True)
            (self.root / "Library" / "LaunchAgents").mkdir()
            after = pkg.snapshot(self.root, ignore_macos_caches=True)
            self.assertNotEqual(before, after)
            self.assertEqual("dir", after["Library/LaunchAgents"])

    def test_macos_home_snapshot_records_files_named_library_or_caches(self):
        with mock.patch.object(pkg.sys, "platform", "darwin"):
            library = self.write("Library")
            self.assertIn("Library", pkg.snapshot(self.root, ignore_macos_caches=True))
            library.unlink()
            self.write("Library/Caches")
            self.assertIn("Library/Caches", pkg.snapshot(self.root, ignore_macos_caches=True))

    def test_macos_home_snapshot_records_symlink_named_caches(self):
        target = self.root / "runtime-cache"
        target.mkdir()
        library = self.root / "Library"
        library.mkdir()
        (library / "Caches").symlink_to(target, target_is_directory=True)
        with mock.patch.object(pkg.sys, "platform", "darwin"):
            self.assertIn("Library/Caches", pkg.snapshot(self.root, ignore_macos_caches=True))
