# Codex dispatcher: minimum sandbox access

The receiver and bridge run as user services; the dispatcher processes notifications inside a
Codex session. A working receiver, an `enqueued` bridge record, and a writable Codex project do not
mean that the dispatcher can update the message files outside that project.

For a Linux app-server using `sandbox_mode="workspace-write"`, configure the **dispatcher session's
effective sandbox**, not the bridge service. `approval_policy="never"` does not grant access: denied
operations fail without an approval prompt. The bridge does not change these settings.

## Which paths need writes?

Use the absolute `tailmsg_home` and `bridge_home` from the installation's `codex-bridge.json` (also
included in each trusted bridge notification). `bridge_home` can be outside the data home.

| Operation | Additional writable directory | Network access |
|---|---|---|
| `ack ID` | `<tailmsg_home>/inbox` | No |
| Dispatcher deduplication and handoff records | `<bridge_home>/handled` | No |
| Tracked `request respond ID TEXT` / `reply ID TEXT` | `<tailmsg_home>/requests` | No; the receiver sends the response or serves it to a polling pull requester |
| Tracked `reply ID TEXT --file PATH` | `<tailmsg_home>/requests`, `<tailmsg_home>/outbox` | No; the receiver serves the files |
| Legacy text-only `reply ID TEXT` | None beyond the handled records used to track the reply | Yes |
| Legacy `reply ID TEXT --file PATH` | `<tailmsg_home>/outbox` | Yes |
| `fetch ID` for an incoming pull request | Existing `<tailmsg_home>/inbox/ID/files` | No; validates already uploaded local files |
| `fetch ID` for ordinary messages or response attachments | `<tailmsg_home>/inbox` | Yes |

`ack` writes a temporary file beside `inbox/<id>.json` and atomically replaces that JSON, so giving
write access to only the existing file is insufficient. Attachment replies snapshot files under
`outbox/<token>/files/` and write `meta.json`. Fetches use `inbox/<id>/files/`, including temporary
`.part` files. Ordinary text replies do not persist an outbox entry; the dispatcher must record the
send result in its handled record to avoid repeated replies. Tracked responses instead persist the
result and an immutable retry envelope in `requests/ledger.sqlite3`. SQLite needs write access to
the containing directory for its journal. Local `request status` reads this ledger; `--refresh`
also needs network access and writes verified remote responses to it.

For one-way pull requests, the input manifest is `request.uploads`. The receiver verifies and places
these files under `inbox/ID/files/` before notifying the dispatcher. A local `fetch ID` verifies their
size and SHA-256 without contacting the requester. A missing or modified input is an error, not a
reason to enable networking or download a replacement. Authorized `request respond ... --file PATH`
stores the result for the requester to poll and download, with no reverse connection. This applies
even when `received.peer.node` is a name but the envelope's sender uses a stable `node:` identity;
the bridge uses the receiver's authenticated identity-matching rules.

Keep `config.json`, `codex-bridge.json`, the installed code and workflow, and the bridge queue
database read-only to the dispatcher. Do **not** add the entire data home or bridge state directory
as a writable root: that would include access policy, `allow_replies`, or the service's queue.
Do not use the data home, its parent, or your home directory as the dispatcher's working directory;
the workspace itself is writable independently of the extra roots. A data home inside any other
already-writable root is similarly unprotected. Review resolved paths and symlinks.

## Prepare the directories outside the sandbox

After normal Yobiru setup and bridge installation, run this in a trusted local setup terminal,
using your actual paths. These are example defaults; read your bridge configuration first:

```sh
YOBIRU_DATA="$HOME/.tailmsg"
YOBIRU_BRIDGE="$YOBIRU_DATA/codex-bridge"
test -f "$YOBIRU_DATA/config.json" && test -f "$YOBIRU_DATA/codex-bridge.json" || exit 1
umask 077
mkdir -p "$YOBIRU_DATA/inbox" "$YOBIRU_DATA/outbox" "$YOBIRU_DATA/requests" "$YOBIRU_BRIDGE/handled"
mkdir -p "$HOME/yobiru-dispatcher"
```

Pre-create **all four** directories, even when outbox will remain read-only. The Tailmsg CLI
initializes its home, inbox and outbox on every invocation and creates `config.json` if missing.
An established installation must already have that configuration; do not widen access just to let
a dispatcher create it. The handled directory belongs to the workflow and is not created by the
bridge installer. Existing directory modes are not changed by `mkdir -p`.

## Configure the app-server and existing dispatcher

For an owner-managed app-server, this example supplies defaults for a local dispatcher that only
processes and acknowledges messages. Replace `/home/you` and all paths with **absolute, resolved
paths** for your installation. Do not put `~` or `$HOME` inside the TOML array and expect shell
expansion. Keep your existing account, model, socket and thread UUID.

```sh
cd /home/you/yobiru-dispatcher
codex app-server --listen unix:///home/you/.codex/yobiru.sock \
  -c 'sandbox_mode="workspace-write"' \
  -c 'approval_policy="never"' \
  -c 'sandbox_workspace_write.writable_roots=["/home/you/.tailmsg/inbox","/home/you/.tailmsg/requests","/home/you/.tailmsg/codex-bridge/handled"]' \
  -c 'sandbox_workspace_write.network_access=false'
```

Apply these options to the app-server's existing supervisor command; do not start a second server
on its socket. A loaded/resumed thread or host can have its own permission overrides: restarting the
server with new defaults is not proof that an existing session received them. Check the same
dispatcher's effective workspace, writable roots and network policy through the host, then resume
that exact UUID as appropriate. Do not create a replacement thread or point the bridge at `--last`.
Hosts and managed policies may impose additional restrictions.

Only if the owner has authorized automatic replies, preserve/enable `allow_replies` through the
bridge's normal installation options. Tracked responses need only local ledger writes: the receiver
delivers them or serves them to the pull requester. Legacy direct replies and ordinary/response
attachment downloads also need network access:

```text
-c 'sandbox_workspace_write.network_access=true'
```

For authorized replies **with attachments**, also replace the writable-roots option with:

```text
-c 'sandbox_workspace_write.writable_roots=["/home/you/.tailmsg/inbox","/home/you/.tailmsg/requests","/home/you/.tailmsg/codex-bridge/handled","/home/you/.tailmsg/outbox"]'
```

Network access is also needed for authorized attachment downloads (`fetch`), except local validation
of already uploaded pull request inputs. It is not required for `ack` or handled records. This Codex
network switch is broader than a per-command or tailnet-only
grant; keep it off when the workflow does not need networking. `allow_replies=false` must still mean
**no agent-generated business reply**, even if networking is available for another task. For tracked
requests the trusted bridge may persist a fixed `blocked` protocol result without starting the model;
the receiver delivers minimal receipt/status metadata. Network availability alone
never authorizes sending. Normal Tailscale CLI/daemon access and peer network policy must also work;
do not work around a host denial by running the dispatcher with unrestricted permissions.

The option names follow the [official Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).
Check `codex app-server --help` for your installed CLI's supported flags.

## Verify in the actual dispatcher

Before delegating a request, confirm that the configured handled directory is writable. A local
probe can create, replace and remove a uniquely named temporary file in each required directory,
without touching existing messages. Confirm through the host's effective permissions that policy,
runtime and queue files remain outside writable roots; do not test this by overwriting them.

Then use an authorized test message: inspect its contents, persist its handled record in the exact
configured directory, complete the bounded work and only then run:

```sh
python3 /home/you/.tailmsg/tailmsg.py --home /home/you/.tailmsg request respond MESSAGE_ID 'Verified result' --state completed
python3 /home/you/.tailmsg/tailmsg.py --home /home/you/.tailmsg ack MESSAGE_ID
python3 /home/you/.tailmsg/tailmsg.py --home /home/you/.tailmsg show MESSAGE_ID
```

Verify `acked_at` and the handled record. If replies are authorized, test a reply separately and
check the receiving agent; HTTP 202 means stored, not processed. If a write fails, keep the message
pending and report the exact denied path and current sandbox policy. Do not put handled records in
an alternative project directory, silently skip them, grant yourself access, or repeat a dispatched
task. If work already completed and only `ack` failed, reconcile the existing handled record before
retrying the acknowledgment. Bridge `status` does not verify any of these dispatcher permissions.
