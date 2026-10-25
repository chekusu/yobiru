# Platform notes and sources

What each host documents about plugins, which formats Yobiru ships for it, and what is still
unverified. Marketplace submission routes were rechecked on 2026-10-15. Historical host behavior
and CLI observations retain their dates below; check them again before relying on a new host version.

## Formats shipped

| File | Read by | Notes |
|---|---|---|
| `plugins/yobiru/plugin.json` | Agent Plugins clients (Cursor, Codex) | Closed portable schema. `extensions.com.openai` contains the complete OpenAI interface, `requires_local_executor: true`, onboarding and publication metadata; it replaces the legacy overlay rather than merging with it. Skills come from `skills/`. |
| `plugins/yobiru/.codex-plugin/plugin.json` | Older Codex plugin clients | Compatibility manifest with the shared `interface` block; current OpenAI directory metadata lives in the portable extension. Public-directory review is separate from repository installation. |
| `plugins/yobiru/.cursor-plugin/plugin.json` | Cursor | Native manifest; no component paths, so Cursor's default discovery finds `skills/`. |
| `.agents/plugins/marketplace.json` | Codex repository marketplace | `source: {source: local, path: ./plugins/yobiru}`, policy `AVAILABLE` / `ON_INSTALL`, category `Productivity`. |
| `.cursor-plugin/marketplace.json` | Cursor (repository, team marketplace import) | `name`, `owner.name`, `plugins[]` with `source: plugins/yobiru`. |
| `plugins/yobiru/.claude-plugin/plugin.json` | Claude Code | Native manifest; default discovery finds `skills/`. Declares one stdio MCP server inline: `sh ${CLAUDE_PLUGIN_ROOT}/scripts/yobiru mcp`. |
| `.claude-plugin/marketplace.json` | Claude Code repository marketplace | `name`, `owner.name`, `plugins[]` with `source: ./plugins/yobiru` and the version. |

Not shipped: a shared MCP file (`mcp.json` / `.mcp.json`), hooks, rules, commands, agents, variables. The
skills plus the bundled command already cover every operation. The one MCP server lives in the Claude Code
manifest only, because Claude Code expands `${CLAUDE_PLUGIN_ROOT}` and no other host reads that file; a
shared `mcp.json` would reach Cursor, which does not expand the standard `${PLUGIN_ROOT}` variable. Other
MCP clients can register `yobiru mcp` by hand. Private Tailscale messaging does not expose a hosted MCP
endpoint. Friends has a separate gateway endpoint, described below.

For public bots and Friends, a user may also add the local stdio proxy `yobiru public mcp` to an MCP client by
hand, and a gateway serves the same API at `/mcp` to bearer-authenticated clients (no OAuth); see
[public bots and Friends](PUBLIC_BOTS.md#mcp).

## Tailscale (onboarding)

`yobiru onboard`, `doctor` and the setup docs cite only these official sources (checked 2026-10-11):
<https://tailscale.com/docs/install/linux>, <https://tailscale.com/docs/install/mac>,
<https://tailscale.com/docs/install/windows>, <https://tailscale.com/download> and the Linux install
script <https://tailscale.com/install.sh>. Linux: the script installs the package with root/sudo and
`tailscale up` prints a login URL the user opens. macOS: the app (Mac App Store or standalone) asks the
user to approve its system extension / VPN configuration; its CLI lives inside the app bundle. Windows:
the installer needs administrator rights; sign-in happens in the app. Yobiru's receiver binds the
machine's own Tailscale address, so hosts that can only run Tailscale in userspace networking mode (no
`/dev/net/tun`) are reported as blocked rather than worked around.

## OpenAI: Codex and ChatGPT desktop

- Plugin format, `.codex-plugin` overlay, `extensions["com.openai"]`, marketplace locations, cache
  location `~/.codex/plugins/cache/<marketplace>/<plugin>/<version>/` (`local` for local marketplaces):
  <https://developers.openai.com/plugins/build/plugins>
- Current submission portal: <https://platform.openai.com/plugins>. Skills that run local programs
  follow the desktop-only route, declaring `requires_local_executor: true` in `extensions.com.openai`:
  <https://developers.openai.com/plugins/deploy/desktop-only>. Yobiru uses this route with no MCP
  server registration and no second ChatGPT implementation. It requires a local execution environment with
  Python and Tailscale, not a hosted ChatGPT web/mobile connector. Organization eligibility,
  identity verification, clean desktop testing and review still apply. A portal access restriction
  must be reported rather than bypassed. Local MCP allowlisting is a separate case; the plugin
  registers no MCP server (the optional `yobiru public mcp` stdio proxy for public bots is added by a
  user by hand and is not part of a submission). See [submission status](MARKETPLACE_SUBMISSION.md).
- CLI used in the instructions: `codex plugin marketplace add|upgrade|remove`, `codex plugin
  add|remove NAME@MARKETPLACE` (checked against `codex plugin --help`, codex-cli 0.162.1).
- Optional Codex bridge: `codex queue --thread <session UUID or exact name> --message TEXT` queues a
  message for an existing session; `--remote` accepts `ws://`, `wss://`, `unix://` or `unix://PATH`
  (checked against `codex queue --help`, codex-cli 0.162.1). Yobiru passes only a session UUID and an
  explicit `unix:///absolute/path`. Reconciliation runs `codex app-server --listen stdio://` for a
  read-only `thread/read` with the pinned `CODEX_HOME`. The Linux bridge expects the user to run the
  app-server themselves (for example `codex app-server --listen unix://PATH`); how a given setup keeps
  that app-server running is outside Yobiru. Historical live Linux 0.5.0 acceptance and remaining
  host tests are separated in [VERIFICATION.md](VERIFICATION.md).

## Claude Code

- Plugins, marketplaces and the `${CLAUDE_PLUGIN_ROOT}` variable: <https://code.claude.com/docs/en/plugins>,
  <https://code.claude.com/docs/en/plugin-marketplaces>, <https://code.claude.com/docs/en/plugins-reference>
- CLI used in the instructions (checked against `claude plugin --help`, Claude Code 2.1.306):
  `claude plugin marketplace add|update|remove`, `claude plugin install|update|uninstall NAME@MARKETPLACE`,
  `claude plugin validate [--strict] PATH`. Inside a session: `/plugin marketplace add chekusu/yobiru`,
  `/plugin install yobiru@yobiru`.
- `yobiru mcp` (`plugins/yobiru/scripts/yobiru_mcp.py`) speaks MCP over stdio (protocol 2025-11-25,
  2025-06-18 and 2025-03-26; tools only). Each tool runs one `yobiru` command of the same plugin and data
  home, with option values before `--` and positional values after it. It listens on no port and holds no
  state of its own; the receiver (`serve`) still has to run for anything to arrive. If the config has an
  agent named `claude`, the server sends as `claude` (`--as`) and lists `claude`'s inbox; `--agent NAME`
  chooses another name.
- Waking: the optional `yobiru claude-bridge` adapter starts visible background Remote Control sessions with durable intent and exact-session reconciliation. Installation previews by default and never starts a service; see [Claude bridge setup and recovery](CLAUDE_BRIDGE.md). Synthetic tests do not establish native account acceptance. Existing custom adapters are preserved.
- Checked on 2026-10-21 with Claude Code 2.1.306: `claude plugin validate --strict` passes for the plugin
  and the marketplace, `claude plugin marketplace add` + `claude plugin install yobiru@yobiru` from a local
  checkout work, and `claude mcp list` reports the plugin's server as connected.
- Live on 2026-10-21, Mac Studio (receiver 0.6.0-dev.1, agent `claude` added) to a Grok Bot cloud computer
  (receiver 0.6.0-beta.1): Claude Code 2.1.306 loaded the plugin from a checkout (`--plugin-dir`) and listed
  the 10 tools; its headless run then stopped at sign-in, so the steps were driven over stdio against the
  server launched as the manifest launches it. A tracked `yobiru_send` was received at once and completed
  about 26 s later; Grok Bot's plain message to `claude` was read, replied to (routed to `grok`) and
  acknowledged. The run also showed that a long `request list` overflowed the output cap; listings are now
  filtered to the server's agent and trimmed (`limit`, `total`, `truncated`).

## Cursor

- Plugin reference (manifests, discovery, marketplace file, submission checklist):
  <https://cursor.com/docs/reference/plugins>
- Installing, team marketplaces (Dashboard → Plugins & MCPs → Add Marketplace → Import from Repo),
  installation modes, local testing in `~/.cursor/plugins/local`: <https://cursor.com/docs/plugins>
- Skills may ship scripts that agents run: <https://cursor.com/docs/skills>
- CLI: `--plugin-dir <path>` loads a local plugin; `agent plugin marketplace add <git-url>` registers a
  marketplace: <https://cursor.com/docs/cli/reference/parameters>, <https://cursor.com/docs/cli/changelog>
- Public listing: the shared Grok Bot/Cursor submission form at
  <https://x.ai/bot/marketplace/submit-a-plugin> accepts a public repository and requires local and
  in-app testing. Review is separate from submission. Do not submit duplicate entries merely because
  Cursor and Grok Bot are different installation surfaces.

## Grok Bot

Grok Bot is documented on cursor.com and docs.x.ai. It is not the Grok Build CLI.

- Marketplace submission: <https://x.ai/bot/marketplace/submit-a-plugin> targets Grok Bot and Cursor;
  its Grok option is marked coming soon. This repository supplies a shared skills package and a
  dedicated `yobiru-grok-bot` skill, not a separate Grok-specific manifest. See the
  [Grok Bot setup and recovery guide](GROK_BOT.md).

- Plugins are added from **Connect apps** in the sidebar (mobile: avatar → **Connect apps**) and
  confirmed under **Installed**; a Cursor team admin controls which marketplace plugins are available:
  <https://cursor.com/help/grok-bot/connect-plugins> (navigation rechecked 2026-10-15).
- The Marketplace installs supported connectors and packaged skills; private skills are shared by the
  account's Bots: <https://docs.x.ai/grok-bot/skills-routines-and-automations>. Check the current account's
  skill management controls if a private skill is missing from the `/` menu:
  <https://cursor.com/docs/grok-bot/work>.
- Bots work on a persistent cloud computer with a shell and filesystem; `/workspace` is the durable
  place for files, while hand-installed packages can disappear on rebuilds:
  <https://cursor.com/docs/grok-bot/work>
- Which computer a conversation uses: the owner's chat uses the owner's computer, a teammate's chat
  usually the teammate's computer, Slack channels and group chats a shared computer:
  <https://docs.x.ai/grok-bot/team-bots>, <https://cursor.com/help/grok-bot/team-bots>
- Custom MCP servers can be **Command** (runs on the conversation's computer) or **Remote HTTPS**:
  <https://docs.x.ai/grok-bot/team-bots>. Neither has been tried with Yobiru's public bot MCP endpoint
  or stdio proxy; a Remote HTTPS server that requires OAuth cannot use its static bearer credential.
- Tailscale on team computers (Enterprise **Team Setup** scripts, Debian-based Linux, `sudo`
  available): <https://cursor.com/docs/grok-bot/private-networks>
- Routine webhooks: the routine's **Webhook** section shows the URL, the key and the header
  `Authorization: Bearer <key>`; a 200 means a run was accepted and started, not finished:
  <https://cursor.com/help/grok-bot/routines>. The page does not say whether a webhook call that
  arrives while an earlier run is still going waits for it or starts a parallel run.

Maintainers' historical evidence: on 2026-10-13, a manually deployed 0.5.0 Grok Bot runtime completed a
real cross-machine tracked request and authenticated status refresh. This does not verify marketplace
installation or native skill discovery.

Community-reported, not verified by the maintainers: one user's real account, with the Yobiru 0.5.0
release archive unpacked in `/workspace` (not a marketplace install), reported in
[#11](https://github.com/chekusu/yobiru/issues/11):

- 2026-10-13: the conversation's computer had `/dev/net/tun` and passwordless `sudo`; `tailscaled`
  and a long-running `tailmsg.py serve` ran there, with Tailscale state under `/workspace` and a
  small restore script that starts the receiver. The peer was reached through a DERP relay, not a
  direct path.
- 2026-10-13: a routine webhook set up as the yobiru-wake skill describes (`--webhook --url-file …
  --secret-file …`, plus `--peer-max <peer>=now`) woke the routine for a tracked request from an
  OpenClaw agent on a Mac (OpenClaw 2026.10.2). The routine reported `running`, sent its own tracked
  request back, and both requests completed before their timeouts (`late: false`). Repeated on
  2026-10-14 with OpenClaw 2026.10.3.
- These runs cover the success path only. No failure, retry or duplicate-wake path ran live.
- Each message that wakes the routine starts a run, and a run lists all pending messages, so two
  overlapping runs could pick up the same message; a `running` state does not reserve it. That
  user's routine now claims each message id atomically (one directory per id under `/workspace`)
  before working on it. Concurrent `mkdir` was tested; a real duplicate webhook run was not.

Still to establish by the maintainers: that a Cursor-marketplace install of Yobiru makes its skills appear in
Grok Bot and the host invokes them, that the plugin's `scripts/` folder is present on the conversation's
computer (the skills fall back to an unpacked release archive in `/workspace` if it is missing), that the
account's routine reaches the intended computer, and automatic recovery after a rebuild. The setup skill
verifies official release checksums before using an archive in `/workspace`.

## Meta Muse

The official Tailscale connector joins Muse as its own tailnet node and supports outbound connections. Use the packaged **yobiru-muse** skill and `yobiru muse` adapter: check the actual CLI/identity interface, send tracked pull requests and poll the same request ID. No receiver is started inside Muse. If the connector cannot be used, the intended bot's Friends link provides the existing outbound HTTPS alternative. See [Muse setup, connector contract and limits](MUSE.md).

The implementation has synthetic one-way transport tests; native Muse execution, region/account availability, secure credential storage and directory listing are separate, unverified acceptance states. The gateway's existing remote MCP uses static bearer credentials, not an OAuth integration. Meta's custom connector and directory submission flows must not be conflated.

## Remaining acceptance steps

1. Codex: CLI install and skill discovery are done (see [VERIFICATION.md](VERIFICATION.md)). Still open: a new
   thread asking "Set up Yobiru on this machine", confirming the agent runs `onboard` from the plugin
   cache path, and a look at the Codex desktop app's plugin page.
2. Cursor: load `plugins/yobiru` with `agent --plugin-dir` or `~/.cursor/plugins/local/yobiru`,
   confirm the skills in **Customize**, run the same prompt.
3. Grok Bot: a release-archive setup with Tailscale, a receiver and a routine webhook wake has a
   community report (above). Still open: import <https://github.com/chekusu/yobiru> as a Cursor team
   marketplace, enable Yobiru, add it in Grok Bot, confirm the skills and run `paths` on the
   conversation's computer.
4. Two real machines: setup on both, `send`, `fetch`, `reply`, and a `now` wake through each adapter.
5. Claude Code: the same exchange driven by a signed-in Claude Code session itself (marketplace install,
   then the model calling the tools), and Windows.
6. Marketplaces: the repository <https://github.com/chekusu/yobiru> is packaged for Codex repository
   installation (`codex plugin marketplace add chekusu/yobiru`) and Cursor team import. Record
   actual host tests, submission receipts, review decisions and public listing links separately in
   [MARKETPLACE_SUBMISSION.md](MARKETPLACE_SUBMISSION.md). Preparing a valid archive is not a submission.

## macOS gateway interpreter

For a multiprocessing Friends gateway on macOS, use a standalone modern CPython installation.
During the October 26, 2026 maintenance run, Apple's system Python 3.9 crashed in its SQLite
`openDatabase` path after `fork` (`_os_log_preferences_refresh`); the single-process connector tests
passed on the same machine. CI selects CPython 3.12 explicitly on both macOS and Linux so this host
interpreter is not selected by PATH accident. The CI result for a particular commit remains the
acceptance evidence; selecting that interpreter is not itself a passing test.

This limitation concerns the multiprocessing gateway and Apple's bundled interpreter. It does not
change the Python 3.8+ requirement for the private messaging CLI or imply all Python 3.9 builds fail.
