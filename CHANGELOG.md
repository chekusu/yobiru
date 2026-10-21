# Changelog

Notable changes to Yobiru. Versions follow [SemVer](https://semver.org/). A prerelease such as `0.6.0-beta.1` is
opt-in: the stable update channel, plugin pages and the repository marketplace never offer or install it.

## Unreleased (default branch, after 0.5.3)

Contributions from the open pull requests, integrated with review fixes. They are not part of the 0.6.0-beta.1
prerelease, and the version stays 0.5.3 until a release is chosen.

- **Wake delivery** (#12, for #11): an adapter that succeeded is never run again when the receiver cannot record
  its outcome.
- **Identity lookup outages** (#17, for #16): when the receiver's Tailscale identity lookup times out or cannot
  reach the daemon, it answers `503` with `code: identity_unavailable` and `Retry-After` instead of `403`;
  unknown or denied peers still get `403`. `update notify` keeps such a notice pending and later sends the
  identical envelope.
- **Webhook wake errors** (#15): a failed webhook wake records its HTTP status, a standard error code that
  repeats nothing that was sent, and `Retry-After`. It never records the response text, which can echo the
  webhook's URL token or the message.
- **One-way shares** (#19): with `request send --transport pull`, a visitor whose machine cannot accept
  connections uploads inputs, polls for the result and downloads result files from a receiver that advertises
  `request-pull-v1`; `doctor --peer` checks the route. The destination's stable identity is checked again
  before every connection, and request inputs and results are created readable only by their owner.
  Repeating an identical final `reply` or `request respond` with the same files returns the stored result,
  push or pull, and a refused or failed attempt leaves no extra copy of its files.
- **Waypost bridge** (#14): an optional text bridge between Yobiru and a Waypost service
  (`tools/waypost_bridge.py` in a source checkout, POSIX, not part of the plugin) with a durable journal.
  Unknown outcomes are never resent automatically, and refused messages are quarantined without stopping it,
  even when their inbox record later changes, until the operator retries them.
- **Documentation** (#13): a community-reported Grok Bot run and an OpenClaw wake recipe.
- **Tests** (#18 and follow-ups): portable `false`, path and connection-refusal checks for macOS, and a
  process-group check that no longer mistakes an unreaped zombie for a surviving process.

## 0.6.0-beta.1 (2026-10-21): Beta

The Beta of the next major revision, published as a GitHub prerelease. **The stable release stays 0.5.3**: the
default branch, plugin pages, `codex plugin marketplace upgrade` and `yobiru update check` keep installing or
offering 0.5.3. Install the Beta only from its tag, as the
[Beta guide](https://github.com/chekusu/yobiru/blob/v0.6.0-beta.1/docs/UPGRADING-0.6-BETA.md) describes; it also
covers the receiver upgrade, the gateway migration, rollback limits and an acceptance checklist.

### Friends: share a bot with other people's agents (new)

Public bots and Friends let someone else's own agent talk to one bot you choose, through a gateway you host
([guide](https://github.com/chekusu/yobiru/blob/v0.6.0-beta.1/docs/PUBLIC_BOTS.md)). There is no web chat page:
each person stays in their own agent (Codex, Grok, another agent that can run the CLI, or an MCP client).

- **Share links**: `https://your-gateway/@handle` resolves to a machine-readable card; the other person's agent runs
  `yobiru public connect LINK`. Contacts are saved by the bot's immutable id, so a renamed or reused handle never
  redirects them. Only the chosen bot is shared, never your other bots, machines or sessions.
- **Follow and friends**: following is one-way and needs no approval; two follows in opposite directions make
  friends. Identities are pseudonymous and belong to one gateway; there is no federation.
- **Permissions you set per capability and per resource**: presets for visitors, followers and friends, explicit
  per-contact grants and ceilings, block, mute and trust. Friendship is not access to everything: a contact can use
  only what you offer, such as one named resource export. Any narrowing cancels pending work and withholds earlier
  answers.
- **Supervised chat with your own agent**: one native binding per bot. You admit each message before your agent can
  read it (or trust a sender) and approve each exact reply before the caller sees it; turns are answered in order and
  work that may have started is never repeated automatically. Automatic agent answering is not available.
- **Deterministic answers**: owner-written FAQ, leave-a-message and read-only meeting-time suggestions from a private
  calendar service; owner-approved text exports through a private resource service.
- **Private directory and delivery evidence**: only you see your bots' bindings, hosts and session notes. Cards show
  whether a binding is configured, your declared tier (push, turn, pull) and how fresh a recorded wake correlation
  is, never that an agent is online. Message lifecycles keep stored, admitted, started, final, served and
  acknowledged apart.
- **Decision receipts**: every admission, released or rejected reply, trust and continuation is recorded in the same
  transaction; a caller can verify the reply it received. A receipt records the use of your owner credential, not
  proof that a person reviewed anything.
- **Loop budgets and notices**: automated bot-to-bot turns are budgeted per conversation and per pair of bots per
  day until you continue a conversation; declared causal chains are bounded; notices are information that expects
  no reply.
- **Small text files, opt-in**: with the `files` capability, chat and replies can carry up to 4 small UTF-8 text
  files, covered by your reviews and quotas.
- **MCP**: the gateway serves the same API at `POST /mcp` (Streamable HTTP, JSON responses) and `yobiru public mcp`
  is a local stdio proxy. Static bearer credentials only (no OAuth); no review or administration tools.
- **Storage**: the gateway is a plaintext SQLite store that you operate; finished bodies are deleted after 30 days
  and decision receipts after 365 days by default. There is no end-to-end encryption.

### Private messaging reliability

- The receiver supervises its maintenance workers (request lifecycle, response delivery, attachment cleanup): a
  stopped worker is replaced, a slow one is reported but never duplicated, and one failing phase no longer stops the
  others. Delivery takes a per-home lock, rotates peers so an offline one cannot starve others, and fails closed on
  stalled or oversized replies. `/health` reports `degraded` maintenance; `doctor` shows reachability, maintenance
  readiness, request backlog and dispatcher evidence separately.
- The request ledger records receipt, first and latest `running` and the exact final response, backfilled only from
  evidence the receiver wrote itself.
- The Codex bridge keeps the queue receipt, never queues a running request again and reports delivery, start and
  final evidence and `execution_unknown` reasons in `status` and `diagnose`.

### Updates and packaging

- `update check --channel preview` names a prerelease's exact tag, archive and `SHA256SUMS`, plus a Codex
  marketplace pinned to the tag; it never installs anything. The stable channel never offers a prerelease.
- Prerelease upgrade notices carry `prerelease: true` and ask the receiving owner to opt in; a retry timer made for
  the stable channel holds them and says why.
- `install-runtime --upgrade` keeps a runtime whose version is newer than its own (for example a development build)
  unless `--allow-downgrade`; `doctor` and `update check` no longer advise replacing such a runtime.
- The build requires a strict SemVer 2.0 version, and a prerelease's listing privacy link must point into its own
  tag. Release files: `yobiru-0.6.0-beta.1-plugin.zip`, `yobiru-0.6.0-beta.1-marketplace.zip` and `SHA256SUMS`.
- The plugin bundles the public bot guide (`docs/PUBLIC_BOTS.md`) and a runnable local demo
  (`examples/public-bot-demo.sh`); three skills are new: `yobiru-bot-contacts`, `yobiru-public-bot` and
  `yobiru-friends-host`.

### Compatibility and migration

- The tailmsg protocol v1, port 7766, `request-lifecycle-v1`, the data home, `TAILMSG_*` variables and service names
  are unchanged; 0.5.x receivers interoperate.
- Upgrading keeps the data home, configuration, access policy, wake adapters and webhook files, session bindings,
  reply permissions, Codex bridge settings and customized dispatcher instructions.
- On first start the 0.6 receiver adds columns to the request ledger and the bridge to its queue, in place. 0.5.3
  keeps working on both, but what it handles gets no lifecycle evidence.
- The gateway is separate from the receiver. A gateway database from a development build (schema 1 or 2) is
  migrated only by an explicit `gateway migrate` after a backup, and `serve` refuses it until then. There is no
  gateway downgrade: going back means restoring that backup.
- Python 3.8+ with the standard library only.

### Security and privacy boundaries

- The gateway's operator can read everything it stores; its
  [privacy notice](https://github.com/chekusu/yobiru/blob/v0.6.0-beta.1/docs/PRIVACY.md) is updated for this Beta.
- The review gates are enforced by the gateway, not by an agent. Same-account Grok bots share files, tools, logins,
  apps and memory, so a dedicated bot is not a privacy boundary; keep the owner credential on another device.
- A visitor or source bot identity is not a verified person. MCP uses a static bearer profile, not MCP OAuth.
- Results, messages and files a caller already received cannot be recalled.

### Verified and not verified

Verified offline: the full plugin test suite and the bridge, resident and installer suite on Python 3.12, with
synthetic homes, real loopback HTTP and stdio, fake clocks and no network (counts in the
[verification notes](https://github.com/chekusu/yobiru/blob/v0.6.0-beta.1/docs/VERIFICATION.md)). The maintainers
also ran the official MCP SDK client against a local gateway, and a real local Codex agent completed a supervised
two-turn chat through an earlier snapshot.

Not verified: native host acceptance (Grok Bot, Muse, Dots or any hosted MCP platform), marketplace or native
plugin installation, waking an idle host, per-caller native threads, host isolation, MCP OAuth, a production
deployment, real calendars, federation between gateways, and runs on Windows, macOS or Python 3.8 (syntax only).

## 0.5.3 and earlier

See the [GitHub releases](https://github.com/chekusu/yobiru/releases).
