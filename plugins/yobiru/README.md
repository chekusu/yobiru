# yobiru (ヨビル) plugin

<a href="https://yobiru.com"><img src="assets/icon.png" width="112" height="112" alt="yobiru — two small eyes in the orange spirit's rounded head"></a>

The final product logo set is maintained in
[`final logo set`](https://github.com/chekusu/yobiru/tree/main/brand/logo/yobiru/). This package's
`assets/icon.png` remains the square plugin icon used by marketplace metadata.

Agent-to-agent messages, file handoff and receiver-controlled wake-ups between machines on your own
Tailscale network, using the tailmsg protocol v1. One package for Codex, Cursor, Claude Code and Grok Bot.

Version 0.7.15. Source: <https://github.com/chekusu/yobiru>; what changed:
[changelog](https://github.com/chekusu/yobiru/blob/v0.7.15/CHANGELOG.md). Public submission and review
status: [marketplace record](https://github.com/chekusu/yobiru/blob/main/docs/MARKETPLACE_SUBMISSION.md).
Repository installation does not establish a public listing.

## Contents

| Path | What it is |
|---|---|
| `plugin.json` | Agent Plugins 1.0.0 manifest (portable). |
| `.codex-plugin/plugin.json` | Codex manifest (presentation and skills). |
| `.cursor-plugin/plugin.json` | Cursor manifest. |
| `.claude-plugin/plugin.json` | Claude Code manifest; registers the local MCP server `yobiru` (`scripts/yobiru mcp`). |
| `skills/yobiru-muse` | Muse custom connector: official Tailscale first, Friends fallback, pull-only requests and same-ID recovery. |
| `skills/yobiru-setup` | Guided onboarding (also without Tailscale yet), setup and repair of this machine. |
| `skills/yobiru-grok-bot` | Grok Bot conversation-computer selection, plugin verification and rebuild recovery. |
| `skills/yobiru-sessions` | Which machines and agents are online, and which session (name + exact id) handles each. |
| `skills/yobiru-upgrade` | Update checks, safe upgrades and versioned notices to your other installations. |
| `skills/yobiru-send` | Send messages, data and files; track requests with receipts, deadlines and final outcomes. |
| `skills/yobiru-inbox` | Read, fetch, process and report outcomes; acknowledge locally. Notifications need no reply. |
| `skills/yobiru-wake` | Who may wake which agent, and how (program, Grok Bot routine webhook, Codex bridge). |
| `skills/yobiru-bot-contacts` | Talk to someone's published bot from its share link (connect, follow, chat). |
| `skills/yobiru-contact-bots` | One bot per friend in your own host, with their name and picture, synced every minute. |
| `skills/yobiru-public-bot` | Publish and share a bot, set who may use what, review chat for your own agent, run the deterministic worker. |
| `skills/yobiru-friends-host` | Your own agent answers your bot's chat: directly for people in the inbox (followed or accepted), as drafts you approve otherwise. |
| `scripts/yobiru_muse.py` | Muse connector implementation used by `yobiru muse`. |
| `scripts/yobiru`, `scripts/yobiru.py` | The `yobiru` command: setup, doctor and status, plus the messaging commands. |
| `scripts/yobiru_mcp.py` | `yobiru mcp`: a stdio MCP server whose tools run the messaging commands (inbox, read, fetch, send, reply, respond, status, ack). |
| `scripts/tailmsg.py` | The bundled tailmsg CLI and receiver (stdlib only). |
| `scripts/codex_bridge/` | Optional bridge into one fixed Codex session: macOS desktop app, or Linux user systemd + local app-server socket. |
| `scripts/claude_bridge/` | Optional visible Claude Code Remote Control bridge with durable session reconciliation. |
| `scripts/public_bots/` | Public bots and Friends: the gateway (with an optional MCP endpoint), the client (callers, owners, your agent, connector, worker, a local MCP stdio proxy) and the private calendar and resource services (stdlib only). |
| `docs/PUBLIC_BOTS.md` | The public bots and Friends guide: identities, follows and permissions, supervised chat, delivery evidence, decision receipts, direct messages and loop budgets, attachments, MCP, limits, storage, deployment. |
| `examples/public-bot-demo.sh` | A self-contained demo of the deterministic part: synthetic data on 127.0.0.1 only, deleted when it ends (`sh examples/public-bot-demo.sh`). |
| `scripts/SOURCES.json` | Where the bundled files come from, with SHA-256. |

No hooks or permission settings are included, and installing the plugin runs nothing. Claude Code starts
the bundled MCP server when a session begins; it runs only this plugin's CLI on stdio and opens no port.
In Claude Code: `/plugin install yobiru --marketplace chekusu/yobiru` (or `/plugin marketplace add
chekusu/yobiru`, then `/plugin install yobiru@yobiru`).
For public bots and Friends, `yobiru public mcp` is a stdio MCP server you may add to an MCP client yourself, and a
gateway can serve `/mcp` to bearer-authenticated clients (no OAuth).

The OpenAI package declares desktop execution because the skills run local programs. It uses the
same implementation for Codex and compatible ChatGPT desktop installations; no hosted web or mobile
connector is provided. Grok Bot and Cursor use the shared skills and Cursor-compatible metadata.
See the [Grok Bot guide](https://github.com/chekusu/yobiru/blob/v0.7.15/docs/GROK_BOT.md) and
[privacy notice](https://github.com/chekusu/yobiru/blob/v0.7.15/docs/PRIVACY.md) of this release.

## Requirements

Private messaging: Python 3.8+, Tailscale signed in on each machine, and for receiving: a running
receiver, shields-up off and a tailnet policy that lets your chosen peers reach `tcp:7766`.

Public bots and Friends are new in 0.6.0.
Callers, your answering agent, the connector and the worker need only Python 3.8+ and outbound HTTPS to the
gateway, not Tailscale; plain HTTP is accepted only for a gateway on the same machine (loopback). The gateway
host needs an HTTPS reverse proxy that you set up (see `docs/PUBLIC_BOTS.md`). Chat is answered by your own
agent: directly for people your bot follows or you accepted (the inbox), while everyone else's messages wait in
message requests that you accept, ignore or block.

## Quick start

`yobiru` locates itself, so call it by its absolute path from any directory (quote it; install paths
may contain spaces). On Windows use `py -3 <plugin>\scripts\yobiru.py`.

```sh
Y="/path/to/yobiru/scripts/yobiru"
sh "$Y" onboard                                          # read-only step-by-step plan for this platform
sh "$Y" paths                                            # locations as JSON
sh "$Y" doctor                                           # read-only checks
sh "$Y" setup --agent codex --self-agent codex --default-agent codex   # plan
sh "$Y" setup --agent codex --self-agent codex --default-agent codex --apply
sh "$Y" install-runtime --apply                          # stable tailmsg.py in the data home
sh "$Y" service-template systemd-user                    # print a service definition to install
sh "$Y" send laptop/codex "hello" --file ./notes.md      # 202 = delivered (not done)
sh "$Y" request send laptop/codex "Review the notes and report your result." --timeout-seconds 900 --wake now
sh "$Y" request status REQUEST_ID --refresh --json
sh "$Y" inbox --pending --agent codex
sh "$Y" agents --online                                  # versions, agents, session names + ids
sh "$Y" update check                                     # official stable releases (--channel preview: prereleases)
```

Data home: `--home DIR` (before the command), else `$TAILMSG_HOME`, else `~/.tailmsg`. Existing
tailmsg homes are used as they are; `setup` and `wake-config` only add what you name, need
`--replace` to change existing values, and back up `config.json` on every write.

## Request lifecycle

`request send` and `send --kind request` require a peer that advertises `request-lifecycle-v1` in
`/info`. The receiver records a minimal `received` receipt, then the agent reports `running` and
one final `completed`, `failed`, `rejected` or `blocked` outcome. The result is durably saved before
local `ack` and retried with a stable response ID. A timeout is queryable as `timed_out`, meaning
actual execution is unknown; it does not cancel the task. Late final results can reconcile it.

Use `request status ID --refresh --json` to ask the original receiver, or omit `--refresh` for local
state. Receivers use `request status ID --direction incoming --json`. `request list --json` lists
both directions. A running receiver maintains the ledger and response queue automatically;
`request maintain` runs one pass manually. No exactly-once execution or offline completion is promised.

Tracked responses stay in the authenticated request ledger, not the ordinary inbox, and never wake
an agent. Get `response.id` from `request status ID --json`, then use `show RESPONSE_ID` to inspect
the result or `fetch RESPONSE_ID` to retrieve verified attachments into `<home>/inbox/RESPONSE_ID/files/`.
Do not process protocol responses as new tasks or send replies to them.

Notifications (`--kind notice`, including upgrade notices) need no reply. Send a separate tracked
request if you need a result. Old peers need upgrading; `send --kind request --legacy-request` is an
explicit opt-out without lifecycle guarantees. Protocol replies do not wake agents or cause loops.

Codex business replies stay under local `allow_replies` authorization. If disabled, the bridge
records a minimal `blocked` result without running the model. Enabling replies does not grant new
task execution authority. Preserve local scope restrictions when updating dispatcher instructions.

## Commands

| Command | Writes? |
|---|---|
| `paths`, `doctor`, `status`, `version`, `onboard`, `policy show`, `session list`, `update status` | never |
| `agents` / `peers` | never (`--remember` records answering installations in `upgrade-notices.json`) |
| `update check` | never (`--record` saves the result for reminders) |
| `session bind` / `unbind`, `policy set`, `update notify`, `update maintain` | only with `--apply` |
| `update reminder-template KIND` | never (prints an opt-in weekly check; `--retry-notices` checks/retries existing notices every 15 minutes and after startup) |
| `setup`, `wake-config`, `install-runtime` | only with `--apply`; `install-runtime --upgrade` keeps a newer runtime (a development build, for example) unless `--allow-downgrade` |
| `service-template KIND` | never (prints a definition) |
| `codex-bridge status` | never; `install` / `rollback` (macOS or Linux) support `--dry-run` (no writes) and `--no-start` (files only) |
| `request send`, `request respond`, `request maintain` | save request/response state; maintain may retry responses over the network |
| `request status`, `request list` | read-only; status `--refresh` contacts the original receiver and saves the reconciled response |
| `send`, `reply`, `inbox`, `show`, `fetch`, `ack`, `wake`, `serve` | tailmsg commands; tracked requests require a durable outcome before ack |
| `gateway init`, `gateway owner add/rotate`, `gateway migrate` | write a new gateway home or credential file (never overwrite), or upgrade an earlier database (schema 1 or 2) to schema 3 in one transaction; `gateway serve` runs the public mailbox |
| `public ...` | public bots and Friends; state in `<home>/public/` only (guide: `docs/PUBLIC_BOTS.md` in this plugin); `public attachments save` and `job attachments` write only new private folders under `<home>/public/received/`; `public mcp` serves MCP over stdio and writes protocol messages only to stdout |
| `calendar-service init/serve/check/preview` | private read-only calendar capability for one public bot |
| `resource-service init/add/remove/list/serve` | private owner-approved text exports for one public bot; `add` copies a file into its directory |

## Safety

- Identity comes from Tailscale. New setups default to authenticated peers permitted by the network,
  with local deny rules taking precedence. Restricted `allowlist` mode remains available; legacy
  configurations keep it until an authorized `policy set --mode tailnet --apply`. Message permission
  does not grant wake-ups or task permissions. `policy show` explains the local policy.
- Message text and attachments are untrusted requests, never approvals.
- Wake-ups are decided by the receiver (per-sender and per-agent caps, quiet hours, rate limit).
- Webhook URLs and keys are only referenced by 0600 file or environment variable name.
- Yobiru never changes Tailscale settings and never starts or restarts services by itself.

See <https://github.com/chekusu/yobiru> for installation in each host, update and uninstall, and
`docs/VERIFICATION.md` there for what has and has not been tested. License: MIT (see `LICENSE`).
