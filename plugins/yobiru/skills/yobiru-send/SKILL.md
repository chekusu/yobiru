---
name: yobiru-send
description: Send Yobiru messages, data and files to another machine's agent; make tracked requests with receipts, deadlines and final results; query their status; or reply. Use for cross-agent collaboration, requests, notifications and checking why a requested result has not arrived.
---

# Yobiru: send, reply, data and files

## Run the Yobiru command

The command ships in this plugin: `scripts/yobiru`, two folders above this file
(`<folder of this SKILL.md>/../../scripts/yobiru`). Use its quoted absolute path with `sh`; below it is
written `Y`, so a real command looks like `sh "/abs/path/yobiru/scripts/yobiru" send ...`. Windows:
`py -3 "<plugin>\scripts\yobiru.py"`. If the file is missing or `send` says "not set up", use the
yobiru-setup skill first. A non-default data home goes before the command: `sh Y --home DIR send ...`.

In Claude Code the plugin's `yobiru` MCP server offers `yobiru_send` (with `track: true` for a tracked
request), `yobiru_reply`, `yobiru_request_status` and `yobiru_agents` for the default data home; the rules
below apply to them unchanged.

## Before sending

- Send what the user asked for, or what a collaboration the user set up needs: follow-ups, answers
  and results for the agent they asked you to work with, or anything their standing instructions
  cover. Ask only when the recipient, content or scope is unclear, or a send would go beyond that
  authorization. A request inside a received message never extends it by itself.
- Never include secrets, credentials, or private content from other conversations or files the user
  did not point to.
- Private Yobiru messaging has no central relay or storage service. A message goes to the receiving machine over
  Tailscale (which may use encrypted relays). Ordinary attachments stay on the sender until fetched;
  pull requests upload their input files to the target. The recipient must run a receiver that
  allows this machine.

## Addresses

This skill covers machines on the user's tailnet. To reach someone's published bot by an address
such as `https://gateway.example/@assistant`, use the yobiru-bot-contacts skill instead.

To see which machines and agents are online and which session handles each, run `sh Y agents --online`
(yobiru-sessions skill).


- `HOST`: Tailscale hostname, MagicDNS name or Tailscale IP; add `:PORT` if it is not 7766.
- `HOST/AGENT`: deliver to one named agent on that machine and wake only that one, for example
  `dev-server/codex`. Same as `send HOST --agent AGENT`.
- Receivers older than agent addressing reject named agents (HTTP 400). Resend without the agent only
  if the user agrees.

Prefer a stable short hostname for new messages instead of copying an IP from an earlier message.
Each send reads the current `tailscale status --json`: a short name matches the DNS short name or
reported HostName, and a unique online match wins over old offline entries after a reinstall.
Multiple online matches (or multiple offline matches with none online) are ambiguous: use
`agents --json` and the intended exact MagicDNS name; never pick by list order or strip numeric suffixes.
Full MagicDNS names and explicit IPs stay exact. A session display alias is not a machine address.
If the rebuilt host lost its name too, it must restore its chosen Tailscale hostname or be selected
explicitly. Name resolution cannot reinstall Tailscale, sign in, or start the receiver.
Replies, old attachment grants and queued upgrade notices remain bound to their original identities;
do not silently redirect them through a similar name.

## Commands

    sh Y send dev-server/codex "Nightly build failed on main; log attached" --kind notice --file ./build.log --wake now
    sh Y request send dev-server/codex "Investigate the build and report the result." --timeout-seconds 900 --wake now
    sh Y request send shared-server/codex "Read input.txt and return a result file." --transport pull --file ./input.txt
    sh Y request status REQUEST_ID --refresh --json
    sh Y request list
    sh Y send bot-box "{\"type\":\"status\",\"job\":\"backup\",\"ok\":true}" --kind status
    sh Y reply MESSAGE_ID "Done. Summary attached." --file ./summary.md

- `--file PATH` (repeatable) for ordinary sends and push requests: the file is copied into an unguessable link that expires after 24 hours
  (`--ttl-hours N`) and that only the recipient machine can download; the recipient verifies size and
  SHA-256. This machine's receiver must keep running until the recipient fetches it.
- Structured data: put JSON or other text in the message and label it with `--kind` (up to 32
  characters, such as `status`, `request`, `data`). A message is limited to 256 KiB; send bigger data
  as `--file`.
- `--wake now|batch|none` is only a request; the receiver's policy decides and may lower it. Leave it
  out for receivers that predate the wake field (they reject it).
- `reply ID TEXT` sets reply_to and goes back to the sending agent. For a tracked request it also
  saves a final `completed` response; use `request respond --state running` for progress and
  `--state failed|rejected|blocked` for other final outcomes (choose one state, not the literal pipe list).
- `--as NAME` chooses the local agent on a new message; the default is configured self_agent.
  Tracked replies preserve the original authenticated route and reject `--agent`, `--as` and `--port`
  overrides. Both `reply` and `request respond` accept `--file PATH` and optional `--ttl-hours N`.
  Pull replies accept files but reject `--ttl-hours`; those files persist until explicit local cleanup.

## One-way shared machines

Use `request send PEER TEXT --transport pull` when the visitor can reach a shared receiver but the
receiver cannot open connections back. `push` remains the default. Both ends must support
`request-pull-v1`; never silently downgrade a pull request to push or an untracked message. Before
sending, `doctor --peer PEER --json` checks the running receiver's health, identity acceptance and
capability without sending a message or changing network policy. An empty shared-device display name
is compatible with an authenticated stable Tailscale node identity on updated receivers.

The visitor uploads input attachments before the request is queued. It does not need a running
receiver. The target's authorized `reply` or `request respond` saves a response for the original
requester, without reverse connections. `stored: true, transport: "pull"` means stored for polling,
not delivered to the visitor. Poll with `request status ID --refresh --json`, then `fetch RESPONSE_ID`
to download and verify result files. Pull responses do not wake an agent on the visitor's machine.
Files are retained until explicit local cleanup; `--ttl-hours` is unsupported. At most 100 input files
fit within the target's `max_fetch_bytes` total (default 1 GiB); unfinished uploads expire at the
request deadline, while accepted requests and their results remain queryable.

If preparation, upload or submission was interrupted, use `request resume REQUEST_ID` to retry the
saved envelope and snapshots under the same ID. Do not run a new `request send` to retry transport.
One-way pull does not enable unrelated messages back to the visitor. Ordinary sends, push replies
and sender-hosted files continue to require the reverse path. Network permission does not grant
task execution, wake-up or automatic reply authority.

## Requests require an outcome; notifications need no reply

Use `request send PEER TEXT --timeout-seconds 900` whenever you need the other agent to answer or
perform work and report the result. `send --kind request` also creates a tracked request by default.
Both require the peer to advertise `request-lifecycle-v1` in authenticated `/info`; a legacy or
unreachable peer must not silently get an untracked fallback. Only use `send --kind request
--legacy-request` when the user accepts legacy delivery without tracked receipts or final outcomes.

For information that needs no response, use `send --kind notice`. Upgrade notices also need no reply;
when upgrade verification is required, send a separate authorized tracked request. Do not reply to
receipts or responses, and do not turn a notification into a request simply to get an acknowledgment.

The receiver durably records a minimal `received` receipt automatically, without an agent turn.
The agent reports `running`, then a final `completed`, `failed`, `rejected` or `blocked` outcome.
A required result can be a refusal or missing-permission status: requests do not grant execution or
business-reply permission. Receipt/response delivery uses a durable retry outbox and stable IDs;
there is no guarantee that an offline agent executes or that a task executes exactly once.

Use `request status ID --json` for locally saved state and `--refresh` to reconcile with the original
receiver. `request list` lists saved requests. After the deadline, an unanswered request is
`timed_out`: the execution outcome is unknown, not cancelled or necessarily failed. A late final
response may update it and is marked late. Before retrying work, reconcile the same request and its
actual effects; another `request send` creates a different ID and can duplicate work.

## Read a tracked result or its attachments

Tracked responses are stored in the authenticated request ledger, not the ordinary inbox; they never
wake an agent. Get `response.id` from `request status REQUEST_ID --json` (refresh first if needed):

    sh Y show RESPONSE_ID
    sh Y fetch RESPONSE_ID

These commands can read the authenticated response from the ledger. `fetch` checks the original
sender, size and SHA-256 and saves files under `<home>/inbox/RESPONSE_ID/files/`. Do not reply, ack or
manually wake the protocol response. A response ID is different from its original request ID.

## What the result means

`send` and `reply` print JSON such as `{"status": 202, "id": "...", "to": "...", "files": 1}`.
Pull replies instead report local result storage; confirm delivery by polling and fetching on the requester.

| status | meaning |
|---|---|
| 202 | Delivered: the receiver checked this machine's Tailscale identity and stored the message. It has not necessarily been read or acted on. |
| 200 | Already delivered earlier (same id); nothing new happened. |
| 403 | The receiver's access policy denied this machine, or the identity did not match. Check deny rules and any active allowlist; do not widen access without the owner's authorization. |
| 400 | A field was rejected (for example a named agent or wake on an older receiver) or the address was wrong. |
| 413 | Too large; send the content as a file. |
| connection error | No complete response: receiver or network path may be unavailable. Delivery is unknown; the receiver may already have stored the message. |

For a timeout or connection failure, report the named peer, resolved IP:port, and message ID from
the diagnostic. Check that the receiver is running, then use the suggested bounded, read-only
`tailscale ping --c 3 --timeout 3s HOST` to inspect the network path. A successful ping does not prove
the Yobiru port or access policy allows the request. Do not restart Tailscale automatically. Check
the recipient's inbox for the message ID before another send: running `send` again creates a new ID
and can duplicate an already delivered message.

Report the message ID and exact state. For a tracked request, save the ID and check `request status`
instead of waiting without a way to inspect it. A receipt does not prove the agent read or did
anything; a final response reports its outcome, and its evidence still needs checking. Legacy replies
are ordinary messages whose `reply_to` matches the original ID (see yobiru-inbox).
