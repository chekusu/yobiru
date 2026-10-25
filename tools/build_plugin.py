#!/usr/bin/env python3
"""Build, check and release the Yobiru agent plugin (Python 3.8+, stdlib only).

  python3 tools/build_plugin.py sync      render manifests + copy bundled sources into the plugin dir
  python3 tools/build_plugin.py check     verify everything is in sync and valid (no writes; CI/tests)
  python3 tools/build_plugin.py release [--out dist]
                                          check, then write deterministic zip archives + SHA256SUMS
  python3 tools/build_plugin.py files     list the files that go into the plugin archive

packaging/product.json is the single source of the product identifier, version and host metadata.
The root tailmsg.py and Codex bridge files stay canonical; the plugin carries byte-identical copies
(recorded in scripts/SOURCES.json). Host manifests are generated, never edited by hand.
"""
import argparse
import hashlib
import io
import json
import os
import re
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
PRODUCT_FILE = ROOT / "packaging" / "product.json"
AGENT_PLUGINS_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
ZIP_TIME = (1980, 1, 1, 0, 0, 0)            # fixed timestamp: archives depend only on file contents
JUNK_DIRS = {"__pycache__"}
JUNK_FILES = {".DS_Store"}
JUNK_SUFFIXES = (".pyc", ".pyo")
FORBIDDEN_NAMES = {"config.json", "codex-bridge.json", "installation.json", ".env", "inbox", "outbox",
                   "test-run", ".live-test", ".tailmsg", "backups"}
FORBIDDEN_SUFFIXES = (".sqlite3", ".sqlite", ".db", ".pem", ".key", ".p12", ".log", ".bak")
TEXT_SUFFIXES = (".md", ".json", ".py", ".txt", ".svg", ".yaml", ".yml", ".sh", "")
# Generic markers of private data; maintainers can add personal markers (one regex per line) in a file
# named by $YOBIRU_PRIVATE_DENYLIST that is never committed.
DENY_PATTERNS = [
    (r"\btail[0-9a-f]{4,}\.ts\.net\b", "a real tailnet MagicDNS name"),
    (r"(?<![\w.~])/home/(?!you/|me/|user/)[a-z_][a-z0-9_-]*/", "an absolute home directory path"),
    (r"/Users/(?!you/|YOU/|me/|user/)[^/\s\"'<>]+/", "an absolute macOS home directory path"),
    (r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b", "a concrete Tailscale IPv4 address"),
    (r"fd7a:115c:a1e0::[0-9a-f]{1,4}:[0-9a-f]{1,4}", "a concrete Tailscale IPv6 address"),
    (r"\btskey-[A-Za-z0-9-]{8,}", "a Tailscale auth key"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "a private key"),
    (r"\b(?:sk|rk)-[A-Za-z0-9_-]{20,}", "an API key"),
    (r"\bgh[pousr]_[A-Za-z0-9]{20,}", "a GitHub token"),
    (r"\bxox[abprs]-[A-Za-z0-9-]{10,}", "a Slack token"),
    (r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", "a UUID (session or thread id?)"),
    (r"[A-Za-z0-9._%+-]+@(?!example\.(?:com|org|net|invalid)\b)[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}",
     "an email address"),
]
DENY_ALLOWED = {"100.64.0.0"}               # the tailnet CIDR in tailmsg.py, not a host
PLUGIN_NAME_RE = re.compile(r"^(?!.*(?:--|\.\.))[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\Z")   # Agent Plugins + Cursor
CODEX_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*\Z")
MARKETPLACE_RE = re.compile(r"^[A-Za-z0-9_-]+\Z")
SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*\Z")
# SemVer 2.0, the grammar the launcher uses to compare an installation with releases: a prerelease such as
# 0.6.0-beta.1 is a list of dot-separated identifiers, none empty, numeric ones without leading zeros.
PRE_ID = r"(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-" + PRE_ID + r"(?:\." + PRE_ID + r")*)?"
                       r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?\Z")
CODEX_KEYS = {"id", "name", "version", "description", "skills", "apps", "mcpServers", "interface", "author",
              "homepage", "repository", "license", "keywords"}
CODEX_INTERFACE_KEYS = {"displayName", "shortDescription", "longDescription", "developerName", "category",
                        "capabilities", "websiteURL", "privacyPolicyURL", "termsOfServiceURL", "brandColor",
                        "composerIcon", "logo", "logoDark", "screenshots", "defaultPrompt", "default_prompt"}
AGENT_PLUGIN_KEYS = {"$schema", "name", "version", "description", "author", "homepage", "repository", "license",
                     "keywords", "extensions"}
OPENAI_KEYS = {"interface", "requires_local_executor", "onboardingSkill", "publication"}
OPENAI_INTERFACE_KEYS = CODEX_INTERFACE_KEYS | {"supportURL"}
CURSOR_KEYS = {"name", "description", "version", "author", "homepage", "repository", "license", "keywords", "logo",
               "rules", "agents", "skills", "commands", "hooks", "mcpServers", "variables"}
CLAUDE_KEYS = {"name", "version", "description", "author", "homepage", "repository", "license", "keywords",
               "mcpServers"}


class BuildError(Exception):
    pass


def jbytes(obj):
    return (json.dumps(obj, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def executables(p):
    """The launcher and its Python entry point, named after the product id."""
    return {"scripts/" + p["id"], "scripts/" + p["id"] + ".py"}


def load_product():
    p = json.loads(PRODUCT_FILE.read_text(encoding="utf-8"))
    if not PLUGIN_NAME_RE.match(p["id"]) or p["pluginDir"] != "plugins/" + p["id"]:
        raise SystemExit("product.json: id must be a plugin name and pluginDir must be plugins/<id>")
    return p


# ---------------------------------------------------------------- expected tree
def render(p):
    """{repo-relative path: bytes} for every generated file."""
    pid, c = p["id"], p["codex"]
    author = {"name": p["publisher"]}
    portable = {"$schema": AGENT_PLUGINS_SCHEMA, "name": pid, "version": p["version"],
                "description": p["description"], "author": author, "repository": p["repository"],
                "license": p["license"], "keywords": p["keywords"]}
    codex = {"name": pid, "version": p["version"], "description": p["description"], "author": author,
             "repository": p["repository"], "license": p["license"], "keywords": p["keywords"],
             "skills": "./skills/",
             "interface": {"displayName": p["displayName"], "shortDescription": c["shortDescription"],
                           "longDescription": c["longDescription"], "developerName": p["publisher"],
                           "category": c["category"], "capabilities": c["capabilities"],
                           "defaultPrompt": c["defaultPrompt"], "brandColor": c["brandColor"],
                           "composerIcon": c["composerIcon"], "logo": c["logo"]}}
    for key in ("websiteURL", "privacyPolicyURL"):
        codex["interface"][key] = c[key]
    # The portable OpenAI extension replaces (rather than merges with) the legacy
    # overlay. Keep the complete interface in both; only the extension uses new
    # directory fields so old Codex manifest validators remain compatible.
    oa = p["openai"]
    portable["extensions"] = {"com.openai": {
        "interface": dict(codex["interface"], supportURL=oa["supportURL"]),
        "requires_local_executor": oa["requires_local_executor"],
        "onboardingSkill": oa["onboardingSkill"],
        "publication": oa["publication"],
    }}
    cursor = {"name": pid, "version": p["version"], "description": p["description"], "author": author,
              "repository": p["repository"], "license": p["license"], "keywords": p["keywords"],
              "logo": p["cursor"]["logo"]}
    # Claude Code: default discovery finds skills/; the MCP server is declared here only, because Claude Code
    # expands ${CLAUDE_PLUGIN_ROOT} and no other host reads this file (a shared mcp.json would reach Cursor).
    cl = p["claude"]["mcpServer"]
    claude = {"name": pid, "version": p["version"], "description": p["description"], "author": author,
              "repository": p["repository"], "license": p["license"], "keywords": p["keywords"],
              "mcpServers": {cl["name"]: {"command": cl["command"], "args": cl["args"]}}}
    for manifest in (portable, codex, cursor, claude):
        manifest["homepage"] = p["homepage"]
    m = p["marketplace"]
    codex_market = {"name": m["name"], "interface": {"displayName": m["displayName"]},
                    "plugins": [{"name": pid, "source": {"source": "local", "path": "./" + p["pluginDir"]},
                                 "policy": {"installation": c["installation"],
                                            "authentication": c["authentication"]},
                                 "category": c["marketplaceCategory"]}]}
    cursor_market = {"name": m["name"], "owner": {"name": p["publisher"]},
                     "metadata": {"description": m["description"], "version": p["version"]},
                     "plugins": [{"name": pid, "source": p["pluginDir"], "description": p["description"]}]}
    claude_market = {"name": m["name"], "owner": {"name": p["publisher"]},
                     "metadata": {"description": m["description"], "version": p["version"]},
                     "plugins": [{"name": pid, "source": "./" + p["pluginDir"], "description": p["description"],
                                  "version": p["version"], "category": p["claude"]["marketplaceCategory"],
                                  "homepage": p["homepage"]}]}
    d = p["pluginDir"]
    out = {d + "/plugin.json": jbytes(portable), d + "/.codex-plugin/plugin.json": jbytes(codex),
           d + "/.cursor-plugin/plugin.json": jbytes(cursor), d + "/.claude-plugin/plugin.json": jbytes(claude),
           ".agents/plugins/marketplace.json": jbytes(codex_market),
           ".cursor-plugin/marketplace.json": jbytes(cursor_market),
           ".claude-plugin/marketplace.json": jbytes(claude_market)}
    sources = {}
    for group in ("core", "codexBridge", "claudeBridge", "publicBots", "license"):
        for src, dst in sorted(p["bundle"][group].items()):
            data = (ROOT / src).read_bytes()
            out[d + "/" + dst] = data
            sources[dst] = {"source": src, "sha256": sha256(data)}
    out[d + "/scripts/SOURCES.json"] = jbytes({
        "about": "Byte-identical copies of canonical files at the repository root, refreshed by "
                 "tools/build_plugin.py sync. Edit the root files, not these copies.",
        "protocol": "tailmsg v1", "files": sources})
    return out


def symlinks(p, root=ROOT):
    """Every symlink that could change what ships: the plugin root, catalogs, and any file OR directory
    inside the plugin (os.walk does not descend into symlinked directories, so they are listed here)."""
    base = root / p["pluginDir"]
    found = [rel for rel in (p["pluginDir"], "plugins", ".agents", ".agents/plugins",
                             ".agents/plugins/marketplace.json", ".cursor-plugin",
                             ".cursor-plugin/marketplace.json", ".claude-plugin",
                             ".claude-plugin/marketplace.json") if (root / rel).is_symlink()]
    if base.is_dir() and not base.is_symlink():
        for dirpath, dirnames, filenames in os.walk(str(base)):
            for name in dirnames + filenames:
                if Path(dirpath, name).is_symlink():
                    found.append(Path(dirpath, name).relative_to(root).as_posix())
            dirnames[:] = [d for d in dirnames if not Path(dirpath, d).is_symlink()]
    return sorted(found)


def plugin_files(p, root=ROOT):
    """Sorted plugin-relative POSIX paths that belong in the archive (junk such as __pycache__ skipped).
    Refuses symlinks anywhere in the plugin: an archive must hold exactly the files that were checked."""
    links = symlinks(p, root)
    if links:
        raise BuildError("symlinks are not allowed in the plugin or catalogs: %s" % ", ".join(links))
    base = root / p["pluginDir"]
    out = []
    for dirpath, dirnames, filenames in os.walk(str(base)):
        dirnames[:] = sorted(x for x in dirnames if x not in JUNK_DIRS)
        for name in sorted(filenames):
            if name in JUNK_FILES or name.endswith(JUNK_SUFFIXES):
                continue
            out.append(Path(dirpath, name).relative_to(base).as_posix())
    return sorted(out)


# ---------------------------------------------------------------- validation
def parse_frontmatter(text):
    """Minimal YAML frontmatter reader for `key: value` / `key: "value"` lines (enough for SKILL.md)."""
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---", 4)
    if end < 0:
        return None
    try:
        import yaml  # optional, stricter when available
        data = yaml.safe_load(text[4:end])
        return data if isinstance(data, dict) else None
    except ImportError:
        pass
    data = {}
    for line in text[4:end].splitlines():
        m = re.match(r"^([A-Za-z_-]+):\s*(.*)$", line)
        if m:
            v = m.group(2).strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            data[m.group(1)] = v
    return data


def rel_inside(base, rel):
    """True if a manifest path stays inside base (relative, no '..', resolves inside)."""
    if not isinstance(rel, str) or not rel or rel.startswith("/") or "\\" in rel:
        return False
    parts = PurePosixPath(rel).parts
    if any(x == ".." for x in parts):
        return False
    target = (base / rel).resolve()
    return target == base.resolve() or str(target).startswith(str(base.resolve()) + os.sep)


def public_https_url(value):
    """A credential-free HTTPS listing URL; availability is checked before submission."""
    if (not isinstance(value, str) or not value or len(value) > 1024 or
            any(c.isspace() or ord(c) < 32 or 127 <= ord(c) <= 159 for c in value)):
        return False
    try:
        parsed = urlsplit(value)
        return (parsed.scheme == "https" and bool(parsed.hostname) and
                parsed.username is None and parsed.password is None)
    except ValueError:
        return False


def validate_openai(manifest, legacy_ui, base):
    """Validate this skills-only submission's documented OpenAI fields.

    https://developers.openai.com/plugins/deploy/submission
    https://developers.openai.com/plugins/deploy/desktop-only
    This is intentionally a check of the shape Yobiru ships, not a general
    replacement for the portal's eligibility, policy and skill scans.
    """
    errs = []
    extensions = manifest.get("extensions")
    if not isinstance(extensions, dict) or set(extensions) != {"com.openai"}:
        return ["plugin.json: extensions must contain the complete com.openai settings"]
    oa = extensions["com.openai"]
    if not isinstance(oa, dict) or set(oa) - OPENAI_KEYS:
        return ["plugin.json: unsupported com.openai settings"]
    if oa.get("requires_local_executor") is not True:
        errs.append("plugin.json: com.openai.requires_local_executor must be true for local Tailscale commands")
    ui = oa.get("interface")
    if not isinstance(ui, dict):
        errs.append("plugin.json: com.openai.interface must contain the full listing; the overlay is not merged")
        ui = {}
    if set(ui) - OPENAI_INTERFACE_KEYS:
        errs.append("plugin.json: unsupported com.openai.interface fields")
    for key, value in legacy_ui.items():
        if key not in ui or ui[key] != value:
            errs.append("plugin.json: com.openai.interface.%s must match the compatibility manifest" % key)
    for key, limit in (("displayName", 30), ("shortDescription", 30),
                       ("longDescription", 4000), ("developerName", 80)):
        value = ui.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            errs.append("plugin.json: com.openai.interface.%s must be 1-%d characters" % (key, limit))
    for key in ("websiteURL", "supportURL", "privacyPolicyURL"):
        if not public_https_url(ui.get(key)):
            errs.append("plugin.json: com.openai.interface.%s must be a credential-free HTTPS URL" % key)
    if "screenshots" in ui:
        errs.append("plugin.json: skills-only directory submissions cannot include screenshots")
    onboarding = oa.get("onboardingSkill")
    if (not isinstance(onboarding, str) or not onboarding.startswith("./skills/") or
            not onboarding.endswith("/SKILL.md") or not rel_inside(base, onboarding) or
            not (base / onboarding).is_file()):
        errs.append("plugin.json: com.openai.onboardingSkill must name an included skill")
    publication = oa.get("publication")
    if not isinstance(publication, dict) or set(publication) - {"translations"}:
        errs.append("plugin.json: unsupported com.openai.publication settings")
        return errs
    translations = publication.get("translations")
    if not isinstance(translations, dict) or not translations:
        errs.append("plugin.json: publication.translations must be a nonempty locale map")
        return errs
    for locale, fields in translations.items():
        if not isinstance(locale, str) or not re.match(r"^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*\Z", locale):
            errs.append("plugin.json: invalid publication translation locale")
        if not isinstance(fields, dict) or not fields or set(fields) - {"subtitle", "description"}:
            errs.append("plugin.json: translation fields must be subtitle and/or description")
            continue
        for key, value in fields.items():
            limit = 30 if key == "subtitle" else 4000
            if (not isinstance(value, str) or not value.strip() or len(value) > limit or
                    any((ord(c) < 32 or 127 <= ord(c) <= 159) and
                        not (key == "description" and c == "\n") for c in value)):
                errs.append("plugin.json: translated %s must contain supported text within %d characters" % (key, limit))
    return errs


def validate(p, root=ROOT):
    errs = []
    base = root / p["pluginDir"]
    pid = p["id"]
    links = symlinks(p, root)
    if links:
        return ["symlink not allowed (would be dropped from or redirect the archive): %s" % x for x in links]

    def load(rel):
        try:
            return json.loads((root / rel).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            errs.append("%s: %s" % (rel, e))
            return {}

    # Agent Plugins portable manifest (mirrors the closed 1.0.0 schema)
    m = load(p["pluginDir"] + "/plugin.json")
    if set(m) - AGENT_PLUGIN_KEYS:
        errs.append("plugin.json: fields not in the Agent Plugins schema: %s" % sorted(set(m) - AGENT_PLUGIN_KEYS))
    if m.get("$schema") != AGENT_PLUGINS_SCHEMA:
        errs.append("plugin.json: $schema must be %s" % AGENT_PLUGINS_SCHEMA)
    if not isinstance(m.get("name"), str) or len(m["name"]) > 64 or not PLUGIN_NAME_RE.match(m["name"]):
        errs.append("plugin.json: invalid name")
    for k in ("version", "description", "homepage", "repository", "license"):
        if k in m and not isinstance(m[k], str):
            errs.append("plugin.json: %s must be a string" % k)
    if "author" in m and (not isinstance(m["author"], dict) or set(m["author"]) - {"name", "email", "url"}
                          or not all(isinstance(v, str) for v in m["author"].values())):
        errs.append("plugin.json: author must be {name, email, url} strings")
    if "keywords" in m and not (isinstance(m["keywords"], list) and all(isinstance(k, str) for k in m["keywords"])):
        errs.append("plugin.json: keywords must be strings")
    # Codex compatibility manifest (rules of the plugin-creator validator)
    c = load(p["pluginDir"] + "/.codex-plugin/plugin.json")
    if set(c) - CODEX_KEYS:
        errs.append(".codex-plugin: unsupported fields %s" % sorted(set(c) - CODEX_KEYS))
    if not CODEX_NAME_RE.match(str(c.get("name"))) or c.get("name") != pid:
        errs.append(".codex-plugin: name must be %s" % pid)
    if not SEMVER_RE.match(str(c.get("version"))):
        errs.append(".codex-plugin: version must be strict semver")
    if not SEMVER_RE.match(str(p.get("version"))):
        errs.append("packaging/product.json: version must be SemVer 2.0 (for example 0.6.0 or 0.6.0-beta.1)")
    elif "-" in p["version"].split("+")[0]:
        # A prerelease ships from its own tag while the default branch keeps the stable release, so its listing
        # must link the privacy notice of what it ships, not of the stable branch.
        pinned = "%s/blob/v%s/" % (p["repository"], p["version"])
        if not str(p["codex"].get("privacyPolicyURL")).startswith(pinned):
            errs.append("packaging/product.json: a prerelease's codex.privacyPolicyURL must point into its own tag "
                        "(%s...)" % pinned)
    if c.get("skills") != "./skills/":
        errs.append(".codex-plugin: skills must be ./skills/")
    if not isinstance(c.get("author"), dict) or not c["author"].get("name"):
        errs.append(".codex-plugin: author.name required")
    ui = c.get("interface") or {}
    if set(ui) - CODEX_INTERFACE_KEYS:
        errs.append(".codex-plugin: unsupported interface fields %s" % sorted(set(ui) - CODEX_INTERFACE_KEYS))
    for k in ("displayName", "shortDescription", "longDescription", "developerName", "category"):
        if not isinstance(ui.get(k), str) or not ui[k].strip():
            errs.append(".codex-plugin: interface.%s required" % k)
    if not isinstance(ui.get("capabilities"), list):
        errs.append(".codex-plugin: interface.capabilities must be a list")
    dp = ui.get("defaultPrompt")
    if not isinstance(dp, list) or not 1 <= len(dp) <= 3 or any(len(x) > 128 for x in dp):
        errs.append(".codex-plugin: interface.defaultPrompt must be 1-3 strings of <=128 chars")
    if not re.match(r"^#[0-9A-Fa-f]{6}\Z", str(ui.get("brandColor"))):
        errs.append(".codex-plugin: interface.brandColor must be #RRGGBB")
    for k in ("composerIcon", "logo"):
        rel = str(ui.get(k, ""))
        if not rel.startswith("./") or not rel_inside(base, rel[2:]) or not (base / rel[2:]).is_file():
            errs.append(".codex-plugin: interface.%s must name a file inside the plugin" % k)
    errs.extend(validate_openai(m, ui, base))
    # Cursor native manifest
    cu = load(p["pluginDir"] + "/.cursor-plugin/plugin.json")
    if set(cu) - CURSOR_KEYS:
        errs.append(".cursor-plugin: unsupported fields %s" % sorted(set(cu) - CURSOR_KEYS))
    if cu.get("name") != pid or not PLUGIN_NAME_RE.match(str(cu.get("name"))):
        errs.append(".cursor-plugin: name must be %s" % pid)
    if not isinstance(cu.get("author"), dict) or set(cu["author"]) - {"name", "email"} or not cu["author"].get("name"):
        errs.append(".cursor-plugin: author must be {name[, email]}")
    if not rel_inside(base, cu.get("logo")) or not (base / str(cu.get("logo"))).is_file():
        errs.append(".cursor-plugin: logo must be a relative path to a file inside the plugin")
    for k in ("skills", "rules", "agents", "commands", "hooks", "mcpServers"):
        if k in cu:
            errs.append(".cursor-plugin: %s would replace default discovery; keep it out" % k)
    if (base / "mcp.json").exists() or (base / ".mcp.json").exists() or (base / "hooks").exists():
        errs.append("plugin: no shared MCP config or hooks are shipped; Claude Code's server lives in .claude-plugin")
    # Claude Code native manifest: skills by default discovery, one stdio MCP server running the bundled launcher
    cc = load(p["pluginDir"] + "/.claude-plugin/plugin.json")
    if set(cc) - CLAUDE_KEYS:
        errs.append(".claude-plugin: unsupported fields %s" % sorted(set(cc) - CLAUDE_KEYS))
    if cc.get("name") != pid:
        errs.append(".claude-plugin: name must be %s" % pid)
    if not isinstance(cc.get("author"), dict) or set(cc["author"]) - {"name", "email", "url"} or \
            not cc["author"].get("name"):
        errs.append(".claude-plugin: author must be {name[, email, url]}")
    servers = cc.get("mcpServers")
    if not isinstance(servers, dict) or len(servers) != 1:
        errs.append(".claude-plugin: exactly one MCP server expected")
    else:
        (sname, server), = servers.items()
        args = server.get("args") if isinstance(server, dict) else None
        launcher = "${CLAUDE_PLUGIN_ROOT}/scripts/" + pid
        if (set(server) != {"command", "args"} or server.get("command") != "sh" or not isinstance(args, list) or
                args[:1] != [launcher] or args[1:2] != ["mcp"] or
                any(not isinstance(x, str) or "$" in x for x in args[1:])):
            errs.append(".claude-plugin: mcpServers.%s must run `sh %s mcp ...`" % (sname, launcher))
        if not (base / "scripts" / pid).is_file() or not (base / "scripts" / (pid + "_mcp.py")).is_file():
            errs.append(".claude-plugin: the MCP server's launcher or scripts/%s_mcp.py is missing" % pid)
    for man, label in ((m, "plugin.json"), (cu, ".cursor-plugin"), (cc, ".claude-plugin")):
        for k in ("version", "description", "homepage", "repository", "license", "keywords"):
            if man.get(k) != c.get(k):
                errs.append("%s: %s differs from .codex-plugin" % (label, k))
    if not public_https_url(m.get("homepage")):
        errs.append("plugin.json: homepage must be a credential-free HTTPS URL")
    # catalogs
    cm = load(".agents/plugins/marketplace.json")
    if not MARKETPLACE_RE.match(str(cm.get("name"))):
        errs.append(".agents marketplace: invalid name")
    entries = [e for e in cm.get("plugins", []) if isinstance(e, dict) and e.get("name") == pid]
    if len(entries) != 1:
        errs.append(".agents marketplace: exactly one %s entry required" % pid)
    else:
        e = entries[0]
        if e.get("source") != {"source": "local", "path": "./" + p["pluginDir"]}:
            errs.append(".agents marketplace: source must be local ./%s" % p["pluginDir"])
        pol = e.get("policy") or {}
        if pol.get("installation") not in ("NOT_AVAILABLE", "AVAILABLE", "INSTALLED_BY_DEFAULT") or \
                pol.get("authentication") not in ("ON_INSTALL", "ON_USE") or not e.get("category"):
            errs.append(".agents marketplace: policy.installation, policy.authentication and category required")
    km = load(".cursor-plugin/marketplace.json")
    if not PLUGIN_NAME_RE.match(str(km.get("name"))) or not (km.get("owner") or {}).get("name"):
        errs.append(".cursor-plugin marketplace: name (kebab-case) and owner.name required")
    kentries = [e for e in km.get("plugins", []) if isinstance(e, dict) and e.get("name") == pid]
    if len(kentries) != 1 or not rel_inside(root, kentries[0].get("source")) or \
            not (root / str(kentries[0].get("source")) / ".cursor-plugin" / "plugin.json").is_file():
        errs.append(".cursor-plugin marketplace: one %s entry whose source holds .cursor-plugin/plugin.json" % pid)
    if len(jbytes(km)) > 10 * 1024 * 1024:
        errs.append(".cursor-plugin marketplace: larger than 10 MB")
    clm = load(".claude-plugin/marketplace.json")
    if not PLUGIN_NAME_RE.match(str(clm.get("name"))) or not (clm.get("owner") or {}).get("name"):
        errs.append(".claude-plugin marketplace: name (kebab-case) and owner.name required")
    centries = [e for e in clm.get("plugins", []) if isinstance(e, dict) and e.get("name") == pid]
    if len(centries) != 1 or centries[0].get("source") != "./" + p["pluginDir"] or \
            not (root / p["pluginDir"] / ".claude-plugin" / "plugin.json").is_file() or \
            centries[0].get("version") != c.get("version"):
        errs.append(".claude-plugin marketplace: one %s entry, source ./%s holding .claude-plugin/plugin.json, "
                    "same version" % (pid, p["pluginDir"]))
    # skills
    skills = sorted(x for x in (base / "skills").iterdir() if x.is_dir()) if (base / "skills").is_dir() else []
    if not skills:
        errs.append("skills: none found")
    shipped = set(plugin_files(p, root))
    for sd in skills:
        if "skills/%s/SKILL.md" % sd.name not in shipped:
            errs.append("skills/%s: SKILL.md would not be in the archive" % sd.name)
        f = sd / "SKILL.md"
        if not f.is_file():
            errs.append("skills/%s: missing SKILL.md" % sd.name)
            continue
        fm = parse_frontmatter(f.read_text(encoding="utf-8"))
        if fm is None:
            errs.append("skills/%s: SKILL.md needs YAML frontmatter" % sd.name)
            continue
        name, desc = fm.get("name"), fm.get("description")
        if name != sd.name or not SKILL_NAME_RE.match(str(name)) or len(str(name)) > 64:
            errs.append("skills/%s: name must equal the folder name (lowercase-hyphen, <=64)" % sd.name)
        if not str(name).startswith(p["skillPrefix"]):
            errs.append("skills/%s: name must start with %s" % (sd.name, p["skillPrefix"]))
        if not isinstance(desc, str) or not desc.strip() or len(desc) > 1024:
            errs.append("skills/%s: description must be 1-1024 characters" % sd.name)
        if fm.get("disable-model-invocation") not in (None, False):
            errs.append("skills/%s: must stay model-invocable" % sd.name)
    # the runtime reports its product version over /info; it must be this release
    core_src = (root / "tailmsg.py").read_text(encoding="utf-8") if (root / "tailmsg.py").is_file() else \
        (base / "scripts" / "tailmsg.py").read_text(encoding="utf-8")
    vm = re.search(r'^PRODUCT_VERSION = "([^"]+)"', core_src, re.M)
    if not vm or vm.group(1) != p["version"]:
        errs.append("tailmsg.py: PRODUCT_VERSION must equal packaging/product.json version %s" % p["version"])
    # hygiene
    deny = [(re.compile(rx), why) for rx, why in DENY_PATTERNS]
    extra = os.environ.get("YOBIRU_PRIVATE_DENYLIST")
    if extra:
        for line in Path(extra).read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.startswith("#"):
                deny.append((re.compile(line.strip(), re.I), "a private marker from $YOBIRU_PRIVATE_DENYLIST"))
    for rel in plugin_files(p, root):
        path = base / rel
        parts = rel.split("/")
        if set(parts) & FORBIDDEN_NAMES or rel.endswith(FORBIDDEN_SUFFIXES):
            errs.append("plugin/%s: looks like runtime state or a secret; never ship it" % rel)
    scan = [(p["pluginDir"] + "/" + rel, base / rel) for rel in plugin_files(p, root)]
    scan += [(x.relative_to(root).as_posix(), x) for x in sorted(root.glob("*.md")) + sorted(root.glob("docs/*.md"))]
    scan += [("packaging/product.json", PRODUCT_FILE)]
    for label, path in scan:
        if not path.name.endswith(TEXT_SUFFIXES) or path.suffix == ".png":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for rx, why in deny:
            for hit in rx.finditer(text):
                if hit.group(0) not in DENY_ALLOWED:
                    errs.append("%s: contains %s (%r)" % (label, why, hit.group(0)[:40]))
                    break
    return errs


def drift(p, root=ROOT):
    """Generated/copied files that differ from what sync would write, plus wrong executable bits."""
    out = ["symlink not allowed: %s" % x for x in symlinks(p, root)]
    if out:
        return out
    for rel, data in sorted(render(p).items()):
        f = root / rel
        if not f.is_file() or f.read_bytes() != data:
            out.append("%s is out of date (run: python3 tools/build_plugin.py sync)" % rel)
    if os.name == "posix":
        base = root / p["pluginDir"]
        for rel in plugin_files(p, root):
            want = rel in executables(p)
            have = bool((base / rel).stat().st_mode & stat.S_IXUSR)
            if want != have:
                out.append("%s/%s: executable bit should be %s" % (p["pluginDir"], rel, "set" if want else "clear"))
    return out


# ---------------------------------------------------------------- archives
def zip_bytes(entries):
    """entries: sorted [(arcname, bytes, executable)] -> deterministic stored zip."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for name, data, exe in entries:
            info = zipfile.ZipInfo(name, date_time=ZIP_TIME)
            info.create_system = 3
            info.external_attr = ((0o100755 if exe else 0o100644) & 0xFFFF) << 16
            info.compress_type = zipfile.ZIP_STORED
            z.writestr(info, data)
    return buf.getvalue()


def archives(p, root=ROOT):
    base = root / p["pluginDir"]
    pid, ver = p["id"], p["version"]
    files = [(rel, (base / rel).read_bytes(), rel in executables(p)) for rel in plugin_files(p, root)]
    plugin_zip = zip_bytes([("%s/%s" % (pid, rel), data, exe) for rel, data, exe in files])
    mroot = "%s-marketplace" % pid
    market = [("%s/%s/%s" % (mroot, p["pluginDir"], rel), data, exe) for rel, data, exe in files]
    for rel in (".agents/plugins/marketplace.json", ".cursor-plugin/marketplace.json", ".claude-plugin/marketplace.json",
                "LICENSE"):
        market.append(("%s/%s" % (mroot, rel), (root / rel).read_bytes(), False))
    market_zip = zip_bytes(sorted(market))
    return {"%s-%s-plugin.zip" % (pid, ver): plugin_zip, "%s-%s-marketplace.zip" % (pid, ver): market_zip}


# ---------------------------------------------------------------- commands
def cmd_sync(p):
    changed = 0
    for rel, data in sorted(render(p).items()):
        f = ROOT / rel
        if not f.is_file() or f.read_bytes() != data:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(data)
            print("wrote %s" % rel)
            changed += 1
    if os.name == "posix":
        base = ROOT / p["pluginDir"]
        for rel in plugin_files(p):
            mode = 0o755 if rel in executables(p) else 0o644
            if stat.S_IMODE((base / rel).stat().st_mode) != mode:
                os.chmod(str(base / rel), mode)
                print("chmod %o %s/%s" % (mode, p["pluginDir"], rel))
    print("sync: %d file(s) updated" % changed)
    return 0


def cmd_check(p, quiet=False):
    problems = drift(p) + validate(p)
    for x in problems:
        print("FAIL  %s" % x)
    if not quiet:
        print("check: %s" % ("ok" if not problems else "%d problem(s)" % len(problems)))
    return 1 if problems else 0


def cmd_release(p, out):
    if cmd_check(p, quiet=True):
        print("release: refusing to build from an unchecked tree")
        return 1
    out = Path(out) if Path(out).is_absolute() else ROOT / out
    out.mkdir(parents=True, exist_ok=True)
    sums = []
    for name, data in sorted(archives(p).items()):
        (out / name).write_bytes(data)
        sums.append("%s  %s\n" % (sha256(data), name))
        print("%s  %s  (%d bytes)" % (sha256(data), out / name, len(data)))
    (out / "SHA256SUMS").write_text("".join(sums), encoding="utf-8")
    print("wrote %s" % (out / "SHA256SUMS"))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd")
    sp.required = True
    sp.add_parser("sync")
    sp.add_parser("check")
    sp.add_parser("files")
    r = sp.add_parser("release")
    r.add_argument("--out", default="dist")
    a = ap.parse_args(argv)
    p = load_product()
    try:
        return run(p, a)
    except BuildError as e:
        print("FAIL  %s" % e)
        return 1


def run(p, a):
    if a.cmd == "sync":
        return cmd_sync(p)
    if a.cmd == "check":
        return cmd_check(p)
    if a.cmd == "files":
        for rel in plugin_files(p):
            print(rel)
        return 0
    return cmd_release(p, a.out)


if __name__ == "__main__":
    sys.exit(main())
