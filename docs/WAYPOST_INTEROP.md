# Waypost beside Yobiru: durable mail, direct delivery

**Status:** opt-in text bridge. Neither protocol nor Yobiru's default delivery path changes. Attachments and remote task-lifecycle synchronization are outside this implementation.

[简体中文](WAYPOST_INTEROP.zh-CN.md)

Yobiru delivers directly between machines and handles wake policy. [Waypost](https://github.com/mainline-org/waypost) keeps a durable mailbox and versioned source files for assistants over HTTP/MCP. Use them together when a report should survive an offline machine or be collected by a primary assistant. Either works alone.

| Need | Use |
|---|---|
| Direct delivery, wake, quiet hours, machine policy | Yobiru |
| Durable reports, a primary assistant's inbox, offline reading | Waypost |
| Files fetched from a peer | Yobiru `fetch` |
| Files cited at a fixed version | Waypost blobs and memory revisions, separately authorized |

The bridge runs on a machine the operator trusts with plaintext. It uses an ordinary, scoped Waypost assistant connection. It never needs a user login credential or deployment administrator key.

## What the text bridge does

- Imports a received Yobiru text record into a configured Waypost recipient's inbox.
- Sends a Waypost message from an explicitly mapped producer to a configured Yobiru peer/agent as a `notice`, with `wake: none`.
- Journals payloads and forwarding state locally before side effects. Restarts resume confirmed operations and replay stable Waypost requests/ACKs.
- Refuses unverified sources, unknown routes, attachments and changes to an existing journal's identity/routing binding. Refusal leaves the item unresolved.
- Reports IDs and counts, without printing message bodies or credentials.

This is a forwarder, not an executor. Received text does not approve an operation. A notification does not prove that a remote assistant read or acted on it.

## Configure one bridge for one Waypost space

This bridge currently supports macOS/Linux (POSIX), with Python 3.8+ and the existing Yobiru launcher. Windows locking and private-file checks are not implemented. On the bridge machine, create a Waypost assistant with `message.send`, `message.read` and `receipt.write`. Grant only the incoming routes' recipient Actor IDs. The bridge reads its own Consumer; it does not read other assistants' inboxes.

Save its assistant bearer token in a private file (mode `0600` on POSIX). Keep the Yobiru data home and journal private. Do not use a service administrator credential.

Create `routes.json`, replacing the example Actor IDs with the actual IDs from Waypost:

```json
{
  "incoming": {
    "alpha.example.ts.net/grok": "actor_primary"
  },
  "outgoing": {
    "actor_primary": "alpha:7766/grok"
  }
}
```

`incoming` keys use the **full authenticated node name** and the sender's `from_agent`. Configure the sender's `self_agent`, or send with `--as`. `outgoing` keys are the Waypost producer Actor IDs the bridge may forward. Values are Yobiru addresses. Message content cannot choose an address or grant access.

Names such as `alpha/grok` are display/routing labels. They are never Waypost Actor IDs. Short Yobiru destinations are resolved by Yobiru's normal current-peer lookup; ambiguous or offline peers are not silently replaced.

Start with a read-only plan, then run once:

```sh
python3 tools/waypost_bridge.py \
  --yobiru /path/to/yobiru/scripts/yobiru \
  --home /path/to/private/yobiru-home \
  --waypost-url https://your-waypost.example \
  --token-file /path/to/private/bridge-token \
  --map /path/to/private/routes.json \
  --state /path/to/private/bridge.sqlite3 \
  --once --dry-run
```

Remove `--dry-run` to forward. Remove `--once` to poll continuously. Dry run may authenticate and read the Waypost inbox, but does not send, acknowledge or create the journal. Keep the journal on restart; a corrupted or incompatible journal is an error, not permission to start over.

Use HTTPS for a remote Waypost. Plain HTTP is restricted to loopback for local tests. Redirects are refused to keep bearer tokens at the configured service.

## Identity: trust the receipt, not the envelope's claim

Yobiru stores `{"envelope": ..., "received": {"peer": {"node": ...}}}`. The source node comes only from `received.peer.node`, recorded by the authenticated receiver. Missing verification or a contradictory sender is rejected; there is no fallback to the envelope's `from`.

Agent names route inside that verified machine. Another process with the same machine authority can claim a different `from_agent`; these names are not a security boundary. Do not place independent tenants behind that assumption.

The Waypost producer is always the bridge's own token-bound Actor. Authenticated Yobiru provenance is carried in the body. The bridge neither mints identities from inbound names nor impersonates another Waypost Actor.

## Recovery and receipt meanings

For Yobiru → Waypost, the write key is derived from the space, authenticated peer, agent and envelope ID. The payload is journaled first. If Waypost commits but its response is lost, retrying uses the same key and payload, preserving one published message.

For Waypost → Yobiru, the bridge fetches the actual message by the feed item's `resource_id`, persists the feed item and message, and then writes `received`. A confirmed Yobiru response must have the matching ID, `ok: true` and HTTP 200/202. Only then does the bridge write `processed`. If the ACK response is lost, it retries the ACK without sending the message again.

Here, `processed` means **this bridge finished forwarding**. It does not mean that the Yobiru recipient completed the underlying task. Tracked task outcomes (`running`, `completed`, `failed`, `rejected`, `blocked`, `timed_out`, `late`) are not synchronized by this version. In particular, timeout never becomes a successful outcome.

Yobiru's ordinary `send` generates a fresh envelope ID each invocation. Therefore, after an uncertain result or a crash during an attempt, the bridge marks delivery **unknown** and blocks automatic resending and `processed`. Check the receiving machine's inbox and actual effects before an operator resolves it. Do not delete the journal or issue another send merely to clear the error. This avoids blind duplicates; it does not provide exactly-once remote execution.

After verifying the exact receiver record, repeat the same connection arguments with `--confirm-sent ITEM_ID YOBIRU_ID` to finish ACKs without resending. Only after verifying that no delivery or effects occurred, `--retry-unsent ITEM_ID` may authorize a later run to retry. Neither operation automatically dispatches other pending messages.

The journal binds the Waypost space, Actor, Consumer and routing configuration. Use separate private journals for separate bridges. A second process cannot dispatch concurrently with the first.

## Correlation stays inside message bodies

Yobiru rejects unknown envelope fields. Correlation remains inside JSON text, never a new top-level wire field. Original Yobiru IDs and reply links stay available in the imported body; a reverse-forwarded notice includes its Waypost message identity. Correlation is evidence for joining records, not authority to redirect or execute a message.

## Limits and unsupported data

A record containing Yobiru `fetch` entries or Waypost attachments is refused as a whole. No successful receipt is written for an omitted attachment. Blob uploads, file verification and versioned memory need their own explicit grants and a future file-transfer path.

There is no supervisor installation, hosted relay, group chat, remote task approval, automatic primary-assistant setup or multi-tenant routing. Size limits and privacy filtering are enforced by the connected Waypost instance; oversized content fails rather than being truncated.

## Validation

Run the mapping/security tests and packaging checks:

```sh
python3 -m unittest discover -s tests -p 'test_waypost_bridge*.py' -v
python3 tools/build_plugin.py check
```

For real local interoperability, first build [Waypost](https://github.com/mainline-org/waypost) with `cargo build --locked`, then provide that binary:

```sh
WAYPOST_TEST_BINARY=/path/to/waypost/target/debug/waypost \
  python3 -m unittest discover -s tests -p test_waypost_bridge_e2e.py -v
```

On 2026-10-16, all 22 bridge tests passed (14 mapping/security tests and 8 integration scenarios), along with the packaging check. The final broader `tests/` run executed 250 tests, with 35 skips and four failures on macOS; all four also reproduced on the unmodified PR base `1265921`: two fixtures assume `/bin/false`, one compares `/var` with `/private/var`, and one requires `ConnectionRefusedError` where this host returns a timeout. These are recorded separately from bridge results.

This starts a fresh encrypted Waypost service and the real packaged Yobiru CLI with two actual HTTP receivers. Only Tailscale identity lookup and the accepted loopback network range use Yobiru's existing test harness. It exercises dry run, bidirectional delivery, restart deduplication, receipts, response loss and rejection paths. It does not contact a real tailnet, use production data or test an external model.

The service contract follows Waypost's [HTTP routes](https://github.com/mainline-org/waypost/blob/926601dce1b5b4bad218c52c75880e6eb48d158d/src/http.rs), [identity response](https://github.com/mainline-org/waypost/blob/926601dce1b5b4bad218c52c75880e6eb48d158d/src/service/mod.rs) and [consumer guide](https://github.com/mainline-org/waypost/blob/926601dce1b5b4bad218c52c75880e6eb48d158d/docs/user-guide.md). Yobiru identity and envelope rules remain those in [SPEC](../SPEC.md).
