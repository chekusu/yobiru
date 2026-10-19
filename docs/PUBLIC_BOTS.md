# Public bot contacts (first version)

**Status: unreleased first version on a development branch.** It has been tested only with synthetic data,
local processes and loopback HTTP. It has not been deployed on a public address, packaged in a release,
or verified inside a native Grok Bot, Codex or Cursor host. Private Tailscale messaging (tailmsg protocol v1,
receiver port 7766) is unchanged and does not depend on anything described here.

People and their agents add a **bot** as a contact, the way they would add an account in a chat app, instead
of addressing a machine. A bot has an immutable id (`bot_...`) and an editable public handle (`@assistant`);
the machine that answers for it can change without changing the contact. Callers do not need Tailscale.

```text
caller's agent (CLI/skill)                              owner's devices
  yobiru public chat ... ──HTTPS──> public gateway <──HTTPS── constrained worker ──loopback──> private calendar
                                    (self-hosted mailbox)      (outbound polling,             capability service
                                                               deterministic answers)         (raw events stay here)
```

The gateway is a separate process (`yobiru gateway serve`, default `127.0.0.1:7780`) with its own data
directory and credentials. It never contacts the private receiver, never makes outbound requests, never
runs adapters or shell commands, and never forwards public content to an agent session.

## What this version answers

The bot answers only through a built-in **deterministic responder** with exactly three capabilities:

| Capability | What happens | What leaves the owner's side |
|---|---|---|
| `faq` | The caller's text is matched against owner-written keywords; the best match's owner-written answer is returned, or an owner-written fallback. | Only owner-written text. The caller's text is never echoed, executed or used to read anything. |
| `leave_message` | The text is saved in the worker device's local inbox (`<data home>/public/worker-inbox/`), labeled untrusted. | A fixed confirmation. Reading and replying is the owner's own decision (`owner reply`). |
| `calendar_slots` | Read-only meeting-time suggestions computed by the private capability service from the owner's events file and rules. | At most `max_slots` (default 3) start/end times in UTC, an `as_of` time, and "nothing has been booked". |

Anything else fails closed: unknown message kinds, capabilities, responder types or fields are refused.
There are no attachments, no meeting writes, no invitations, no group rooms, no external chat-provider
connectors (such as LINE or WeChat) and no calendar provider sync in this version.

## Roles, credentials and the trust boundary

| Role | Credential | Can do | Cannot do |
|---|---|---|---|
| Gateway administrator | Local file access to the gateway home | `gateway init`, `gateway owner add/rotate` | Nothing over HTTP: there is no remote bootstrap or admin endpoint. |
| Owner | `ybo_` owner credential (from `init` or `owner add`) | Create, edit, publish and unpublish bots; decide contacts and grants; bind workers and verifiers; read conversations; reply; delete conversations | Act as a worker or caller; see other owners' bots. |
| Worker | `ybw_` per-bot worker credential (`worker bind`) | Claim, start, finish and release work of **one** bot | Owner actions, contacts, other bots, capability redemption. |
| Capability service | `ybv_` per-bot verifier credential (`verifier bind`) plus a local `ybc_` access credential | Redeem tickets of that bot's started work; answer the worker over loopback | Claim work, read conversations, act as owner. |
| Caller | `yba_` actor credential (`enroll`) | Request friendship, open conversations, send, read their own status and history | Read or address anything of another caller; choose a machine, agent or session. |

Credentials are random 256-bit tokens. The gateway stores only their SHA-256 hashes; the CLI stores tokens in
`0600` files, sends them only in the `Authorization` header to the exact gateway URL they were issued for, never
prints them, refuses plain `http://` for anything but loopback, and never follows redirects.

**Enrollment is unauthenticated and pseudonymous.** Anyone who can reach the gateway can obtain a caller
credential (`verified_person: false`). It proves possession of that token, not who someone is; display names
are self-asserted and labeled unverified. Friend privileges always need the owner's explicit approval.

**Trusted operator versus hostile request.** The gateway, the CLI, the deterministic responder and the
capability service are trusted code run by the owner. Public message content, contact-request notes and
display names are hostile input: they are stored and shown as data, and they can only select among
enumerated, schema-checked operations. They never become shell commands, file paths, URLs, adapter targets,
owner instructions or tool calls, and they are never written into the private tailmsg inbox, a wake adapter
or the Codex bridge.

**Same-user processes are not a sandbox.** The worker never receives the events file, the verifier credential
or any calendar path, and it cannot redeem tickets; the capability service receives only a ticket and asks the
gateway what may be computed. That is an interface boundary. If the worker and the capability service run as
the same unrestricted OS user, a compromised worker could still read the owner's files. For a host-enforced
boundary, run the capability service as a different OS user or in a separate container; this version does
not set that up or verify it.

### Isolation contract for personal agents

Unrestricted personal agent sessions (Grok, Codex, Cursor or any LLM session with the owner's memory,
files, tools or accounts) are **not** safe public workers, and this version refuses them: `responder.type`
must be `deterministic`; `codex`, `grok`, `llm`, `exec`, `webhook`, `mcp` and anything else fail closed.
Connecting a non-deterministic responder would first need an enforceable contract, verified on the actual
host: no access to the owner's memory, files, accounts or general tools; only the capability APIs above; a
separate OS identity or container; per-caller conversation isolation; and egress limited to the gateway. A
prompt asking the model not to leak is not such a boundary. Native agents can still operate this CLI as
callers or as owner administrators, with their host's normal approvals.

## Contacts and grants

A first conversation makes the caller a **visitor** (capabilities: the bot's visitor policy, by default
`faq` and `leave_message`). A caller can send a contact request; the owner approves it with an explicit
capability list for that one friend, bounded by the bot's friend policy:

```sh
sh "$Y" public owner contact assistant ACTOR_ID approve --capabilities faq,leave_message,calendar_slots
sh "$Y" public owner contact assistant OTHER_ACTOR approve --capabilities faq    # chat only, no calendar
sh "$Y" public owner contact assistant ACTOR_ID grant --capabilities none        # explicit empty grant
```

| Owner action | Effect |
|---|---|
| `approve --capabilities LIST` | Friend with exactly LIST (may be empty). Approving a revoked contact starts a new grant generation and a new epoch. |
| `grant --capabilities LIST` | Changes a friend's list. |
| `reject` | Declines a pending request (the caller may ask again after a cooldown). |
| `demote` | Friend back to visitor. |
| `mute` | No new messages from this caller, and their pending work is cancelled; history stays readable. `unmute` allows new messages; cancelled work stays cancelled. |
| `revoke` | Relationship ended: pending work cancelled, conversations closed, later reads refused, new messages refused. |
| `restore` | A revoked contact may start again as a visitor in a new epoch; old conversations stay closed. |

Every change that removes something a contact could use (narrowing, demotion, revocation, a bot policy that
drops a capability) starts a new **grant generation**: the contact's pending work is cancelled, and results
admitted under an older generation are no longer released to the caller, including results never fetched.
Widening never revives cancelled work or withheld results. Removing a capability from the bot policy also
removes it from every friend's stored grant, so re-adding it later regrants nobody silently.

Grants are per bot: being a friend of one bot gives nothing on another. A result the caller already
received cannot be taken back; withholding applies to the gateway's later answers only.

## Messages, receipts and outcomes

A caller message is admitted only after authentication, publication, relationship, capability, size and
limit checks, and it is committed to SQLite before the `202` receipt. The receipt means **stored, not
executed**.

| State | Meaning |
|---|---|
| `queued` | Stored in the bot's mailbox; no worker has picked it up (for example, the owner's device is offline). |
| `claimed` | A worker holds a lease; not started. |
| `started` | The worker reported starting under a current lease. |
| `completed`, `declined`, `failed` | The worker's final outcome. `failed` with `capability_unavailable` means nothing was confirmed. |
| `expired` | The deadline passed (default 1 day, at most 7). `deadline_after_start` means the outcome is unknown. |
| `cancelled` | The owner's settings changed (mute, revoke, narrowing, unpublish, deletion): no further work or result is accepted or released for it. Work that had already started may still have happened. |

Publication and the contact's current grant are rechecked at admission, claim, start, capability
redemption and finish, and again on every caller read.

**Idempotency and recovery.** Every send carries an op key, scoped to the caller and conversation. The CLI
records the op key, conversation and payload in `public/sent.sqlite3` before the message leaves, and records
a dispatch durably before each POST. Retrying with the same op key returns the same message
(`duplicate: true`); the same key with different content is refused (`409`). After a lost response:

```sh
sh "$Y" public pending                    # sends without a confirmed message id
sh "$Y" public status --op-key OP         # ask the gateway; a found message is confirmed
sh "$Y" public resend OP                  # the identical payload under the same op key
```

A lookup that finds no message is an observation, not proof: the same request may still be in flight, and
records older than the gateway's retention period are deleted. A refusal after an uncertain attempt does not
settle that attempt either; such sends stay `unknown`. Nothing is resent automatically.

**Workers and leases.** A worker claims work under a lease with a fence number; start and finish need the
current lease, fence and worker binding. An expired lease, a stale fence or a replaced worker is refused
(`409 fenced`, or `401` for a revoked credential). `worker bind --replace` (for example after moving the
bot to a rebuilt host) revokes all earlier workers of the bot, advances its worker epoch and hands their
claims back to the queue. Claims are therefore at-least-once: a claimed or started item can be processed
again after a crash or relocation (`previously_started: true`). The deterministic responder is read-only, so a
repeat has no external effect; this is not exactly-once execution. One device runs at most one worker process
per bot and data home; a second one is refused instead of releasing the first one's claims.

## The calendar capability

The owner keeps calendar data in a JSON events file on their own device (an export, not a provider
connection):

```json
{"events": [{"start": "2030-01-07T09:00:00Z", "end": "2030-01-07T10:00:00Z", "title": "never disclosed"},
            {"all_day": true, "date": "2030-01-08"},
            {"start": "2030-01-07T13:00:00+09:00", "end": "2030-01-07T14:00:00+09:00", "status": "cancelled"}]}
```

`status: cancelled` and `transparency: transparent` events are free time; everything else is busy, and an
all-day event blocks the local day. Titles, attendees, locations, notes and any other field are never output.
A malformed event makes the service refuse (`calendar_unavailable`) rather than guess.

Owner rules (in the service's `service.json`): time zone (`UTC`, a fixed offset such as `+09:00`, or an IANA
name on Python 3.9+), work days and hours, offered durations, horizon (days), minimum notice, step, buffer
around busy events, `max_slots` (at most 5), which relationship levels may ask (default: friends only),
per-contact and per-bot daily query budgets, and a freshness window for cached answers (default 15 minutes).
Slots are computed in absolute time; on daylight-saving transition days a work window whose start or end does
not exist or occurs twice is skipped.

Each answer is authorized by the gateway twice (after the service's lock is held, and again just before
anything is released or budget is spent), keyed to the caller's stored query and grant generation. A retried
claim of the same message receives the identical cached answer without spending budget, but only while the
rules, the events file and the freshness window are unchanged; otherwise it is refused as stale and never
recomputed. Suggestions are availability at `as_of`, not bookings. Approved free time is intentional
disclosure within these bounds, not zero disclosure: earliest-first suggestions also show that earlier times
in the window were not offered.

## Limits

Defaults (override in `gateway.json` `limits`):

| Limit | Default | Bounds |
|---|---|---|
| `max_body_bytes`, `max_text_chars` | 16384, 4000 | request size; checked before reading the body |
| `read_timeout_seconds` | 10 | total time for request line, headers and body |
| `max_concurrent_requests` | 32 | extra connections get `503` at once |
| `enroll_per_hour`, `enroll_per_hour_per_ip`, `max_actors` | 60, 10, 10000 | caller identities on the gateway |
| `new_contacts_per_hour_per_bot` | 30 | first-time callers one bot admits, however many identities exist |
| `pending_contact_requests_per_bot`, `contact_requests_per_hour_per_actor` | 50, 5 | open friend requests |
| `conversations_per_contact`, `new_conversations_per_hour_per_bot`, `turns_per_conversation` | 5, 60, 100 | conversation growth and reply loops |
| `messages_per_minute_per_actor`, `messages_per_hour_per_actor` | 10, 60 | per caller |
| `pending_per_contact`, `pending_per_bot`, `pending_total`, `max_messages` | 10, 200, 2000, 100000 | queued work and storage |
| `min/default/max_deadline_seconds` | 60 / 86400 / 604800 | message deadlines |
| `max_lease_seconds`, `claim_max`, `workers_per_bot` | 300, 10, 4 | worker claims |
| `retention_days` | 30 | deletion of finished message bodies and results |

Per-caller limits alone could be bypassed by enrolling many identities; the per-bot admission, pending-queue
and gateway-wide enrollment limits bound that. Connection-level abuse protection (TLS, request rates per
network, slow-loris defense at scale) belongs to the reverse proxy and was not tested here.

## Privacy, storage and deletion

The gateway is a **message store you run**. It keeps in plaintext SQLite (`gateway.sqlite3`, `0600`):
bot profiles and policies, caller display names, contact relationships and request notes, conversation
membership, message bodies, stored queries, worker results and owner replies, credential hashes, and
rate-limit counters, some keyed by client IP address for up to about an hour. Its log (stderr) records
client addresses, request routes and status codes, never bodies or credentials. There is no end-to-end
encryption: whoever operates the gateway host can read message content.

Retention: message bodies and results are deleted 30 days after the message finished (default
`retention_days`). Caller identities, bot profiles, contacts, conversations' metadata and handle
reservations are not deleted automatically. The owner can delete a conversation's stored messages
(`public owner delete-conversation CID`); SQLite `secure_delete` overwrites removed content in the database
file, but write-ahead-log files, backups and copies already delivered to callers, workers or their agents'
providers are not erased. Revocation stops later releases; it cannot recall what was already received.

On the owner's devices: the worker inbox keeps delivered leave-messages, `leases.json` keeps claimed items
until they finish, and the capability service's `ledger.sqlite3` keeps the slot answers it released. On the
caller's device: `sent.sqlite3` keeps sent payloads (resolved rows beyond the newest 1000 are pruned), and
the CLI stores contacts and credentials. None of these are deleted automatically.

## Deploying the gateway

The gateway listens on loopback by default and refuses other addresses unless `allow_non_loopback_bind` is
set in `gateway.json`. Put an HTTPS reverse proxy in front of it:

```text
# Caddy example
bots.example.com {
    request_body {
        max_size 16KB
    }
    reverse_proxy 127.0.0.1:7780
}
```

- Use the HTTPS URL as `--public-url` at `init`; the CLI refuses plain `http://` for non-loopback gateways.
- Set `trust_forwarded_for` only when the proxy runs on the same host and sets `X-Forwarded-For`; otherwise
  every caller shares the proxy's address in the per-IP enrollment limit.
- Configure connection limits and timeouts at the proxy as well.
- Tailscale Funnel can provide public HTTPS transport to the gateway, but Funnel traffic carries no
  Tailscale identity; the gateway's own credentials do the authentication. Never expose the private receiver
  (port 7766) through Funnel or a proxy.
- Back up the gateway home while the service is stopped, and protect backups like the database itself.

The owner's worker and capability service connect outbound to the gateway; they need no open port. Running
them continuously (systemd, launchd) is the owner's setup; this version ships no service templates for them.

## A complete local walkthrough

All commands run as written on one machine with loopback addresses, using separate data homes for the
roles (`--home` before the command). `Y` is the plugin's `scripts/yobiru`.

```sh
G=/tmp/yobiru-demo; mkdir -p "$G"
sh "$Y" gateway --gateway-home "$G/gateway" init --port 7780              # owner credential: $G/gateway/owner.token
sh "$Y" gateway --gateway-home "$G/gateway" serve &                       # http://127.0.0.1:7780
URL=http://127.0.0.1:7780

# owner device
sh "$Y" --home "$G/owner" public owner login "$URL" --token-file "$G/gateway/owner.token"
sh "$Y" --home "$G/owner" public bot create --handle assistant --name "Demo assistant"
sh "$Y" --home "$G/owner" public bot publish assistant

# a caller (no Tailscale needed)
sh "$Y" --home "$G/caller" public enroll "$URL" --name "Demo caller"
sh "$Y" --home "$G/caller" public add "$URL/@assistant"
sh "$Y" --home "$G/caller" public chat assistant "What can you do?" --faq          # 202 queued, not executed
sh "$Y" --home "$G/caller" public request-friend assistant --note "Hello"

# owner: approve with explicit capabilities, then hand a worker credential to the worker device
sh "$Y" --home "$G/owner" public owner contacts assistant --pending                  # note the actor_id
sh "$Y" --home "$G/owner" public owner contact assistant ACTOR_ID approve --capabilities faq,leave_message
sh "$Y" --home "$G/owner" public worker bind assistant --token-file "$G/worker.token"

# worker device: dedicated credential, deterministic responder, one pass
sh "$Y" --home "$G/worker" public worker login "$URL" --token-file "$G/worker.token" --handle @assistant
sh "$Y" --home "$G/worker" public responder init BOT_ID
sh "$Y" --home "$G/worker" public worker run BOT_ID --once

# the caller sees the final answer
sh "$Y" --home "$G/caller" public history assistant
```

For calendar suggestions, the owner adds `calendar_slots` to the friend's grant, then:

```sh
sh "$Y" calendar-service init --dir "$G/calendar" --gateway "$URL" --bot BOT_ID     # edit policy, write events.json
sh "$Y" --home "$G/owner" public verifier bind assistant --token-file "$G/calendar/verifier.token"
sh "$Y" calendar-service serve --dir "$G/calendar" &                                 # prints its loopback URL
sh "$Y" --home "$G/worker" public responder init BOT_ID --force --calendar-url CAL_URL \
    --calendar-token-file "$G/calendar/access.token"
sh "$Y" --home "$G/caller" public slots assistant --duration 30 --after 2030-01-07 --before 2030-01-08
```

## HTTP API (version 1)

All bodies are JSON objects (no duplicate keys, no unknown fields); credentials go in
`Authorization: Bearer ...`. Unknown routes answer `404`, query strings `400`.

| Route | Credential | Purpose |
|---|---|---|
| `GET /v1/health` | none | liveness |
| `POST /v1/actors` | none | enroll a pseudonymous caller (shown once) |
| `GET /v1/bots/{bot_id or @handle}` | none | public card of a published bot; anything else is `404` |
| `GET /v1/actor` | caller | own identity |
| `GET /v1/bots/{ref}/contact`, `POST .../contact-request` | caller | own relationship; ask to be a friend |
| `POST /v1/bots/{ref}/conversations`, `GET` the same | caller | open (idempotent by `op_key`) or list own conversations |
| `GET /v1/conversations/{cid}`, `POST .../messages` | caller | own history; send |
| `GET /v1/conversations/{cid}/operations/{op_key}` | caller | reconcile a send by op key |
| `GET /v1/messages/{mid}` | caller | own message state and released result |
| `GET /v1/owner`, `GET/POST /v1/owner/bots`, `GET/PATCH /v1/owner/bots/{id}` | owner | owner identity and bots |
| `POST /v1/owner/bots/{id}/publish`, `.../unpublish` | owner | publication |
| `GET .../contacts`, `POST .../contacts/{actor_id}` | owner | contacts and decisions |
| `POST/GET .../workers`, `POST .../workers/{id}/revoke` | owner | worker binding and fencing |
| `POST/GET .../verifiers`, `POST .../verifiers/{id}/revoke` | owner | capability service credentials |
| `GET .../conversations`, `GET/DELETE /v1/owner/conversations/{cid}`, `POST .../messages` | owner | read, delete, reply |
| `GET /v1/worker`, `POST /v1/worker/claim`, `POST /v1/worker/messages/{mid}/start|finish|release` | worker | the work loop |
| `POST /v1/capability/redeem` | verifier | authoritative subject and query for one started work item |

The private calendar service exposes only `POST /v1/suggest` (access credential, body `{"ticket": ...}`) and
`GET /v1/health`, on `127.0.0.1`.

## What has and has not been verified

| Evidence state | This version |
|---|---|
| Configured | Example configurations and commands above. |
| On disk | The plugin archive includes `scripts/public_bots/`; the build check passes. |
| Running locally | Gateway, worker, capability service and callers as separate processes on loopback in automated tests (see [verification](VERIFICATION.md)). |
| Native host discovery | Not tested: no Grok Bot, Codex or Cursor session used these skills in this version. |
| Deployed | Not done: no public address, TLS proxy, Funnel or real calendar was used. |

Not verified or not provided: deployment behind a real proxy; real calendar exports; Windows and macOS
runs; execution on Python 3.8 (syntax only); IANA time zones without Python 3.9+; any OS-level isolation;
wake-up of a sleeping agent host (the worker must be running to answer); native marketplace installation.
