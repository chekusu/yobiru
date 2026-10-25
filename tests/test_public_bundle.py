"""Public bot contacts as shipped: the bundled guide, what points at it, and the local demo.

The demo runs from the real plugin archive, unpacked far from the checkout under a path with spaces, with a
synthetic HOME, no TAILMSG_HOME and its own TMPDIR. It uses synthetic data and 127.0.0.1 only.
"""
import io
import os
import re
import socket
import subprocess
import unittest
import zipfile

from support.pkg import REPO, Package, load_builder, python_env

B = load_builder()
P = B.load_product()
PLUGIN = REPO / P["pluginDir"]
GUIDE = "docs/PUBLIC_BOTS.md"
DEMO = "examples/public-bot-demo.sh"
SKILLS = ("yobiru-public-bot", "yobiru-bot-contacts")
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
# "Version 0.6.0-beta.1. Source: ..." -> the whole SemVer, dotted prerelease identifiers included, before the full stop
SEMVER = r"\d+\.\d+\.\d+(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
VERSION_LINE = re.compile(r"^Version (" + SEMVER + r")\.(?=\s|$)", re.M)


def prose(text):
    """Markdown outside fenced code blocks."""
    out, fenced = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif not fenced:
            out.append(line)
    return "\n".join(out)


def anchors(text):
    """GitHub heading anchors of a Markdown document."""
    found = set()
    for line in prose(text).splitlines():
        m = re.match(r"^#{1,6}\s+(.+?)\s*$", line)
        if m:
            found.add(re.sub(r"[^\w\- ]", "", m.group(1).lower()).replace(" ", "-"))
    return found


def section(text, heading):
    start = text.index("\n## %s\n" % heading)
    end = text.find("\n## ", start + 1)
    return text[start:end if end > 0 else len(text)]


class BundledGuideTests(unittest.TestCase):
    def test_plugin_readme_states_the_packaged_version(self):
        text = (PLUGIN / "README.md").read_text(encoding="utf-8")
        self.assertEqual([P["version"]], VERSION_LINE.findall(text))
        self.assertRegex(P["version"], B.SEMVER_RE)

    def test_version_statement_keeps_dotted_prereleases_whole(self):
        for version in ("0.5.3", "0.6.0-beta.1", "1.0.0-rc.1.2", "0.6.0-beta.1+build.7"):
            self.assertEqual([version], VERSION_LINE.findall("Intro.\nVersion %s. Source: x.\n" % version))
        self.assertEqual([], VERSION_LINE.findall("Version 0.6.0-beta.1 has no full stop here\n"))

    def test_guide_and_demo_ship_in_the_archive_as_checked_files(self):
        self.assertEqual(GUIDE, P["bundle"]["publicBots"][GUIDE])
        canonical = (REPO / GUIDE).read_bytes()
        self.assertEqual(canonical, (PLUGIN / GUIDE).read_bytes())
        sources = B.json.loads((PLUGIN / "scripts" / "SOURCES.json").read_text(encoding="utf-8"))["files"]
        self.assertEqual({"source": GUIDE, "sha256": B.sha256(canonical)}, sources[GUIDE])
        archive = B.archives(P)["%s-%s-plugin.zip" % (P["id"], P["version"])]
        with zipfile.ZipFile(io.BytesIO(archive)) as z:
            names = set(z.namelist())
            self.assertEqual(canonical, z.read("%s/%s" % (P["id"], GUIDE)))
        self.assertIn("%s/%s" % (P["id"], DEMO), names)

    def test_skills_and_plugin_readme_point_at_files_in_the_plugin(self):
        shipped = set(B.plugin_files(P))
        for name in SKILLS:
            text = (PLUGIN / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
            refs = re.findall(r"<folder of this SKILL\.md>/\.\./\.\./([\w./-]+[\w])", text)
            self.assertIn(GUIDE, refs, name)
            for rel in refs:
                self.assertIn(rel, shipped, "%s refers to %s, which is not in the plugin" % (name, rel))
            self.assertNotIn("of the Yobiru repository", text)
        readme = (PLUGIN / "README.md").read_text(encoding="utf-8")
        for rel in (GUIDE, DEMO):
            self.assertIn("`%s`" % rel, readme)
            self.assertIn(rel, shipped)
        self.assertIn("`%s`" % DEMO, (REPO / GUIDE).read_text(encoding="utf-8"))

    def test_links_in_shipped_documents_resolve_inside_the_plugin(self):
        docs = [rel for rel in B.plugin_files(P) if rel.endswith(".md")]
        self.assertIn(GUIDE, docs)
        for rel in docs:
            path = PLUGIN / rel
            text = path.read_text(encoding="utf-8")
            for target in LINK.findall(prose(text)):
                if target.startswith("https://"):
                    continue
                self.assertFalse(re.match(r"^[a-z][a-z0-9+.-]*:", target), "%s: %s" % (rel, target))
                file_part, _, anchor = target.partition("#")
                dest = (path.parent / file_part).resolve() if file_part else path
                self.assertTrue(dest.is_file() and PLUGIN.resolve() in dest.parents,
                                "%s links to %s, which is not in the plugin" % (rel, target))
                if anchor and dest.suffix == ".md":
                    self.assertIn(anchor, anchors(dest.read_text(encoding="utf-8")), "%s: %s" % (rel, target))

    def test_repository_links_into_the_guide_name_existing_sections(self):
        names = anchors((REPO / GUIDE).read_text(encoding="utf-8"))
        # CHANGELOG.md links the guide of a release by its tag, so the same entry also reads correctly on the default
        # branch, which may not have the guide: such a link must name a release tag, and this release's own tag must
        # name an existing section of this guide.
        pinned = re.compile(r"^%s/blob/v(%s)/%s\Z" % (re.escape(P["repository"]), SEMVER, re.escape(GUIDE)))
        docs = sorted(REPO.glob("*.md")) + sorted((REPO / "docs").glob("*.md"))
        checked = 0
        for path in docs:
            for target in LINK.findall(prose(path.read_text(encoding="utf-8"))):
                file_part, _, anchor = target.partition("#")
                if not file_part.endswith("PUBLIC_BOTS.md"):
                    continue
                if re.match(r"^[a-z][a-z0-9+.-]*:", file_part):
                    tag = pinned.match(file_part)
                    self.assertIsNotNone(tag, "%s: a link to the guide must name a release tag: %s"
                                         % (path.name, target))
                    if tag.group(1) != P["version"]:
                        continue
                else:
                    self.assertEqual(REPO / GUIDE, (path.parent / file_part).resolve(), "%s: %s" % (path.name, target))
                if anchor:
                    self.assertIn(anchor, names, "%s: %s" % (path.name, target))
                checked += 1
        self.assertGreater(checked, 5)

    def test_walkthrough_runs_the_demo_and_defines_every_value(self):
        text = section((REPO / GUIDE).read_text(encoding="utf-8"), "Try it locally")
        self.assertIn("sh examples/public-bot-demo.sh", text)
        self.assertNotIn("run as written", text)
        self.assertNotRegex(text, r"\b20\d\d-\d\d-\d\d\b")              # dates are computed, not fixed
        code = "\n".join(re.findall(r"```sh\n(.*?)```", text, re.S))
        words = prose(text)
        values = set(re.findall(r"\$([A-Z][A-Z_]*)", code)) | set(re.findall(r"\b([A-Z]+_[A-Z_]+)\b", code))
        self.assertTrue({"Y", "T", "URL", "BOT_ID", "ACTOR_ID", "CAL_URL", "DAY", "MESSAGE_ID"} <= values, values)
        for value in values:
            self.assertIn("`%s`" % value, words, "%s is used but not explained" % value)
        last_request = max(code.index("public slots"), code.rindex("public chat"))
        self.assertGreater(code.index("public worker run"), last_request, "the worker pass must follow the requests")

    def test_demo_is_self_contained(self):
        text = (PLUGIN / DEMO).read_text(encoding="utf-8")
        self.assertIn('Y="$HERE/../scripts/yobiru"', text)               # the launcher of its own plugin
        self.assertIn("mktemp -d", text)
        self.assertRegex(text, r"trap '[^']*rm -rf \"\$T\"' EXIT")
        self.assertNotRegex(text, r"(?<![\w$])(?:/home/|/Users/|~/)")
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        self.assertNotRegex(code, r"\b77(?:66|80)\b")                       # no fixed ports
        self.assertEqual([], [line for line in text.splitlines()
                              if re.search(r"\bsh \"\$Y\" .*\bpublic\b", line) and '--home "$T/' not in line])


class DemoFromArchiveTests(unittest.TestCase):
    def test_demo_runs_from_the_unpacked_plugin_archive_and_cleans_up(self):
        pkg = Package()
        self.addCleanup(pkg.cleanup)
        user_home = pkg.new_home("demo user home")
        tmp = pkg.new_home("demo tmp")
        env = python_env({"HOME": str(user_home), "PATH": str(pkg.bin) + os.pathsep + "/usr/bin:/bin", "LANG": "C.UTF-8",
                          "LC_ALL": "C.UTF-8", "TMPDIR": str(tmp), "PYTHONDONTWRITEBYTECODE": "1"})
        r = subprocess.run(["/bin/sh", str(pkg.plugin / DEMO)], env=env, cwd=str(pkg.work),
                           capture_output=True, text=True, timeout=240)
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)
        out = r.stdout
        for expected in ("asked a visitor question: queued", 'one worker pass: ["completed", "completed"]\n',
                         "the message: queued, in the owner's message requests",
                         "T11:00:00Z", "T12:00:00Z", "T13:00:00Z", '["Please tell the owner hello"]',
                         "private event details are only in: ./calendar/events.json\n",
                         "the caller's next history read: not_found 404",
                         "Demo finished: every step behaved as described."):
            self.assertIn(expected, out)
        self.assertNotIn("SYNTHETIC-PRIVATE", out + r.stderr)
        self.assertEqual([], os.listdir(str(tmp)))                          # the temporary directory is gone
        self.assertEqual([], os.listdir(str(user_home)))                    # no data home was created
        urls = re.findall(r"http://127\.0\.0\.1:(\d+)", out)
        self.assertGreaterEqual(len(set(urls)), 2)
        for port in set(urls):                                              # both services were stopped
            with self.assertRaises(OSError):
                socket.create_connection(("127.0.0.1", int(port)), timeout=2).close()


if __name__ == "__main__":
    unittest.main()
