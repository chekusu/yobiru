# Visible Claude Code wake bridge

Yobiru can dispatch authenticated inbox IDs into visible Claude Code background Remote Control sessions. This optional adapter uses `claude --bg --remote-control --session-id UUID`; it does not use headless `-p`, an SDK or a hidden API run. It requires a trusted working directory, a locally authenticated Claude CLI that supports those options and `claude agents --json --all` with `sessionId`, and a working Yobiru receiver. Current implementation supports macOS and Linux, Python 3.8+.

The `v0.7.15` release includes this bridge. Install its plugin archive only after verifying the matching `SHA256SUMS`; keep the native Claude account and Remote Control acceptance boundary separate from automated tests.

## Preview, install and start

Run `yobiru claude-bridge install --help`. The first install needs the Claude executable and a trusted working directory. Paths below are examples; use absolute local paths:

```sh
sh /absolute/plugin/scripts/yobiru --home /absolute/private/yobiru-data claude-bridge install \
  --claude-path /absolute/bin/claude --workdir /absolute/trusted/workdir
```

Preview does not write files, start services, query Claude authentication or launch Claude. Add `--apply` only for an authorized installation. The default target is `<data-home>/claude-visible`; `--bridge-home` selects a different private target. An existing different agent wake adapter, including one inherited from the default agent’s global configuration, is refused and left intact. In particular, this installer does not take over a custom `claude-local` bridge. Do not disable that bridge or clear its state as an installation shortcut.

The installer freezes a complete copy of the plugin under a content-addressed version directory, keeps an initialized persistent launch ledger, preserves local dispatcher instructions and writes backups before changing bridge/Yobiru configuration. Upgrades preserve launch ownership and all inbox data. It refuses a missing/corrupt existing ledger and a changed agent/executable/workdir binding. The default permission mode is `manual`; another supported mode must be deliberately selected. No arbitrary extra launch flags or permission-bypass mode is exposed.

The result lists a launchd plist and a Linux user systemd unit. These are templates; installation never starts a service. On macOS, an authorized operator may bootstrap the returned plist with `launchctl bootstrap gui/$(id -u) /absolute/path/to/returned.plist`. On Linux, place the returned unit in `~/.config/systemd/user/`, run `systemctl --user daemon-reload`, then enable/start its exact unit name. Run under the receiver owner's account and existing Claude login, never by copying credentials. The receiver must reload its wake-adapter config after this change using the normal receiver restart process. Coordinate any service restart with active work.

Status is read-only and does not run Claude:

```sh
sh /absolute/plugin/scripts/yobiru --home /absolute/private/yobiru-data claude-bridge status
```

Use `--config /absolute/path/config.json` for a custom bridge home. `step` performs one dispatch cycle and can launch a paid Claude session; `daemon` polls continuously. Both require task authorization. The generated receiver wake command only records a trigger and returns; the daemon reads the authenticated inbox. An installed template without a running daemon cannot wake Claude.

## Durable launch and recovery

1. Re-read the verified sender receipt and current receiver access policy, inbox destination, acknowledgement, effective wake and the request ledger. A revoked peer cannot wake Claude from an older queued message. Expired, final, missing or already running tracked requests are not dispatched.
2. Check local Claude authentication. Auth failures launch nothing and leave the inbox pending.
3. Persist and fsync a unique session UUID and its exact message IDs **before** invoking Claude.
4. Start a visible Remote Control session using that UUID, the frozen plugin and local dispatcher instructions.
5. Reconcile only a unique exact `sessionId` from `claude agents --json --all`. Exit zero or a printed short ID alone is insufficient.

A crash, timeout, nonzero exit or missing/ambiguous session observation leaves `intent`/`uncertain` ownership. Later cycles query that same UUID; they never issue a second launch. Some Claude Code versions give a `--bg` session its own UUID and ignore `--session-id`; then the run is matched by its launch name (`Yobiru AGENT ID8`), only when exactly one session not yet owned by another run carries that name and its `startedAt`, when listed, is not before the saved intent. The state records `reconciled_by` (`session_id` or `name`). An unresolved intent blocks additional dispatch until reconciled. Corrupt or missing bridge state fails closed, including after restart. There is one writer lock shared by the dispatcher and installer.

A `dispatched` record means the exact visible session was observed, not that the message was handled. The session checks request state again immediately before work; lifecycle changes can occur after bridge preflight. Completion remains in the Yobiru request ledger. Finished sessions and unacknowledged messages are never automatically reaped or retried. The adapter never invokes `claude stop`, deletes a session or resets a queue.

If an intent cannot be reconciled, preserve `state.json`, receiver records and Claude session evidence. Stop this dispatcher before an operator investigates the exact UUID; absence from one listing is not proof a launch did not occur. Resolve or complete the original task with explicit owner direction. There is deliberately no blind retry/reset command.

Configuration backups contain the old bridge and receiver configuration, **not a replaceable launch ledger**. To roll back, stop the bridge, compare the backup with current configuration, restore only reviewed configuration while preserving `state.json` and inbox/requests, then restart the chosen version. Do not restore an old state file over newer launch ownership. Immutable plugin versions remain available for this purpose.

## Verification boundary

Automated tests use a fake Claude executable and synthetic inbox/ledger records to cover intent-before-launch, interrupted startup, auth failure, duplicate wake, timeout/nonzero outcomes, exact-session reconciliation, expired/final requests, corrupt-state refusal, writer exclusion, read-only status and installer preservation. They do not start a real Claude session, perform paid inference, or verify current Remote Control visibility on a user's account. Validate one authorized harmless request on that account before claiming native acceptance. Local CLI help from Claude Code 2.1.310 supplied the supported flags; future versions may differ.
