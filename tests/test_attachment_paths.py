"""macOS temporary roots keep the attachment path restrictions after realpath.

Simulate only the system aliases and the selected directory's stat/open; state,
files, inode checks and descriptor-relative reads use a real temporary directory.
No directories or links are created at filesystem root.
"""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from support.public_harness import PUBLIC  # noqa: F401 (adds the source client to sys.path)

import bot_client as P


class AttachmentRootPathTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="yobiru-paths-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.files = self.root / "files"
        self.files.mkdir()
        (self.files / "note.txt").write_text("Synthetic attachment")
        self.state = P.State(self.root / "data")

    def command(self, target, command="allow", platform="darwin", aliases=None, home="/Users/synthetic"):
        aliases = {"/tmp": "/private/tmp", "/var": "/private/var"} if aliases is None else aliases
        realpath, stat = os.path.realpath, os.stat

        def canonical(path):
            path = str(path)
            for alias, destination in aliases.items():
                if path == alias or path.startswith(alias + "/"):
                    return destination + path[len(alias):]
            return realpath(path)

        resolved = canonical(target)

        def directory_stat(path, *args, **kwargs):
            return stat(self.files if str(path) == resolved else path, *args, **kwargs)

        stdout, stderr, code = io.StringIO(), io.StringIO(), 0
        with mock.patch.object(P.sys, "platform", platform), \
                mock.patch.object(P.Path, "home", return_value=Path(home)), \
                mock.patch.object(P.os.path, "realpath", side_effect=canonical), \
                mock.patch.object(P.os.path, "islink", side_effect=lambda path: path in aliases), \
                mock.patch.object(P.os, "stat", side_effect=directory_stat), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                P.cmd_attachments(self.state, SimpleNamespace(attachments_cmd=command, dir=target))
            except SystemExit as e:
                code = e.code
        return code, json.loads(stdout.getvalue()) if stdout.getvalue() else None, stderr.getvalue()

    def assert_refused(self, path, reason="hidden, system or secret", **kwargs):
        before = self.state.load("attachments.json")
        code, _, error = self.command(path, **kwargs)
        self.assertEqual(2, code, (path, error))
        if reason:
            self.assertIn(reason, error)
        self.assertEqual(before, self.state.load("attachments.json"))

    def read(self, path, roots, platform="darwin", aliases=None):
        aliases = {"/tmp": "/private/tmp", "/var": "/private/var"} if aliases is None else aliases
        open_ = os.open

        def open_root(path, *args, **kwargs):
            return open_(self.files if path in [r["path"] for r in roots] else path, *args, **kwargs)

        def canonical_alias(path):
            self.assertIn(path, ("/tmp", "/var"), "never realpath an untrusted descendant")
            return aliases.get(path, path)

        with mock.patch.object(P.sys, "platform", platform), \
                mock.patch.object(P.os.path, "realpath", side_effect=canonical_alias), \
                mock.patch.object(P.os.path, "islink", side_effect=lambda path: path in aliases), \
                mock.patch.object(P.os, "open", side_effect=open_root):
            return P.read_attachment(self.state, path, roots, 1024)

    def test_temporary_export_roots_allow_alias_and_canonical_paths(self):
        for parent in ("/var/folders/synthetic", "/tmp"):
            for prefix in ("", "/private"):
                path = prefix + parent + "/export"
                with self.subTest(path=path):
                    code, result, error = self.command(path)
                    self.assertEqual(0, code, error)
                    self.assertIn("/private" + parent + "/export", result["roots"])

    def test_allowed_root_keeps_canonical_path_inode_and_safe_reads(self):
        alias = "/var/folders/synthetic/export"
        target = "/private" + alias
        code, _, error = self.command(alias)
        self.assertEqual(0, code, error)
        roots = self.state.load("attachments.json")["roots"]
        info = self.files.stat()
        self.assertEqual((target, info.st_dev, info.st_ino),
                         (roots[0]["path"], roots[0]["dev"], roots[0]["ino"]))
        (self.files / "private").mkdir()
        (self.files / "private" / "note.txt").write_text("Not an attachment")
        (self.files / "link.txt").symlink_to(self.files / "note.txt")
        (self.files / "linkdir").symlink_to(self.files, target_is_directory=True)
        for prefix in (target, alias):
            self.assertEqual({"name": "note.txt", "content": "Synthetic attachment"},
                             self.read(prefix + "/note.txt", roots))
            for name, reason in (("private/note.txt", "hold secrets"), ("link.txt", "symbolic link"),
                                 ("linkdir/note.txt", "symbolic link"), (".hidden/note.txt", "hidden"),
                                 ("../note.txt", "traversal")):
                with self.subTest(prefix=prefix, name=name), self.assertRaisesRegex(ValueError, reason):
                    self.read(prefix + "/" + name, roots)
        self.files.rename(self.root / "moved")
        self.files.mkdir()
        for prefix in (target, alias):
            with self.subTest(prefix=prefix), self.assertRaisesRegex(ValueError, "replaced"):
                self.read(prefix + "/note.txt", roots)
        code, result, error = self.command(alias, command="disallow")
        self.assertEqual(0, code, error)
        self.assertEqual([], result["roots"])

    def test_reads_accept_only_verified_darwin_aliases(self):
        for alias in ("/tmp", "/var"):
            target = "/private" + alias + "/export"
            code, _, error = self.command(target)
            self.assertEqual(0, code, error)
            roots = self.state.load("attachments.json")["roots"]
            path = alias + "/export/note.txt"
            self.assertEqual("Synthetic attachment", self.read(path, roots)["content"])
            for options in ({"platform": "linux"}, {"aliases": {}}, {"aliases": {alias: "/elsewhere"}}):
                with self.subTest(alias=alias, options=options), self.assertRaisesRegex(ValueError, "outside"):
                    self.read(path, roots, **options)
            for outside in (alias + "other/export/note.txt", alias + "/export-other/note.txt"):
                with self.subTest(path=outside), self.assertRaisesRegex(ValueError, "outside"):
                    self.read(outside, roots)

    def test_private_exception_requires_darwin_and_exact_system_symlink(self):
        path = "/private/var/folders/synthetic/export"
        for options in ({"platform": "linux"}, {"aliases": {}}, {"aliases": {"/var": "/elsewhere"}}):
            with self.subTest(options=options):
                self.assert_refused(path, **options)

    def test_only_temporary_descendants_qualify_for_private_exception(self):
        for path in ("/private", "/private/tmp", "/private/var", "/private/etc/export",
                     "/private/custom/export", "/private/various/export"):
            with self.subTest(path=path):
                self.assert_refused(path, reason=None, aliases={"/tmp": "/private/tmp", "/var": "/private/var",
                                                               "/etc": "/private/etc", "/custom": "/private/custom"})

    def test_remaining_hidden_system_and_secret_components_stay_refused(self):
        for component in (".hidden", "private", "PRIVATE", "secrets", "credentials", "keys", "keychains",
                          "Library", "AppData"):
            for parent in ("/private/var/folders/synthetic", "/private/tmp", "/Users/synthetic"):
                with self.subTest(component=component, parent=parent):
                    self.assert_refused(parent + "/" + component + "/export")

    def test_home_and_data_home_are_still_protected(self):
        for target, options in (("/private/var/folders/synthetic", {"home": "/var/folders/synthetic"}),
                                ("/private/var/folders", {"home": "/var/folders/synthetic"}),
                                (str(self.state.home), {})):
            with self.subTest(target=target):
                code, _, error = self.command(target, **options)
                self.assertEqual(2, code, error)
                self.assertIn("home", error)
                self.assertEqual([], self.state.load("attachments.json")["roots"])
