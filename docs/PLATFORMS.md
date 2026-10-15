# Platform notes and sources

What each host documents about plugins, which formats Yobiru ships for it, and what is still
unverified. Facts below are from the linked official pages as read on 2026-10-10; check them again
before relying on them for a release.

## Formats shipped

| File | Read by | Notes |
|---|---|---|
| `plugins/yobiru/plugin.json` | Agent Plugins clients (Cursor, Codex) | Closed schema: `$schema`, `name`, `version`, `description`, `author`, `homepage`, `repository`, `license`, `keywords`, `extensions`. No `extensions` on purpose: an `extensions["com.openai"]` block would replace the `.codex-plugin` overlay. Skills come from the fixed `skills/` location. |
| `plugins/yobiru/.codex-plugin/plugin.json` | Codex | Compatibility manifest with the Codex `interface` block; validated with the Codex plugin-creator validator. |
| `plugins/yobiru/.cursor-plugin/plugin.json` | Cursor | Native manifest; no component paths, so Cursor's default discovery finds `skills/`. |
| `.agents/plugins/marketplace.json` | Codex repository marketplace | `source: {source: local, path: ./plugins/yobiru}`, policy `AVAILABLE` / `ON_INSTALL`, category `Productivity`. |
| `.cursor-plugin/marketplace.json` | Cursor (repository, team marketplace import) | `name`, `owner.name`, `plugins[]` with `source: plugins/yobiru`. |

Not shipped: MCP servers (`mcp.json`), hooks, rules, commands, agents, variables. The skills plus the
bundled command already cover every operation. A stdio MCP wrapper would need separate tested launch
configuration per host, because Cursor does not expand the standard `${PLUGIN_ROOT}` variable in
`mcp.json`, and a remote MCP endpoint would need a server this project deliberately does not run.

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

## OpenAI Codex

- Plugin format, `.codex-plugin` overlay, `extensions["com.openai"]`, marketplace locations, cache
  location `~/.codex/plugins/cache/<marketplace>/<plugin>/<version>/` (`local` for local marketplaces):
  <https://developers.openai.com/plugins/build/plugins>
- Public directory: the remote HTTPS (Streamable HTTP) endpoint requirement applies to plugins that
  submit an MCP server; skills-only plugins have their own submission path, and lifecycle hooks must
  be removed for either path: <https://developers.openai.com/plugins/guides/submit-claude-plugin>.
  The same page asks plugins whose core value needs local execution to contact OpenAI first, as they
  may need product-specific review. Yobiru runs local commands and handles inbound messages, so no
  public-directory eligibility is claimed; this release targets local and repository marketplaces.
- CLI used in the instructions: `codex plugin marketplace add|upgrade|remove`, `codex plugin
  add|remove NAME@MARKETPLACE` (checked against `codex plugin --help`, codex-cli 0.162.1).
- Optional Codex bridge: `codex queue --thread <session UUID or exact name> --message TEXT` queues a
  message for an existing session; `--remote` accepts `ws://`, `wss://`, `unix://` or `unix://PATH`
  (checked against `codex queue --help`, codex-cli 0.162.1). Yobiru passes only a session UUID and an
  explicit `unix:///absolute/path`. Reconciliation runs `codex app-server --listen stdio://` for a
  read-only `thread/read` with the pinned `CODEX_HOME`. The Linux bridge expects the user to run the
  app-server themselves (for example `codex app-server --listen unix://PATH`); how a given setup keeps
  that app-server running is outside Yobiru. Neither has been exercised against a live app-server for
  this release; see [VERIFICATION.md](VERIFICATION.md).

## Cursor

- Plugin reference (manifests, discovery, marketplace file, submission checklist):
  <https://cursor.com/docs/reference/plugins>
- Installing, team marketplaces (Dashboard → Plugins & MCPs → Add Marketplace → Import from Repo),
  installation modes, local testing in `~/.cursor/plugins/local`: <https://cursor.com/docs/plugins>
- Skills may ship scripts that agents run: <https://cursor.com/docs/skills>
- CLI: `--plugin-dir <path>` loads a local plugin; `agent plugin marketplace add <git-url>` registers a
  marketplace: <https://cursor.com/docs/cli/reference/parameters>, <https://cursor.com/docs/cli/changelog>
- Public listing: submission at <https://cursor.com/marketplace/publish>, manual review, public Git
  repository required.

## Grok Bot

Grok Bot is documented on cursor.com and docs.x.ai. It is not the Grok Build CLI.

- Plugins are added from **Plugins** in the sidebar (mobile: avatar → **Plugins**); a Cursor team admin
  controls which marketplace plugins are available: <https://cursor.com/help/grok-bot/connect-plugins>
- The Marketplace installs supported connectors and packaged skills; private skills are shared by the
  account's Bots: <https://docs.x.ai/grok-bot/skills-routines-and-automations>. A private skill missing
  from the `/` menu is enabled under **Settings → Plugins → Yours**: <https://cursor.com/docs/grok-bot/work>
- Bots work on a persistent cloud computer with a shell and filesystem; `/workspace` is the durable
  place for files, while hand-installed packages can disappear on rebuilds:
  <https://cursor.com/docs/grok-bot/work>
- Which computer a conversation uses: the owner's chat uses the owner's computer, a teammate's chat
  usually the teammate's computer, Slack channels and group chats a shared computer:
  <https://docs.x.ai/grok-bot/team-bots>, <https://cursor.com/help/grok-bot/team-bots>
- Custom MCP servers can be **Command** (runs on the conversation's computer) or **Remote HTTPS**:
  <https://docs.x.ai/grok-bot/team-bots>
- Tailscale on team computers (Enterprise **Team Setup** scripts, Debian-based Linux, `sudo`
  available): <https://cursor.com/docs/grok-bot/private-networks>
- Routine webhooks: the routine's **Webhook** section shows the URL, the key and the header
  `Authorization: Bearer <key>`; a 200 means a run was accepted and started, not finished:
  <https://cursor.com/help/grok-bot/routines>. The page does not say whether a webhook call that
  arrives while an earlier run is still going waits for it or starts a parallel run.

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

Still unverified: that a Cursor-marketplace install of Yobiru makes its skills appear in Grok Bot,
and that the plugin's `scripts/` folder is present on the conversation's computer. The skills fall
back to an unpacked release archive in `/workspace` if the scripts are missing.

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
5. Marketplaces: the repository <https://github.com/chekusu/yobiru> works as a Codex marketplace
   (`codex plugin marketplace add chekusu/yobiru`) and as a Cursor team marketplace import. A public
   Cursor Marketplace listing needs a submission and Cursor's review; nothing has been submitted.
