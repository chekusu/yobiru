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

Not shipped in any manifest: MCP server registrations (`mcp.json`), hooks, rules, commands, agents,
variables. The skills plus the bundled command already cover every operation, and no host starts an MCP
server from the plugin on its own: a registration would need separate tested launch configuration per
host, because Cursor does not expand the standard `${PLUGIN_ROOT}` variable in `mcp.json`. For the
unreleased public bots and Friends only, a user may add the local stdio proxy `yobiru public mcp` to an MCP
client by hand, and a gateway the user hosts serves the same API at `/mcp` to bearer-authenticated clients
(no OAuth). This project runs no MCP service of its own, and no hosted MCP platform has been tested; see
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
  <https://cursor.com/help/grok-bot/routines>

Historical evidence: on 2026-10-13, a manually deployed 0.5.0 Grok Bot runtime completed a real
cross-machine tracked request and authenticated status refresh. This does not verify marketplace
installation or native skill discovery. Still to establish for that route: the host installs and
invokes the skills, bundled scripts are present or the documented archive fallback works, and the
account's routine reaches the intended computer. Automatic recovery after a rebuild also needs its
own test. The setup skill verifies official release checksums before using an archive in `/workspace`.

## Remaining acceptance steps

1. Codex: CLI install and skill discovery are done (see [VERIFICATION.md](VERIFICATION.md)). Still open: a new
   thread asking "Set up Yobiru on this machine", confirming the agent runs `onboard` from the plugin
   cache path, and a look at the Codex desktop app's plugin page.
2. Cursor: load `plugins/yobiru` with `agent --plugin-dir` or `~/.cursor/plugins/local/yobiru`,
   confirm the skills in **Customize**, run the same prompt.
3. Grok Bot: import <https://github.com/chekusu/yobiru> as a Cursor team marketplace, enable Yobiru,
   add it in Grok Bot,
   confirm the skills, run `paths` on the conversation's computer, set up Tailscale and a receiver
   there, and test a routine webhook wake with a message from another machine.
4. Two real machines: setup on both, `send`, `fetch`, `reply`, and a `now` wake through each adapter.
5. Marketplaces: the repository <https://github.com/chekusu/yobiru> is packaged for Codex repository
   installation (`codex plugin marketplace add chekusu/yobiru`) and Cursor team import. Record
   actual host tests, submission receipts, review decisions and public listing links separately in
   [MARKETPLACE_SUBMISSION.md](MARKETPLACE_SUBMISSION.md). Preparing a valid archive is not a submission.
