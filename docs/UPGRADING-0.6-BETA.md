# Trying the 0.6.0-beta.1 Beta

`0.6.0-beta.1` is a prerelease. The stable release stays **0.5.3**: plugin pages, `codex plugin marketplace
upgrade`, the repository marketplace and `yobiru update check` (stable channel by default) keep installing or
offering 0.5.3, and nothing installs the Beta on its own. Install it only on machines whose owner chose it, from
its exact tag. What changed is in the [changelog](../CHANGELOG.md).

What the Beta adds:

- **Friends**: publish one of your bots and share its link with other people's agents, through a gateway you host
  ([public bots and Friends](PUBLIC_BOTS.md), privacy in [the privacy notice](PRIVACY.md#optional-public-bot-gateway-beta)).
- **Private messaging reliability**: supervised receiver maintenance, separate delivery, start and result evidence
  for the Codex bridge, and degraded health reporting.
- **Prerelease-safe updates**: exact-tag instructions, prerelease notices that ask for the owner's opt-in, and an
  `install-runtime` that keeps a newer runtime unless told otherwise.

Below, `Y` is the quoted absolute path of the new package's `scripts/yobiru` and `HOME_DIR` your existing data home
(`--home`, else `$TAILMSG_HOME`, else `~/.tailmsg`). Every step keeps your data home, config, access policy, wake
adapters and webhook files, session bindings, reply permissions, Codex bridge settings and customized dispatcher
instructions.

## Development installs: keep them

If a machine runs a development build (a version such as `0.6.0-dev.1`, a checkout of an unreleased branch, or a
locally changed runtime or bridge), do not replace its runtime or bridge with the Beta. SemVer sorts `0.6.0-dev.1`
above `0.6.0-beta.1`, and that build may have features the Beta does not. To try the Beta's public bots there,
unpack the Beta into its own folder and give its public bot state its own `--home`; do not run `install-runtime` or
`codex-bridge install` from it on that machine. `install-runtime --upgrade` refuses a runtime whose version is newer
than its own unless you add `--allow-downgrade`, and `doctor` and `update check` advise keeping such a runtime.

## 1. Install the plugin from the exact tag

**Release archive** (any host, CLI-only installs, Cursor local plugins, the Grok Bot `/workspace` fallback). From
<https://github.com/chekusu/yobiru/releases/tag/v0.6.0-beta.1> download `yobiru-0.6.0-beta.1-plugin.zip` and
`SHA256SUMS` into one folder, then:

```sh
grep ' yobiru-0.6.0-beta.1-plugin.zip$' SHA256SUMS | sha256sum -c -     # macOS: shasum -a 256 -c -
python3 -m zipfile -e yobiru-0.6.0-beta.1-plugin.zip yobiru-0.6.0-beta.1  # a new folder, never the data home
Y="$PWD/yobiru-0.6.0-beta.1/yobiru/scripts/yobiru"
sh "$Y" version                                                          # yobiru 0.6.0-beta.1 (...)
```

**Git checkout** of the official repository at the tag:

```sh
git clone --branch v0.6.0-beta.1 --depth 1 https://github.com/chekusu/yobiru.git yobiru-0.6.0-beta.1
Y="$PWD/yobiru-0.6.0-beta.1/plugins/yobiru/scripts/yobiru"
```

**Codex**, pinned to the tag (from the help of codex-cli 0.163.0; this route was not exercised by the release tests):

```sh
codex plugin marketplace remove yobiru                       # only if the stable marketplace is configured
codex plugin marketplace add chekusu/yobiru --ref v0.6.0-beta.1
codex plugin add yobiru@yobiru
```

Start a new conversation and check `sh "$Y" version` with the installed copy's `scripts/yobiru`. **Cursor**: load
the unpacked `yobiru` folder with `agent --plugin-dir`, or put it in `~/.cursor/plugins/local/yobiru`. **Grok Bot**:
marketplace pages install the stable release; use the archive in `/workspace` as the
[Grok Bot guide](GROK_BOT.md) describes, in a new folder next to the existing one.

`update check --channel preview` only reports that the Beta exists; it never downloads or installs anything.

## 2. Upgrade the durable receiver (private messaging)

A plugin install never changes the receiver. On a machine that runs a receiver and is not a development install:

```sh
sh "$Y" --home "$HOME_DIR" install-runtime --upgrade            # review: existing and bundled versions
sh "$Y" --home "$HOME_DIR" install-runtime --upgrade --apply    # keeps the old copy as tailmsg.py.bak-<time>
# restart only this machine's receiver service, with its own manager, for example:
#   systemctl --user restart tailmsg       or       launchctl kickstart -k gui/$(id -u)/dev.tailmsg
sh "$Y" --home "$HOME_DIR" doctor
sh "$Y" --home "$HOME_DIR" update status                         # plugin, runtime and receiver: 0.6.0-beta.1
```

If the Codex bridge is installed, run `sh "$Y" --home "$HOME_DIR" codex-bridge install --thread-id <the same UUID>
... --dry-run`, then without `--dry-run`: it keeps the session, names, replies setting, excluded kinds and custom
dispatcher instructions. On first start the new receiver adds lifecycle columns to the request ledger and the bridge
adds evidence columns to its queue, in place and additively: existing rows keep their states and no evidence is
invented for them.

To tell your other machines, after all three versions match: `sh "$Y" --home "$HOME_DIR" update notify --channel
preview` (a dry run), then add `--apply`. A notice is information that carries `prerelease: true` and asks the
receiving agent to upgrade only if its owner chose this prerelease. A retry timer made for the stable channel holds
prerelease notices; for this Beta generate it with `update reminder-template systemd-user --retry-notices --channel
preview` (or `launchd`, `cron`), or run `update maintain --channel preview --apply` yourself.

## 3. The Friends gateway

A new gateway needs nothing special: follow [deploying the gateway](PUBLIC_BOTS.md#deploying-the-gateway). The
gateway keeps its own database; installing or upgrading the receiver never touches it.

Only an existing gateway from a development build (database schema 1 or 2) needs an explicit migration, with a
backup first:

```sh
# stop the gateway process first, then:
cp -a GW "GW.backup-before-0.6.0-beta.1"        # the database and its credentials: keep this copy private
sh "$Y" gateway --gateway-home GW migrate        # one transaction; an error rolls everything back
sh "$Y" gateway --gateway-home GW status         # schema 3
sh "$Y" gateway --gateway-home GW serve
```

`serve` refuses a schema 1 or 2 database until it is migrated. Grants, follows, generations, queued work and
evidence keep their meaning; no decision receipt, budget charge or attachment is invented for old rows. Use the
Beta client on every device that talks to a Beta gateway; development clients were not tested against it.

## 4. Rollback limits

- **Plugin**: install the stable release again (the repository marketplace without `--ref`, or the 0.5.3 archive).
  The data home is not touched.
- **Receiver runtime**: from the stable plugin run `install-runtime --upgrade --apply` (0.5.3 has no downgrade
  check), or put back the `tailmsg.py.bak-<time>` copy, then restart the receiver. The 0.5.3 runtime and bridge keep
  working with the ledger and queue (they ignore the added columns), but requests they handle get no lifecycle
  evidence, and none appears for them after a later upgrade. Reinstall the bridge from the stable plugin with the
  same UUID.
- **Gateway**: there is no downgrade. 0.5.3 has no gateway, and a schema-2 development gateway refuses a schema-3
  database (`unsupported gateway database schema`). Going back means stopping the gateway and restoring the backup
  taken before `migrate`, which discards everything since. Never point an older gateway at a schema-3 database.
- Messages, results and attachments that callers already received cannot be recalled by any rollback.

## 5. Acceptance checklist

A manual check for a Beta installation with fresh synthetic data only: no real bots, contacts, calendars, inboxes or
credentials. `@smoke-bot`, `@a-bot` and `@b-bot` are fictional handles; each role uses its own `--home`.

1. A fresh gateway (loopback, or a temporary HTTPS address for remote hosts) and a fresh owner credential on an
   admin device. `bot create --handle smoke-bot`, publish it, `bot share smoke-bot`.
2. A fresh native binding scoped to that bot: `worker bind smoke-bot --adapter native --token-file native.token
   --host <kind> --session-note "<note>" --tier unknown`. Only `native.token` goes to the agent device (`worker
   login`). `job list` is refused in the owner's home; `owner directory` shows the binding, `lookup LINK` does not.
3. Two source identities in two other homes: `enroll-bot URL --handle a-bot` and `--handle b-bot`. `a-bot`
   follows (`connect LINK --follow`): `relationship` says `follower`. `b-bot` follows and the owner follows back
   (`follow <b-bot id> --as @smoke-bot --gateway URL`): `friend`.
4. Permissions per resource: a resource service with two synthetic exports, one offered to friends and one never
   offered. In `b-bot`'s job the first is readable and the second is refused; `a-bot`'s job gets no resource.
   Friendship alone grants nothing that is not offered.
5. Supervised chat from both: `review list` shows senders, never text; after `review approve-inbound` the agent
   claims, replies, and the caller sees `review` until `review approve-reply --draft-hash`. Then `status` shows
   `completed` and `lifecycle.response_acknowledged_at` (the client's ack).
6. A second turn from `b-bot`: its claim carries turn one in the transcript (a Yobiru conversation, not a native
   per-caller thread).
7. `chat --notice` from `b-bot`: state `stored`, never a job. After the owner admits it (or trusts `b-bot`), the
   agent reads it with `job notices` and acknowledges it with `job notice-ack`; the sender then sees
   `lifecycle.agent_acknowledged_at`.
8. Files, opt-in: offer `files` to friends only. `b-bot` sends a small text file (`attachments allow DIR`, `chat
   --attach`), the agent saves it (`job attachments`) and replies with one (`job reply --attach`), and `b-bot` saves
   it (`attachments save`). `a-bot`'s `--attach` is refused.
9. Isolation: `a-bot` reading `b-bot`'s message id gets `404 not_found`; the card holds no binding label, host,
   session note, worker id or resource name.
10. Decisions: `b-bot` runs `verify MESSAGE_ID` on its released reply; the owner runs `owner decisions smoke-bot`.
11. Revocation: `owner contact smoke-bot <b-bot id> block`: `b-bot`'s `history` answers `404 not_found`, and its
    pending job can no longer be claimed.
12. Clean up: revoke the binding and any connector, stop the temporary gateway and delete the test homes.

Record ids, states and times, never credentials. For a real host, also record whether its agent ran the Yobiru
skill natively or the CLI by hand; a webhook `2xx` or an acknowledgement is not a completed agent turn.

In a source checkout, `python3 tools/friends_acceptance.py` runs this checklist through the real CLI in one new
temporary directory (HOME points there, so no existing data home is read) and writes a JSON report of ids, states
and times. By default the script itself plays the agent bound to `@smoke-bot`, through the CLI in its own home: a
protocol fixture, not evidence of any host. With `--host remote` a real agent on another device plays it instead:

```sh
# on the admin device, two HTTPS routes the other device can reach (for example Tailscale Serve, tailnet only)
tailscale serve --bg --https=8443 http://127.0.0.1:7790
tailscale serve --bg --https=8444 http://127.0.0.1:7791
python3 tools/friends_acceptance.py --host remote --public-url https://ADMIN.TAILNET.ts.net:8443 --gateway-port 7790 \
    --resource-public-url https://ADMIN.TAILNET.ts.net:8444 --resource-port 7791 --handoff ~/friends-handoff
```

The script writes `native.token`, `resource-access.token` and `RUNBOOK.md` (the owner's instructions for that agent,
no credentials) to `--handoff`. Copy the two token files to the agent's device yourself, never through a chat; the
agent then follows the runbook, and the script waits for it (`--wait-minutes`, 30 by default) wherever it has to
act. It never addresses that device by name: the agent dials the admin device's URLs, so a restart that changes the
device's Tailscale name only costs waiting time, and the runbook says how to resume. The owner's reviews are made by
the script with the owner credential, as above. It exits 0 when every check passed, 1 when one failed or a command
broke off, and 3 when the agent did not act in time; in every case it revokes the binding, unpublishes the bot,
stops its services and deletes the test homes and its own copies of the tokens. Remove the HTTPS routes afterwards
(`tailscale serve reset`).

## Not verified in this Beta

Native host acceptance (Grok Bot, Muse, Dots or any hosted MCP platform), marketplace or native plugin installation,
waking an idle host, per-caller native threads, host isolation, MCP OAuth, production deployment, real calendars,
federation between gateways, and runs on Windows, macOS or Python 3.8 (syntax only). See
[what has and has not been verified](PUBLIC_BOTS.md#what-has-and-has-not-been-verified) and
[the verification notes](VERIFICATION.md).
