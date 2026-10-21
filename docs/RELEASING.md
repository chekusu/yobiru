# Building and releasing Yobiru

Everything is plain Python 3.8+ with the standard library; no network access is needed.

## Sources of truth

- `packaging/product.json`: plugin id, display name, version, publisher, license, descriptions,
  marketplace names, and which root files are bundled.
- `tailmsg.py` and the Codex bridge files at the repository root are canonical. The plugin carries
  byte-identical copies under `plugins/yobiru/scripts/`, listed with SHA-256 in `scripts/SOURCES.json`.
- Generated, never edited by hand: `plugins/yobiru/plugin.json`, `plugins/yobiru/.codex-plugin/plugin.json`,
  `plugins/yobiru/.cursor-plugin/plugin.json`, `.agents/plugins/marketplace.json`,
  `.cursor-plugin/marketplace.json`, the bundled copies, `plugins/yobiru/LICENSE` and `SOURCES.json`.
- Hand-written: `scripts/yobiru`, `scripts/yobiru.py`, `skills/*/SKILL.md` and `README.md`.
- Host-specific behavior stays in the shared skills: `yobiru-grok-bot` handles the conversation's
  computer and rebuild recovery, while setup, sending and wake configuration use the existing
  skills and runtime. Do not fork the protocol for a marketplace or invent an undocumented manifest.
- `plugins/yobiru/assets/icon.png` is the approved Image Generation master, shared byte-for-byte
  with the mark on [yobiru.com](https://yobiru.com). Both plugin manifests and all READMEs use this
  same asset. Preserve its three petals and the two black eyes at the rounded lower end of the
  orange petal. Replace it with the approved master when the brand changes; do not redraw it.
  The demo image's brand edit is recorded in `docs/assets/yobiru-demo.brand-update.prompt.txt`.

## Commands

```sh
python3 tools/build_plugin.py sync      # regenerate manifests and copies, fix file modes
python3 tools/build_plugin.py check     # no writes: drift, manifest rules, skills, hygiene
python3 tools/build_plugin.py files     # what goes into the plugin archive
python3 tools/build_plugin.py release   # check, then dist/ archives and SHA256SUMS
```

`check` fails when:

- a generated file or bundled copy differs from what `sync` would write, or an executable bit is wrong;
- `plugin.json` leaves the closed Agent Plugins 1.0.0 schema
  (<https://agent-plugins.org/schemas/1.0.0/plugin.schema.json>) or its generated OpenAI extension
  loses required fields. The complete `extensions.com.openai` block replaces the legacy overlay,
  so interface metadata must stay consistent in both; see
  <https://developers.openai.com/plugins/build/plugins>;
- `.codex-plugin/plugin.json` breaks the rules of the Codex plugin-creator validator (fields, semver,
  interface fields, asset paths inside the plugin);
- `.cursor-plugin/plugin.json` or either marketplace file breaks the documented Cursor rules
  (<https://cursor.com/docs/reference/plugins>), or sets component paths that would replace default
  discovery;
- a skill folder lacks `SKILL.md`, its `name` differs from the folder or lacks the `yobiru-` prefix, or
  its description is empty or longer than 1024 characters;
- anything in the plugin is a symlink (including symlinked directories), or looks like runtime state
  or a secret (`config.json`, inbox, outbox, databases, keys, logs, backups);
- the plugin or the repository docs contain a real tailnet name, an absolute home path, a concrete
  Tailscale address, an email address, a UUID, or a token-like string. Maintainers can add personal
  markers in a file outside the repository and point `YOBIRU_PRIVATE_DENYLIST` at it (one regular
  expression per line).

## Release archives

`release` writes to `dist/` (ignored by Git):

| File | Contents | Use |
|---|---|---|
| `yobiru-<version>-plugin.zip` | `yobiru/` = the plugin directory | Cursor local folder, `agent --plugin-dir`, manual install, Grok Bot fallback in `/workspace` |
| `yobiru-<version>-marketplace.zip` | `yobiru-marketplace/` = both marketplace files + `plugins/yobiru/` + `LICENSE` | `codex plugin marketplace add <unpacked folder>` |
| `SHA256SUMS` | SHA-256 of both archives | verification |

Archives are reproducible: entries sorted, timestamps fixed to 1980-01-01, modes fixed (0755 for
`scripts/yobiru` and `scripts/yobiru.py`, 0644 otherwise), stored without compression, junk such as
`__pycache__` excluded. The same commit gives the same bytes. Check out with LF line endings
(`core.autocrlf=false`) when comparing hashes on Windows.

Python's `zipfile` does not restore file modes when unpacking; the skills therefore run the launcher
as `sh .../scripts/yobiru` (or `python3 .../scripts/yobiru.py`), which works without the executable bit.

## Tests

```sh
python3 -m unittest discover -s tests -v
python3 -m unittest -v test_codex_bridge test_codex_queue_client test_codex_resident test_install_codex_bridge test_install_codex_bridge_linux
```

`tests/` builds the archives in a temporary directory, unpacks them into a path with spaces outside
the checkout and drives the unpacked `yobiru` command with an isolated `HOME`, a fake `tailscale`
CLI and loopback-only receivers. No test reads or writes `~/.tailmsg`, calls the real Tailscale, uses
port 7766, or contacts another machine. The Codex bridge tests use temporary AF_UNIX sockets and fake `codex` and
`systemctl` executables; they never reach a real app-server or the user's systemd manager.

`tests/test_public_*.py` and `tests/test_friends_*.py` cover the public bots and Friends (Beta): a real
gateway, connector, deterministic worker, calendar and resource services and callers as separate processes on
loopback ports, with synthetic homes and data; `tests/support/synthetic_host.py` plays the owner's agent (a
protocol fixture, not an agent). `tests/fixtures/bot_gateway_v1.py` builds real first-version databases for the
migration tests.

`sh selftest.sh` is the upstream end-to-end check. It needs Tailscale on the machine and runs
temporary receivers on the machine's own Tailscale address, ports 7866-7883 (and a stub webhook on
127.0.0.1:7879); confirm those ports are free first. It never uses port 7766 or `~/.tailmsg`. The
older receiver it checks compatibility against is `tests/fixtures/tailmsg_v1_pre_agents.py`, the
protocol v1 release from before @agent addressing. Its runtime files go to `test-run/` (ignored by Git).

## Version bumps

Change `version` in `packaging/product.json` and `PRODUCT_VERSION` in the canonical `tailmsg.py`,
run `sync`, `check`, the relevant tests and `release`, then commit
the regenerated files. Codex reinstalls a local plugin when its version changes. Update the version line of the
plugin README (a test reads it) and add a `CHANGELOG.md` entry. `check` accepts only a SemVer 2.0 version: dot-separated
prerelease identifiers, none empty, numeric ones without leading zeros.

### Prereleases

A prerelease such as `0.6.0-beta.1` is published so that no stable installation picks it up by itself:

- Point `codex.privacyPolicyURL` in `packaging/product.json` into the prerelease's own tag
  (`.../blob/v<version>/docs/PRIVACY.md`); `check` refuses a prerelease that links another branch's or tag's notice.
- Publish a release branch and the tag `v<version>` only. Keep the default branch on the stable release: the
  repository marketplace (`codex plugin marketplace add|upgrade`) and plugin pages install what the default branch
  holds, whatever GitHub release flags say.
- Mark the GitHub release as a prerelease and not as the latest release, and attach the `release` output
  (`yobiru-<version>-plugin.zip`, `yobiru-<version>-marketplace.zip`, `SHA256SUMS`).
- The stable update channel never offers it; `update check --channel preview` names its tag, archive and checksum
  file. Announce it with `update notify --channel preview`; its notices carry `prerelease: true` and ask the receiving
  owner to opt in.
- In the `CHANGELOG.md` entry, link files that exist only in the prerelease by absolute URLs pinned to its tag, so the
  same entry also reads correctly on the default branch.

## Marketplace submissions

Use [MARKETPLACE_SUBMISSION.md](MARKETPLACE_SUBMISSION.md) for current official portals, required
listing fields and the actual submission state. The Grok Bot/Cursor form accepts the public
repository. OpenAI's skills-only desktop route uses the same package with
`requires_local_executor: true`; it does not require inventing a hosted MCP service.

Before submission, verify the exact package, its [privacy notice](PRIVACY.md), reference files and
the intended host's native installation and skill invocation. Generic CLI tests or an existing
receiver are not substitutes for a required in-app plugin test. Keep test evidence sanitized:
no home addresses, tailnet domains, session IDs, account details or webhook secrets. Submit only
statements supported by completed tests. Store a receipt or status returned by the official portal;
mark public availability only after a listing or publication is actually confirmed.

## Renaming

The product id is defined once in `packaging/product.json`. A rename means: update `id`,
`displayName`, `pluginDir`, `skillPrefix` and the marketplace names there; `git mv` the plugin folder
and the skill folders (and their `name:` lines); rename `scripts/yobiru*` and the `PRODUCT`,
`DISPLAY` and `YOBIRU_PYTHON` constants in `scripts/yobiru.py`; update the docs; run `sync` and `check`.
Keep the tailmsg protocol, `tailmsg.py`, `~/.tailmsg`, `TAILMSG_*` variables and service names, so
deployed machines need no change.
