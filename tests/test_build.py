"""Packaging: generated files in sync, manifests valid, archives deterministic and complete."""
import contextlib
import copy
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from support.pkg import REPO, load_builder

B = load_builder()
P = B.load_product()
CODEX_VALIDATOR = Path("/opt/codex-skills/skills/.system/plugin-creator/scripts/validate_plugin.py")


def fixture_copy(dst):
    """A minimal repository copy: plugin, catalogs and packaging metadata."""
    for rel in (P["pluginDir"], ".agents", ".cursor-plugin", ".claude-plugin", "packaging"):
        shutil.copytree(str(REPO / rel), str(dst / rel), symlinks=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy2(str(REPO / "LICENSE"), str(dst / "LICENSE"))
    return dst


class RepositoryTests(unittest.TestCase):
    def test_tree_is_in_sync_and_valid(self):
        self.assertEqual([], B.drift(P))
        self.assertEqual([], B.validate(P))

    def test_bundled_copies_match_canonical_sources(self):
        sources = json.loads((REPO / P["pluginDir"] / "scripts" / "SOURCES.json").read_text())["files"]
        self.assertIn("scripts/tailmsg.py", sources)
        for dst, info in sources.items():
            self.assertEqual((REPO / info["source"]).read_bytes(), (REPO / P["pluginDir"] / dst).read_bytes(), dst)

    def test_manifests_agree_on_identity(self):
        base = REPO / P["pluginDir"]
        manifests = [json.loads((base / rel).read_text()) for rel in
                     ("plugin.json", ".codex-plugin/plugin.json", ".cursor-plugin/plugin.json",
                      ".claude-plugin/plugin.json")]
        self.assertEqual({P["id"]}, {m["name"] for m in manifests})
        self.assertEqual({P["version"]}, {m["version"] for m in manifests})
        self.assertEqual({P["repository"]}, {m["repository"] for m in manifests})
        self.assertEqual({P["homepage"]}, {m["homepage"] for m in manifests})
        oa = manifests[0]["extensions"]["com.openai"]
        self.assertIs(True, oa["requires_local_executor"])
        self.assertEqual(manifests[1]["interface"],
                         {k: v for k, v in oa["interface"].items() if k != "supportURL"})
        self.assertEqual(P["openai"]["supportURL"], oa["interface"]["supportURL"])
        self.assertNotIn("requires_local_executor", manifests[1])  # keep older Codex validators usable
        self.assertNotIn("extensions", manifests[1])
        codex_market = json.loads((REPO / ".agents/plugins/marketplace.json").read_text())
        cursor_market = json.loads((REPO / ".cursor-plugin/marketplace.json").read_text())
        self.assertEqual("./" + P["pluginDir"], codex_market["plugins"][0]["source"]["path"])
        self.assertEqual(P["pluginDir"], cursor_market["plugins"][0]["source"])
        claude_market = json.loads((REPO / ".claude-plugin/marketplace.json").read_text())
        self.assertEqual("./" + P["pluginDir"], claude_market["plugins"][0]["source"])
        self.assertEqual(P["version"], claude_market["plugins"][0]["version"])

    def test_claude_code_manifest_runs_the_bundled_mcp_server(self):
        manifest = json.loads((REPO / P["pluginDir"] / ".claude-plugin" / "plugin.json").read_text())
        self.assertEqual({"yobiru": {"command": "sh", "args": ["${CLAUDE_PLUGIN_ROOT}/scripts/yobiru", "mcp"]}},
                         manifest["mcpServers"])
        self.assertNotIn("skills", manifest)                     # default discovery finds skills/
        for shared in ("mcp.json", ".mcp.json"):                 # other hosts would read a shared file
            self.assertFalse((REPO / P["pluginDir"] / shared).exists())

    @unittest.skipUnless(shutil.which("claude"), "Claude Code CLI not installed")
    def test_claude_code_validator_accepts_plugin_and_marketplace(self):
        for target in (REPO, REPO / P["pluginDir"]):
            r = subprocess.run(["claude", "plugin", "validate", "--strict", str(target)], capture_output=True,
                               text=True, timeout=120)
            self.assertEqual(0, r.returncode, r.stdout + r.stderr)

    @unittest.skipUnless(CODEX_VALIDATOR.is_file(), "Codex plugin-creator validator not installed")
    def test_official_codex_validator_accepts_plugin(self):
        try:
            import yaml  # noqa: F401  (the validator needs PyYAML)
        except ImportError:
            self.skipTest("PyYAML not installed")
        r = subprocess.run([sys.executable, str(CODEX_VALIDATOR), str(REPO / P["pluginDir"])],
                           capture_output=True, text=True)
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)

    def test_skills_are_discoverable_and_locate_the_bundled_command(self):
        skills = sorted((REPO / P["pluginDir"] / "skills").iterdir())
        self.assertTrue({"yobiru-grok-bot", "yobiru-inbox", "yobiru-send", "yobiru-sessions", "yobiru-setup", "yobiru-upgrade",
                         "yobiru-wake"}.issubset({s.name for s in skills}))
        for s in skills:
            text = (s / "SKILL.md").read_text()
            self.assertTrue(text.startswith("---\nname: %s\n" % s.name))
            self.assertIn("../../scripts/yobiru", text)
            self.assertTrue((s / ".." / ".." / "scripts" / "yobiru").resolve().is_file())


class ArchiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.archives = B.archives(P)
        cls.plugin_zip = cls.archives["%s-%s-plugin.zip" % (P["id"], P["version"])]
        cls.market_zip = cls.archives["%s-%s-marketplace.zip" % (P["id"], P["version"])]

    def names(self, data):
        return zipfile.ZipFile(io.BytesIO(data)).namelist()

    def test_build_is_deterministic(self):
        self.assertEqual(self.archives, B.archives(P))

    def test_plugin_archive_holds_exactly_the_plugin(self):
        names = self.names(self.plugin_zip)
        self.assertEqual(sorted(names), names)
        expected = ["%s/%s" % (P["id"], rel) for rel in B.plugin_files(P)]
        self.assertEqual(expected, names)
        for rel in ("plugin.json", ".codex-plugin/plugin.json", ".cursor-plugin/plugin.json", "README.md",
                    "LICENSE", "scripts/yobiru", "scripts/yobiru.py", "scripts/tailmsg.py",
                    "scripts/codex_bridge/install_codex_bridge.py", "assets/icon.png",
                    "skills/yobiru-setup/SKILL.md", "skills/yobiru-send/SKILL.md",
                    "skills/yobiru-inbox/SKILL.md", "skills/yobiru-wake/SKILL.md",
                    "skills/yobiru-sessions/SKILL.md", "skills/yobiru-upgrade/SKILL.md"):
            self.assertIn("%s/%s" % (P["id"], rel), names)

    def test_archive_has_no_runtime_state_or_junk(self):
        for name in self.names(self.plugin_zip) + self.names(self.market_zip):
            parts = name.split("/")
            self.assertFalse(set(parts) & {"__pycache__", "inbox", "outbox", "test-run", ".live-test", "dist"}, name)
            self.assertFalse(name.endswith((".pyc", ".sqlite3", ".log", ".bak", "config.json")), name)

    def test_plugin_archive_is_a_single_openai_submission_root(self):
        # The official portal accepts one root OR one top-level plugin directory.
        # Submit the plugin ZIP, not the repository/marketplace ZIP.
        with zipfile.ZipFile(io.BytesIO(self.plugin_zip)) as archive:
            infos = archive.infolist()
            self.assertEqual({P["id"]}, {i.filename.split("/", 1)[0] for i in infos})
            self.assertIn(P["id"] + "/plugin.json", archive.namelist())
            self.assertLessEqual(len(infos), 5000)
            self.assertLessEqual(len(self.plugin_zip), 100 * 1024 * 1024)
            self.assertLessEqual(sum(i.file_size for i in infos), 512 * 1024 * 1024)
            for info in infos:
                self.assertLessEqual(len(info.filename.split("/")), 20)
            portable = json.loads(archive.read(P["id"] + "/plugin.json"))
            oa = portable["extensions"]["com.openai"]
            self.assertIn(P["id"] + "/" + oa["onboardingSkill"][2:], archive.namelist())
            self.assertNotIn("screenshots", oa["interface"])

    def test_modes_and_timestamps_are_fixed(self):
        for info in zipfile.ZipFile(io.BytesIO(self.plugin_zip)).infolist():
            self.assertEqual((1980, 1, 1, 0, 0, 0), info.date_time)
            mode = info.external_attr >> 16
            want = 0o100755 if info.filename.split("/", 1)[1] in B.executables(P) else 0o100644
            self.assertEqual(want, mode, info.filename)

    def test_marketplace_bundle_is_a_usable_repository_layout(self):
        names = self.names(self.market_zip)
        root = "%s-marketplace/" % P["id"]
        for rel in (".agents/plugins/marketplace.json", ".cursor-plugin/marketplace.json",
                    ".claude-plugin/marketplace.json", "LICENSE", P["pluginDir"] + "/.claude-plugin/plugin.json",
                    P["pluginDir"] + "/.codex-plugin/plugin.json", P["pluginDir"] + "/skills/yobiru-setup/SKILL.md"):
            self.assertIn(root + rel, names)


class ReleaseFilesTests(unittest.TestCase):
    def test_release_writes_this_versions_archives_and_their_checksums_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, B.cmd_release(P, tmp))
            plugin = "%s-%s-plugin.zip" % (P["id"], P["version"])          # the exact version, prerelease included
            market = "%s-%s-marketplace.zip" % (P["id"], P["version"])
            self.assertEqual(sorted([plugin, market, "SHA256SUMS"]), sorted(os.listdir(tmp)))
            sums = [line.split("  ") for line in Path(tmp, "SHA256SUMS").read_text().splitlines()]
            self.assertEqual([market, plugin], [name for _, name in sums])
            for digest, name in sums:
                self.assertEqual(B.sha256(Path(tmp, name).read_bytes()), digest)
            with zipfile.ZipFile(str(Path(tmp, plugin))) as z:
                for rel in ("plugin.json", ".codex-plugin/plugin.json", ".cursor-plugin/plugin.json"):
                    self.assertEqual(P["version"], json.loads(z.read("%s/%s" % (P["id"], rel)).decode())["version"])
                self.assertIn('\nPRODUCT_VERSION = "%s"' % P["version"],
                              z.read("%s/scripts/tailmsg.py" % P["id"]).decode("utf-8"))


class FixtureTests(unittest.TestCase):
    """The checks must catch what would make an archive differ from the checked tree."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = fixture_copy(Path(self.tmp.name))
        self.base = self.root / P["pluginDir"]

    def test_fixture_copy_is_clean(self):
        self.assertEqual([], B.validate(P, self.root))
        self.assertEqual([], B.drift(P, self.root))

    def mutate_portable(self, mutate):
        path = self.base / "plugin.json"
        manifest = json.loads(path.read_text())
        mutate(manifest)
        path.write_text(json.dumps(manifest))
        return "\n".join(B.validate(P, self.root))

    def test_openai_extension_cannot_silently_drop_legacy_listing(self):
        errors = self.mutate_portable(lambda m: m["extensions"]["com.openai"].update(
            interface={"shortDescription": "A partial listing"}))
        self.assertIn("must match the compatibility manifest", errors)
        self.assertIn("privacyPolicyURL", errors)

    def test_local_execution_declaration_is_required(self):
        for bad in (False, None, "true", 1):
            with self.subTest(value=bad):
                errors = self.mutate_portable(lambda m: m["extensions"]["com.openai"].update(
                    requires_local_executor=bad))
                self.assertIn("requires_local_executor must be true", errors)

    def test_directory_subtitle_limit_is_enforced(self):
        errors = self.mutate_portable(lambda m: m["extensions"]["com.openai"]["interface"].update(
            shortDescription="x" * 31))
        self.assertIn("shortDescription must be 1-30 characters", errors)

    def test_missing_or_escaping_onboarding_skill_is_rejected(self):
        for bad in ("./skills/missing/SKILL.md", "./skills/../../../outside/SKILL.md", "/SKILL.md", None):
            with self.subTest(value=bad):
                errors = self.mutate_portable(lambda m: m["extensions"]["com.openai"].update(onboardingSkill=bad))
                self.assertIn("onboardingSkill must name an included skill", errors)

    def test_listing_urls_cannot_embed_credentials(self):
        for key in ("websiteURL", "supportURL", "privacyPolicyURL"):
            with self.subTest(field=key):
                errors = self.mutate_portable(lambda m: m["extensions"]["com.openai"]["interface"].update(
                    {key: "https://secret@example.com/policy"}))
                self.assertIn(key + " must be a credential-free HTTPS URL", errors)

    def test_translation_limits_and_unknown_submission_fields_are_rejected(self):
        errors = self.mutate_portable(lambda m: m["extensions"]["com.openai"]["publication"].update(
            translations={"ja-JP": {"subtitle": "x" * 31}, "zh-CN": {"description": "text\ttext"}}))
        self.assertIn("translated subtitle", errors)
        self.assertIn("translated description", errors)
        errors = self.mutate_portable(lambda m: m["extensions"]["com.openai"].update(test_credentials="never ship"))
        self.assertIn("unsupported com.openai settings", errors)

    def test_skills_only_submission_rejects_screenshot_metadata(self):
        errors = self.mutate_portable(lambda m: m["extensions"]["com.openai"]["interface"].update(
            screenshots=["./assets/icon.png"]))
        self.assertIn("skills-only directory submissions cannot include screenshots", errors)

    def test_symlinked_skills_directory_is_rejected_not_dropped(self):
        outside = Path(self.tmp.name) / "outside-skills"
        shutil.move(str(self.base / "skills"), str(outside))
        os.symlink(str(outside), str(self.base / "skills"))
        self.assertTrue(any("symlink" in e and "skills" in e for e in B.validate(P, self.root)))
        self.assertTrue(any("symlink" in e for e in B.drift(P, self.root)))
        with self.assertRaises(B.BuildError):
            B.archives(P, self.root)

    def test_symlinked_file_and_plugin_root_are_rejected(self):
        os.symlink(str(self.base / "README.md"), str(self.base / "assets" / "linked.md"))
        self.assertTrue(any("assets/linked.md" in e for e in B.validate(P, self.root)))
        os.remove(str(self.base / "assets" / "linked.md"))
        real = Path(self.tmp.name) / "real-plugin"
        shutil.move(str(self.base), str(real))
        os.symlink(str(real), str(self.base))
        self.assertTrue(any(P["pluginDir"] in e for e in B.validate(P, self.root)))
        with self.assertRaises(B.BuildError):
            B.plugin_files(P, self.root)

    def test_runtime_state_and_private_data_are_rejected(self):
        # Synthetic values only (made-up tailnet, address, user and key) that the scanner must reject.
        (self.base / "scripts" / "config.json").write_text("{}")
        (self.base / "skills" / "yobiru-send" / "notes.md").write_text(
            "ping laptop.tail1a2b3c.ts.net at 100.101.102.103 for /home/alice/.tailmsg, token tskey-abcdef123456\n")
        errors = "\n".join(B.validate(P, self.root))
        for needle in ("config.json", "tailnet MagicDNS", "Tailscale IPv4", "home directory", "auth key"):
            self.assertIn(needle, errors)

    def test_private_denylist_from_environment(self):
        deny = Path(self.tmp.name) / "deny.txt"
        deny.write_text("my-secret-hostname\n")
        (self.base / "README.md").write_text((self.base / "README.md").read_text() + "\nmy-secret-hostname\n")
        os.environ["YOBIRU_PRIVATE_DENYLIST"] = str(deny)
        try:
            self.assertTrue(any("private marker" in e for e in B.validate(P, self.root)))
        finally:
            del os.environ["YOBIRU_PRIVATE_DENYLIST"]

    def test_versions_are_strict_semver_and_a_prerelease_links_its_own_privacy_notice(self):
        def errors(version, privacy=None):
            p = copy.deepcopy(P)
            p["version"] = version
            if privacy:
                p["codex"]["privacyPolicyURL"] = privacy
            return "\n".join(B.validate(p, self.root))
        for bad in ("0.6.0-beta.01", "0.6.0-beta..1", "0.6.0-", "0.6", "v0.6.0", "0.6.0-beta_1"):
            self.assertIn("version must be SemVer 2.0", errors(bad), bad)
        self.assertNotIn("SemVer 2.0", errors("0.6.0-beta.1"))
        policy = "https://github.com/chekusu/yobiru/blob/%s/docs/PRIVACY.md"
        for wrong in ("main", "v0.6.0-beta.1"):           # the stable branch, or an earlier prerelease's tag
            self.assertIn("must point into its own tag", errors("0.6.0-beta.2", policy % wrong), wrong)
        self.assertNotIn("must point into its own tag", errors("0.6.0-beta.2", policy % "v0.6.0-beta.2"))
        self.assertNotIn("must point into its own tag", errors("0.6.0", policy % "main"))

    def test_claude_mcp_server_must_be_the_bundled_launcher(self):
        path = self.base / ".claude-plugin" / "plugin.json"
        good = path.read_text()
        for server in ({"command": "npx", "args": ["some-server"]},
                       {"command": "sh", "args": ["${CLAUDE_PLUGIN_ROOT}/scripts/yobiru", "mcp", "${HOME}"]},
                       {"command": "sh", "args": ["${CLAUDE_PLUGIN_ROOT}/scripts/yobiru", "serve"]},
                       {"command": "sh", "args": ["${CLAUDE_PLUGIN_ROOT}/scripts/yobiru", "mcp"], "env": {"X": "1"}}):
            with self.subTest(server=server):
                manifest = json.loads(good)
                manifest["mcpServers"]["yobiru"] = server
                path.write_text(json.dumps(manifest))
                self.assertTrue(any("mcpServers.yobiru must run" in e for e in B.validate(P, self.root)))
        path.write_text(good)
        (self.base / ".mcp.json").write_text("{}")
        self.assertTrue(any("no shared MCP config" in e for e in B.validate(P, self.root)))

    def test_manifest_drift_and_bad_skill_names_are_reported(self):
        m = self.base / ".cursor-plugin" / "plugin.json"
        m.write_text(m.read_text().replace('"license": "MIT"', '"license": "Apache-2.0"'))
        self.assertTrue(any(".cursor-plugin/plugin.json is out of date" in e for e in B.drift(P, self.root)))
        skill = self.base / "skills" / "yobiru-send" / "SKILL.md"
        skill.write_text(skill.read_text().replace("name: yobiru-send", "name: send"))
        self.assertTrue(any("skills/yobiru-send" in e for e in B.validate(P, self.root)))


if __name__ == "__main__":
    unittest.main()
