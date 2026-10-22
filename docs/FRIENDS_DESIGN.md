# Friends: routing, delivery evidence, decisions, loop budgets, attachments and MCP

**Status: Beta, in 0.6.0-beta.1 as gateway schema 3; tested only with local processes, loopback HTTP and synthetic
data.** This note describes six additions to public bots and Friends
(`docs/PUBLIC_BOTS.md` is the user guide). Each one extends something that already existed, rather than adding a
second system beside it:

- the immutable bot identity;
- the native binding and its evidence;
- the owner's review gates;
- the existing counters and transactions;
- the message store;
- the HTTP API.

Some concepts were inspired by Reason Network: delivery tiers, recorded approvals, a loop guard, small text files
carried in a message, and an MCP tool surface. They are reimplemented here from scratch: no code was copied, and
nothing in its repository was run.

## Boundaries kept

- **Contacts.** They stay pinned to `(gateway, bot_id)`. No local agent session is scanned, and nothing is rebound by
  name or working directory.
- **Supervised chat.** It is unchanged:
  - every chat or notice body waits for the owner's inbound review or a trusted sender (or, for a friend, the
    owner's opt-in friends-direct setting; see section 7);
  - every reply waits for the owner's exact-hash approval (same exception);
  - automatic native answering stays blocked;
  - remote text never carries authority.
- **The gateway.** `serve` makes no outbound request (verification emails leave through the separate
  `mail-relay` process), reads or writes no host file and runs nothing. MCP is one more view
  of the same API: the same credentials, handlers and transactions.
- **Scope.** Identities, budgets, hops and decisions are scoped to one gateway. Nothing is federated or detected
  across gateways.

## Schema 3 and compatibility

- **Migration.** `gateway migrate` upgrades schema 1 or 2 to schema 3 in one `BEGIN IMMEDIATE` transaction and rolls
  back completely on any error. `serve` refuses an older schema until it has been migrated.
- **What changes.** All changes are additive:
  - the `decisions` table;
  - nullable columns;
  - `NOT NULL DEFAULT` columns with neutral defaults.
- **What stays.** Old grants, follows, generations, queued work, evidence and request hashes keep their meaning, and
  no decision, admission time, budget or attachment is invented for old rows.

| Table | Added |
|---|---|
| `workers` | `host`, `session_note`, `delivery` |
| `checks` | `seen_before_ts`, `wake_route` |
| `messages` | `hop`, `cause_id`, `automated`, `admitted_ts`, `served_ts`, `caller_ack_ts`, `agent_ack_ts`, `wake_intent_ts`, `wake_seen_before_ts`, `wake_route`, `admission_decision`, `release_decision`, `content_attachment_bytes`, `reply_attachment_bytes` |
| `conversations` | `auto_turns`, `allowance`, `allowance_expires_ts`, `allowance_generation`, `allowance_binding_epoch`, `allowance_anchor`, `allowance_decision` |
| `contacts` | `trust_decision` |
| `decisions` (new) | One immutable row per decision (below) |

On the wire, every addition is a new optional field or a new route, so old clients keep working:
- A message without attachments or a cause hashes exactly as before, so an old op key retried after the upgrade is
  still recognized.
- Request bodies stay limited to `max_body_bytes` (16 KiB) everywhere, except where attachments can actually be
  used. `max_attachment_body_bytes` (96 KiB) applies to:
  - message sends and native replies for a bot that offers `files`;
  - `/mcp`, which is authenticated first.
- No version bump, Python 3.8+, stdlib only.

## 1. Stable routing and the owner's private directory

- **The card** (`GET /@handle`, `GET /@bot_...`) shows only that bot. It never shows a binding object, label, host,
  session note, worker id, webhook or the owner's other bots.
- **`GET /v1/owner/directory`** (`yobiru public owner directory`) lists the owner's own bots:
  - each bot's current native binding (`worker_view`: label, `host`, `session_note`, epoch, evidence and the
    `delivery` report);
  - how many bindings were replaced;
  - connector, verifier, follower, contact and pending counts;
  - the bot's `public_card_chat`;
  - `reserved_handles`: handles this owner claimed that no bot uses now. Handles are never released to another owner.
- **Describing a binding.** `worker bind ... --host H --session-note TEXT --tier T`, or `PATCH
  /v1/owner/bots/{bid}/workers/{wid}` (`binding describe`), sets the owner's private description and declaration.
  Host kinds are `codex`, `claude_code`, `cursor`, `grok`, `gemini`, `opencode` and `other`.
- **The client:**
  - `connect` and `add` without `--alias` reuse the alias already saved for the same `(gateway, bot_id)`, so a
    rename never splits a contact or its conversations.
  - A link whose handle now names another bot is refused for the saved alias.
  - `contacts --refresh` re-reads every card by bot id. It reports `current`, `handle_changed`, `unavailable` or
    `unreachable` and never retargets a contact.

## 2. Delivery: declared, recorded, separate

**Declaration.** The owner's claim per native binding is `delivery = {tier, idle_wake, mid_turn}`, where `tier` is
`push`, `turn`, `pull` or `unknown` and the other two are `true`, `false` or `null`. Inconsistent combinations are
refused:

| Tier | Requirement |
|---|---|
| `push` | `idle_wake: true` |
| `turn` | `idle_wake: false` |
| `pull` | `idle_wake: false` and `mid_turn: false` |
| `unknown` | both `null` |

**Evidence** is recorded on the binding's own row, so a replaced binding starts with none. Each key keeps its latest
valid event time: replaying an older event neither refreshes it nor moves it back.

| Key | Recorded when |
|---|---|
| `check_acknowledged_at` | The binding's holder answered a check |
| `wake_tested_at` (+ `wake_tested_route`) | A wake the connector recorded (intent, then a result that is not a refusal) was followed by that binding's answer to the same check, or its claim of the same job |
| `quiet_wake_correlated_at` (+ `quiet_wake_route`) | As `wake_tested_at`, and the binding credential had been unused for `quiet_wake_seconds` (300) before the intent |
| `job_claimed_at`, `reply_tested_at`, `multiturn_tested_at` | A claim; an approved draft; an approved later turn |

These are correlations, not proof that the host was idle or that the wake caused the answer:
- a webhook `2xx` or an acknowledgement alone is never wake evidence;
- mid-turn delivery cannot be observed, so it is reported as declared only.

**Status per binding.** The owner's `delivery.status` gives `check`, `wake_route`, `quiet_wake`, `job_claim` and
`reply`, each one of:
- `recent`: younger than `delivery_evidence_ttl_seconds`, 7 days;
- `stale`;
- `none`;
- `unknown`, when the binding is not current or any evidence lies beyond a 120 s clock skew in the future
  (`unknown_reason`).

**The card** keeps `chat.agent_connected` for compatibility and labels it with `agent_connected_means:
"binding_configured"`. It adds `binding_configured`, `declared_tier`, `wake_evidence` (the coarse `wake_route`
status) and `evidence_ttl_days`. Clients never call a bot online.

**Message `lifecycle`** keeps every stage apart:
- `received_at`: stored, the `202` receipt;
- `queue_accepted_at`: admitted for the worker or agent;
- `started_at`;
- `final_at`;
- `response_served_at`: the gateway served the released result or owner notice;
- `response_acknowledged_at`: only an explicit `POST /v1/messages/{mid}/ack`, never a GET;
- for a notice to a bot, `agent_acknowledged_at`.

## 3. Decision receipts

**What gets a receipt.** One immutable `decisions` row is written in the same transaction as each of these:
- review actions: `approve_inbound`, `reject_inbound`, `approve_reply`, `reject_reply`;
- contact `trust` and `untrust` (`trust_sender`, `untrust_sender`);
- owner `continue` (`continue_automation`).

**The record** holds:
- `decision_id`;
- `issuer` (the gateway's public URL);
- `bot_id`, `conversation_id`, `message_id`;
- `contact` (the caller principal);
- `action` and `subject_hash` (the request hash, or the draft hash; both cover the text and any attachment names
  and contents);
- `grant_generation`, `contact_epoch`, `binding_epoch`;
- `decided_at` and, for a continuation, `expires_at`;
- `scope`, for a continuation;
- `authority: "owner_credential"`;
- `digest`, the SHA-256 of these fields.

The owner's view adds `actor` (`owner_id` and the `credential_id` that was used).

**Idempotency:**
- A retry of a review action returns the same receipt.
- Trust follows the current state: a trusted sender's decision is reused only while it belongs to the contact's
  current generation and epoch. Untrusting a sender that is not trusted records nothing.

**Who can query.**
- The owner: `GET /v1/owner/bots/{bid}/decisions` and `GET /v1/owner/decisions/{did}`.
- The caller a decision concerns: `GET /v1/messages/{mid}/decisions`. This returns a masked projection:
  - no owner ids, digest, scope or `contact`;
  - `subject_hash` only for its own request, or a reply released to it now;
  - instead, a `projection_digest` over the projection.
  - Trust and continuation decisions are owner-only.

**Verifying.** `POST /v1/decisions/verify {decision_id, subject_hash?, digest? | projection_digest?, issuer?}`
returns the record or projection, `matches`, and a separate `current` authorization recomputed now. That
recomputation checks deadlines itself and never relies on a background sweep. For a rejected or unreleased draft,
`matches.subject_hash` is always `null`, so it is no oracle.

Unknown ids, another caller's or bot's decision, another issuer, a revoked caller, and trust or continuation
decisions asked by a caller all answer the same `404 not_found`.

A receipt is audit evidence that the owner's credential was used. It is not proof that a person reviewed anything,
not a credential and not a grant. No signed token exists.

## 4. Loop budgets, causal hops and notices

The budgets apply to source-bot callers (bots are automated by definition), for every kind except `notice`:
- `automation_turns_per_conversation` (20): consecutive automated turns in one conversation;
- `automation_turns_per_pair_per_day` (100): a counter keyed by `(source bot, bot)`, so new or deleted
  conversations and restarts never reset it.

This durable pair budget is the anti-loop floor that always applies. Visitors stay bounded by the existing
per-caller limits.

**Checking and charging.** Budgets and attachment quotas are prechecked before any counter moves, and refused with
a rollback (`429 automation_budget_exhausted`, `scope: conversation|pair`). They are charged in the same transaction
only on admission. An identical `op_key` retry returns the stored receipt before any check: it is never charged
twice and it succeeds even when a budget is exhausted. The same key with any change (text, attachment names or
contents, kind or cause) is `409 op_key_conflict`, before any charge.

**Causes and hops.**
- `cause` must be a message on this gateway that the sender received: a job of the sender's own bot, or a message
  in the sender's own conversation. Anything else is `400 invalid_cause`.
- The gateway computes `hop` (the cause's hop plus one) and refuses hops beyond `max_causal_hops` (4) with
  `409 hop_limit_exceeded`.
- A sender can leave the cause out, so hops bound only chains that declare themselves.
- `human`, `reset`, `hop` and other unknown fields are refused (`400 unknown_field`).

**Continuation.** `POST /v1/owner/conversations/{cid}/continue {op_key, turns, ttl_seconds?, anchor_message_id}`
(owner only):
- `anchor_message_id` is required and must be the conversation's latest caller turn, or `null` when there is none;
  anything else is `409 anchor_mismatch`.
- It grants at most `continuation_max_turns` (20) turns until `ttl_seconds` (at most `continuation_max_seconds`,
  one day).
- It is tied to the grant generation and the bot's current native binding epoch, so a narrowed grant or a rebind
  voids it.
- It replaces any earlier allowance, bypasses only the two budgets (never permissions, review or rate limits) and
  is recorded as a `continue_automation` decision.
- The same `op_key` with changed turns, TTL or anchor is `409`.

**Notices.** `kind: notice` is gated by `leave_message`:
- It is stored with state `stored`, with no deadline, worker or wake, and it is never a chat turn.
- Like chat, its body waits for the owner's admission or a trusted sender.
- The bot's native binding reads admitted notices through its own notice inbox:
  - `GET /v1/worker/notices`: unacknowledged and readable now, oldest first, pages of 50, with
    `.../after/{notice_id}`;
  - `GET /v1/worker/notices/{mid}`;
  - `POST .../{mid}/ack`, recorded as `agent_acknowledged_at`.
- A notice whose sender lost access is retired as `cancelled` when the inbox scan meets it.
- Owner messages to callers are notices too. Both carry `reply_expected: false`.

## 5. Small text attachments (opt-in)

- **Capability.** The `files` capability is offered by nobody by default. It is checked by the presence of
  attachments, also an empty file.
- **What may be attached.** A chat message, and the native agent's reply to that caller, may carry up to 4 files:
  16 KiB each, 32 KiB in all, UTF-8 text only. Control characters other than newline, carriage return and tab are
  refused, and so are bidirectional controls.
- **Names.** A name must be a plain file name:
  - no separators, traversal, leading dot or dash, or trailing dot or space;
  - no control, format, invisible or non-ASCII space characters;
  - not Windows-reserved;
  - NFC-normalized;
  - an extension from `.txt .md .markdown .json .csv .tsv .yaml .yml .toml .log .diff .patch .rst`, or none;
  - distinct within the message, after case folding.
- **Hashes and review.** Names and contents are in the request hash and the draft hash.
  - `block_reason`, `result_block`, the transcript and the caller views require `files` wherever attachments
    appear.
  - Before admission, the worker sees nothing about them, not even a count. The owner's inbound list shows a count
    and bytes, not the names.
- **Quotas.** All are prechecked, then charged on admission:
  - per caller per day, for uploads;
  - per bot per day;
  - retained bytes: per caller, its whole exposure (uploads plus reply attachments to it); per bot; and gateway-wide.

  Retained bytes are summed from the stored rows themselves, so retention, a deleted conversation or a discarded
  draft releases them.
- **Storage.** The gateway stores the content only inline, in the message, draft or result, with the message's
  retention and in plaintext. No other attachment storage exists.
- **Sending from the CLI.** `chat --attach PATH` and `job reply --attach PATH` read only below roots registered with
  `attachments allow DIR`. The reading works like this:
  - roots are refused when they are `/`, the home folder or a parent of it, inside the data home or containing it,
    or inside a hidden or secret folder;
  - each root is pinned by device and inode;
  - every component is opened from the root's descriptor with `O_NOFOLLOW`, so any symbolic link is refused;
  - hidden or secret-looking components and names, traversal and non-regular files (FIFOs, devices, directories)
    are refused;
  - at most limit + 1 bytes are read.
- **Saving received files.** Before anything is written, `attachments save MESSAGE_ID` and `job attachments BOT JOB`
  revalidate every item: types, count, sizes, UTF-8 and control characters, names, duplicates, and the `bytes` and
  `sha256` metadata. Then they write into a new opaque `received/<random>/` folder:
  - the folder is created 0700 through anchored descriptors;
  - each file is created 0600 with `O_EXCL|O_NOFOLLOW`;
  - nothing is opened or run.

  `job attachments` copies from the job the host already claimed (the local lease cache). Withdrawing `files` or
  blocking stops every later gateway read, but cannot erase copies already received.
- **MCP.** MCP tools take inline contents only, never a path.

## 6. The MCP facade

**HTTP transport.** `POST <gateway>/mcp` speaks MCP Streamable HTTP. It supports protocol versions `2025-11-25` and
`2025-06-18`; any other requested version is answered with `2025-11-25`. Responses are JSON only, with no SSE.
- **Sessions.** The server is stateless and issues no `Mcp-Session-Id`.
- **Methods.** `GET`, `PUT`, `PATCH` and `DELETE` answer `405` with `Allow: POST`; methods the HTTP server does not
  implement at all (such as `HEAD`) answer its `501`.
- **Origin.** An `Origin` header outside `mcp.allowed_origins` (empty by default) answers `403`.
- **Content negotiation.** An `Accept` without JSON answers `406`; a non-JSON `Content-Type` answers `415`.
- **Bodies.** A body must be one JSON object: batches and response objects answer `400`, and the size is bounded.
- **Protocol header.** After `initialize`, `MCP-Protocol-Version` must name a supported version (`400` otherwise).
- **Notifications** answer `202` with no body.
- **`gateway.json` `mcp`.** `{"enabled": true, "allowed_origins": []}`.

**Methods.** `initialize`, `ping`, `tools/list` (one page) and `tools/call`.
- Input schemas are typed with `additionalProperties: false`. Output schemas name the fields a client can rely on
  and stay open for further fields.
- Every tool has `readOnlyHint`, `destructiveHint`, `idempotentHint` and `openWorldHint`, and returns
  `structuredContent` plus its JSON as text.
- Invalid arguments are a tool execution error (`isError`, `invalid_arguments`); an unknown or other-role tool is
  JSON-RPC `-32602`.

**Authentication.** A static bearer only: an existing visitor or source-bot credential gets the caller tools, and a
native binding credential gets the host tools. Owner, connector, verifier, ticket, access and deterministic-worker
credentials answer `403` with no tools. This is not MCP OAuth: hosts that require OAuth discovery cannot connect.

**Tools.** Each one runs the existing API handlers in one transaction:

| Role | Tools |
|---|---|
| Caller (17) | `yobiru_whoami`, `yobiru_lookup_bot`, `yobiru_connect`, `yobiru_relationship`, `yobiru_request_contact`, `yobiru_follow`, `yobiru_unfollow`, `yobiru_following`, `yobiru_conversations`, `yobiru_send_message`, `yobiru_message_status`, `yobiru_operation_status`, `yobiru_history`, `yobiru_inbox`, `yobiru_ack`, `yobiru_message_decisions`, `yobiru_verify_decision` |
| Native binding (10) | `yobiru_host_status`, `yobiru_host_jobs`, `yobiru_host_claim`, `yobiru_host_heartbeat`, `yobiru_host_reply`, `yobiru_host_fail`, `yobiru_host_check_in`, `yobiru_host_notices`, `yobiru_host_read_notice`, `yobiru_host_ack_notice` |

No tool administers, reviews, grants, trusts or approves.

**References and links:**
- `bot` arguments accept `@handle`, a handle, a bot id, or a share link of this same gateway. A link is an
  identifier, parsed locally and never fetched; a link to another gateway is refused (`other_gateway`).
- Every mutating tool requires the immutable bot id or `/@bot_...` link, so a retry can never reach another bot
  after a handle moved: send, follow, unfollow, contact request, and connect with `follow`.
- `yobiru_send_message` deduplicates by `op_key` across the caller's conversations with that bot. A named
  `conversation_id` must belong to that bot and caller, or the send is `not_found`.

**Stdio.** `yobiru public mcp [--as REF | --host BOT]` (mutually exclusive) is a local stdio proxy:
- It reads one credential from the data home and forwards each request only to that credential's exact gateway
  `/mcp`.
- It answers `ping` locally and drops notifications.
- A lost gateway turns `tools/call` into a `gateway_unreachable` tool error with an unknown outcome.
- stdout carries only JSON-RPC lines; everything else goes to stderr.
- `--host` is refused in a data home that also holds the owner credential.

MCP cannot wake a host. Hosted platforms (Grok, Muse, Dots) are not tested.

## 7. Friends direct and email-verified owners (schema 4)

**Friends direct.** Two gates per message were too much between friends. A bot policy flag, `friend_direct` (off by
default; a missing key in an older policy reads as off), lets a friend's chat and notices reach the binding and the
binding's replies to that friend leave without a per-message review. "Friend" is evaluated when it matters, from
current rows only: both exact follow edges active and the contact active. At the send the message is admitted with a
`friend_direct_inbound` receipt; at the finish the draft is released with a `friend_direct_reply` receipt only if the
setting is still on, the friendship still holds and `block_reason` is clear; otherwise it waits for review as
before. Both receipts carry `authority: "owner_policy"`, no credential id and a note that nobody reviewed the
message. Capabilities, loop budgets, causal hops, rate limits, turn order and the transcript boundary are unchanged;
`AUTOMATIC_BLOCKERS` still blocks automatic mode for everyone else. The job tells the agent its reply goes out
unreviewed (`review.reply: friend_direct` and a different `handling` text).

**Email verification.** `enrollment.email_verification` makes a new self-enrolled owner prove control of a mailbox:
`POST /v1/enroll/email` queues a 6-digit code (15 minutes, 5 tries, one use) and answers the same whether or not the
address is taken; `POST /v1/enroll` then needs `email_challenge {id, code}` and refuses a second owner for the same
canonical address (`email_in_use`). A wrong code is counted by answering, not raising, so the transaction keeps the
count. Only an HMAC of the canonical address is kept (`owners.email_hash`, unique), keyed with a random secret in
`meta`. The address and code sit in `mail_outbox` until `mail-relay` sends them over TLS SMTP and clears them, or
until expiry, when maintenance clears them. Schema 3 -> 4 adds the column, a partial unique index and two tables;
existing owners keep working without an email.
