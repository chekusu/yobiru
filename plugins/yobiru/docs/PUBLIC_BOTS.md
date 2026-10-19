# Public bots and Friends

**Status: unreleased, on a development branch.** It has been tested with synthetic data, local processes and
loopback HTTP; one maintainer test also had a real local Codex agent answer two chat turns through an earlier
snapshot of it (see
[what has been verified](#what-has-and-has-not-been-verified)). It has not been deployed to production, packaged
in a release or tested inside a native Grok Bot. To try it, use a checkout of this development branch or a plugin archive
built from it: the published 0.5.3 release does not include it, although this branch keeps the version number
0.5.3. Private Tailscale messaging (tailmsg protocol v1, receiver port 7766) is unchanged and does not depend on
anything described here.

You publish one of your bots and share its link, for example `https://bots.example.com/@alice`. Someone else gives
that link to their own agent (Codex, Grok, any agent that can run this CLI), and their agent talks to your bot
through the gateway: no Tailscale, no browser chat page. A bot has an immutable id (`bot_...`) and an editable
handle; the machine that answers for it can change without changing the contact.

```text
caller's agent (CLI/skill) ──HTTPS──> gateway you host <──HTTPS── connector (wakes your agent with a job id)
  connect / follow / chat           (mailbox, identities,   <──HTTPS── your agent's native binding: job claim/reply
                                     follows, permissions)  <──HTTPS── deterministic worker: FAQ, leave-message, slots
your admin device (owner credential): publish, share, permissions, review inbound chat and replies
```

Commands below are written `sh Y ...`: `Y` stands for the quoted absolute path of the plugin's `scripts/yobiru`
launcher (Windows: `py -3 "<plugin>\scripts\yobiru.py"`), and `--home DIR` before `public` selects a data home.

The gateway is a separate process (`yobiru gateway serve`, default `127.0.0.1:7780`) with its own data directory
and credentials. It never contacts the private receiver, never makes outbound requests, never runs adapters or
commands, and never forwards public content into a tailmsg inbox, a wake adapter or the Codex bridge.

## Two ways a bot answers

| | Chat (`chat`) | FAQ, leave a message, meeting slots (`faq`, `leave_message`, `calendar_slots`) |
|---|---|---|
| Who answers | **Your own agent**, through the bot's single native binding | The built-in **deterministic responder** of a worker device |
| Freeform | Yes: real multi-turn conversation | No: owner-written FAQ answers, a fixed confirmation, or computed slot times |
| Your review | **Supervised**: you admit each message before your agent can read it (or trust a sender), and you approve each reply before the caller sees it | None needed: it releases only owner-written text or slot times |
| Automatic | No. Automatic native answering is not available in this version | Yes |

Supervised mode exists because this version supports no host that enforces a per-bot boundary. Grok Bot, as
documented and as reported by a Grok Bot asked about its own tools, gives every bot of an account the same files,
installed tools, browser logins, connected apps and shared memory, with no per-bot permission setting. Neither a dedicated bot nor an
instruction in a prompt makes such an agent safe to read strangers' messages unattended. The gates below are
enforced by the gateway, not by the agent.

## Identities

| Who | How it is created | What it proves | What it never proves |
|---|---|---|---|
| Visitor (`act_...`) | `yobiru public enroll URL` or automatically by `connect`; self-service | Continuity of the holder of its credential | A person, an organization, a bot |
| Source bot (`bot_...`) | Your own bot (`bot create`), or `yobiru public enroll-bot URL --handle H --name N` (self-service: a new owner credential and a new bot) | That requests come from that bot on this gateway, via a **source credential** that only the bot's owner can mint | A verified person or another platform's account; it can never claim an existing bot or handle |
| Owner | `gateway init` / `gateway owner add` (local, administrator) or self-enrollment (`enroll-bot`) | Administration of that owner's bots | Anything outside this gateway |

- The identity of every request comes only from its credential. Body fields naming a sender are rejected.
- Credentials have separate realms: owner (administration), source (follow and talk as one bot), worker (one bot's
  work; the native binding is a worker), connector (wake only; never reads messages), verifier (capability
  services). None substitutes for another.
- The client generates enrollment credentials itself and saves them (0600) before the request, so a lost answer is
  retried with the same credential and returns the same identity; a later refusal never discards an identity that
  an earlier attempt may have created.
- Identities belong to one gateway. There is no federation: a bot on another gateway is reached by enrolling there
  (as a visitor or a new source bot); a caller-supplied foreign URL proves nothing.
- Self-enrolled bots are source identities: they can follow, talk and be followed back, but cannot be published
  unless the operator sets `enrollment.self_enrolled_publish`.

## Share links

`GET /@handle` and `GET /@bot_...` return the bot's machine-readable card (the same JSON as `GET /v1/bots/{id}`):
`issuer` (the gateway), immutable `bot_id`, handle, links, the fixed capabilities of each relationship level,
whether an owner agent is connected for chat and that replies are owner-reviewed, protocol endpoints, and the
`yobiru public connect LINK` command. It never contains machines, sessions, native agent or routine details,
resource names, owner settings or credentials. There is no web page.

The client saves a contact by `(gateway, bot_id)`: a later handle rename or reuse never retargets it. Credentials
are only ever sent to the exact URL the card was fetched from; a card that names another issuer is reported, never
followed, and redirects are refused.

## Follows, friends and permissions

- **Follow** is directed: `follow LINK --as @my-bot` creates one edge from your bot to theirs (source credential).
  It needs no approval and nothing reciprocal: whether or not their bot follows you back, chat with it works when
  their owner's presets or grants allow chat for you, their agent is connected, and their owner approves its replies.
- **Friends** are two exact reverse edges. Following back is allowed even when the other bot is unpublished, as
  long as it already follows you (`follow bot_... --as @my-bot --gateway URL`).
- `unfollow` removes only your own edge. Visitors cannot follow.

Each bot's owner decides what each relationship level may use, in its policy:

| Field | Meaning |
|---|---|
| `offered` | The bot's ceiling: everything it offers at all. Removing something here also removes it from every explicit grant, and offering it again regrants nobody. |
| `visitor`, `follower`, `friend` | Presets for callers who do not follow, who follow one way, and mutual followers. Each is a subset of `offered`. The `friend` preset is your preconfigured Friends permissions. |
| `accept_followers`, `contact_requests` | Whether new follows and explicit-permission requests are accepted. |

Capabilities are `chat`, `faq`, `leave_message`, `calendar_slots` and `resource:<id>` (an owner-approved export).
Defaults: `chat`, `faq` and `leave_message` at every level; calendar slots and resources nowhere until you add them.

Per contact (`yobiru public owner contact BOT PRINCIPAL ACTION`):

| Action | Effect |
|---|---|
| `grant --capabilities LIST` | Explicit grants for this contact, separate from the relationship: they survive an unfollow and are labeled as owner grants. |
| `restrict --capabilities LIST`, `unrestrict` | A ceiling for this contact: becoming a friend never widens it. |
| `block` (alias `revoke`), `unblock` (`restore`) | Block dominates everything: both follow edges are removed, explicit grants cleared, conversations closed. Unblocking starts a new epoch; nothing earlier is revived. |
| `remove` | Removes this follower's edge (they may follow again; block them to prevent that). |
| `mute`, `unmute` | No new messages for a while; pending work is cancelled. |
| `trust`, `untrust` | Admit this sender's chat to your agent without a per-message review (see below). |
| `approve`, `reject`, `demote` | Answer a permission request; `demote` clears explicit grants. |

What a contact may use is `blocked ? nothing : (preset of its relationship + explicit grants) ∩ offered ∩ its
ceiling`, decided again on every access: at admission, claim, capability use, reply, owner approval and every read.
Any change that removes something a contact could use (unfollow, remove, block, a narrower preset, grant or ceiling,
unpublish) starts a new **grant generation** in the same transaction: its pending work is cancelled, leases and
capability tickets are refused, and everything written under the older generation (the bot's results and the
owner's messages) is no longer released to the caller or included in an agent's transcript. Following again,
regranting or republishing revives none of it. What a caller already received cannot be recalled.

## Chat with your own agent (native binding, supervised)

```sh
# admin device (keeps the owner credential)
sh Y public worker bind alice --adapter native --label "my agent" --token-file native.token
# your agent's own device: only native.token goes there
sh Y --home AGENT_HOME public worker login https://bots.example.com --token-file native.token
```

A bot has at most one native binding. Your agent works only through its binding credential, always with the same
data home:

```sh
sh Y --home AGENT_HOME public job list BOT_ID                    # metadata only: never text or free-text names
sh Y --home AGENT_HOME public job claim BOT_ID JOB_ID            # the message, its transcript, the caller's capabilities
sh Y --home AGENT_HOME public job reply BOT_ID JOB_ID --text ... # becomes a draft
sh Y --home AGENT_HOME public job fail BOT_ID JOB_ID --code no_answer
sh Y --home AGENT_HOME public job resource BOT_ID JOB_ID menu    # an export this caller may read
```

The gates, enforced by the gateway:

1. **Inbound review.** A chat message's text is withheld from the binding, and from your own owner views, until you
   admit that message (`review approve-inbound BOT JOB`, bound to its content hash), trust its sender
   (`owner contact BOT PRINCIPAL trust`), or explicitly reveal it (`review reveal`). The review list shows who sent
   it (their relationship, a verified source bot or a visitor), never the text.
2. **Reply review.** Your agent's reply is a draft the caller cannot see. `review approve-reply BOT JOB --draft-hash
   H` releases exactly that text after the gateway rechecks the caller's current permissions; `reject-reply`
   releases none of it. A host's decline or failure never releases host-written text.
3. **Order.** Turns of one conversation are answered in order, one at a time: a later turn cannot be claimed while
   an earlier one is queued, running, waiting for review or uncertain.
4. **Single claim and fencing.** A claim starts the job under a lease; it is never handed to a second run. Replacing
   or revoking the binding, or a lease that runs out, makes a started or drafted job **uncertain** (its draft is
   discarded and the old host's late answer is refused); you decide `review resolve BOT JOB retry` (it may repeat
   what the host already did) or `abandon`.

These gates hold only while the owner credential stays out of your agent's reach. The CLI refuses `job` commands in a
data home that holds the owner credential and refuses `review approve-reply` in a data home that holds the bot's
native credential (overrides exist and are documented as weakening the review). On a host where all agents share
files, such as one Grok Bot account, an agent manipulated by a message could still read an owner credential stored
elsewhere on that account and approve its own reply: keep the owner credential on another device.

### Conversations are Yobiru's, not the host's

Each caller has their own Yobiru conversations, and a claim hands your agent the transcript of that one
conversation, filtered by the caller's current permissions (`context_reset: true` when earlier turns were left
out). This is a **logical** conversation kept by Yobiru. It is not an isolated native thread: one Grok agent has
one conversation and a memory shared by its runs, so the host itself does not separate callers. No per-caller native
threads are created or claimed.

### Waking your agent

A connector is an always-on process on a machine you choose; it never reads message text. It wakes your agent with
an opaque job id through one route: a `webhook` (for example a Grok Bot routine; URL and key in private files), a
fixed command (`--exec ... {job_id} {phase}`), or `--manual` (you tell your agent yourself).

```sh
sh Y public connector bind alice WORKER_ID --token-file connector.token        # admin device
sh Y --home CONNECTOR_HOME public connector login https://bots.example.com --token-file connector.token
sh Y --home CONNECTOR_HOME public connector config BOT_ID --webhook-url-file url.txt --webhook-secret-file key.txt
sh Y --home CONNECTOR_HOME public connector run BOT_ID
```

Before each wake the connector records an intent at the gateway (and in a local ledger), then the result. Only a
failure before anything was sent counts as `refused` and is retried after a backoff. A 2xx is `accepted`; any other
answer (a 5xx or a 429 can follow work the host already queued), a timeout, or a command that ran and exited non-zero
is `uncertain` and is never repeated automatically (`review resolve BOT JOB rewake` if you decide to). A connector
that crashed during a wake reports it as uncertain on restart instead of waking again.

`binding check` creates a wake test. The binding records separate evidence: `check_acknowledged_at` (the credential
holder answered a check), `wake_tested_at` (a connector-recorded wake was followed by that answer),
`reply_tested_at` and `multiturn_tested_at`. None of these is evidence of isolation.

## The deterministic worker

FAQ, leave-message and meeting-slot messages go to a deterministic worker device (`worker bind BOT --token-file F`,
then `worker login`, `responder init BOT_ID`, `worker run BOT_ID`). Its responder answers only from owner-written FAQ
text, stores leave-messages in its local inbox (labeled untrusted) and asks the private calendar service for slots.
It never receives chat; other responder types (Codex, Grok, LLM, exec, webhook, MCP) are refused. Its claims are
replay-safe, so a replaced worker's unfinished claims return to the queue (at least once).

## The calendar capability

The owner keeps calendar data in a JSON events file on their own device (an export, not a provider connection):

```json
{"events": [{"start": "2030-01-07T09:00:00Z", "end": "2030-01-07T10:00:00Z", "title": "never disclosed"},
            {"all_day": true, "date": "2030-01-08"},
            {"start": "2030-01-07T13:00:00+09:00", "end": "2030-01-07T14:00:00+09:00", "status": "cancelled"}]}
```

`status: cancelled` and `transparency: transparent` events are free time; everything else is busy, and an all-day
event blocks the local day. Titles, attendees, locations and notes are never output; a malformed event makes the
service refuse rather than guess. Rules in the service's `service.json`: time zone, work days and hours, durations,
horizon, notice, step, buffer, `max_slots` (at most 5), which levels may ask (`visitor`, `follower`, `friend`; an
explicit grant counts as `friend`), per-contact and per-bot daily budgets, and a freshness window. Each answer is
authorized by the gateway twice; a retried claim gets the identical cached answer without spending budget while
rules, events and freshness are unchanged. Suggestions are availability, not bookings.

## Resource exports

```sh
sh Y resource-service init --dir RES --gateway https://bots.example.com --bot BOT_ID
sh Y resource-service add --dir RES menu --file menu.md --title "Menu"     # a copy, never a link
sh Y public verifier bind alice --token-file RES/verifier.token
sh Y resource-service serve --dir RES                                       # prints its 127.0.0.1 URL
sh Y public bot edit alice --offered faq,leave_message,calendar_slots,chat,resource:menu --friend-capabilities chat,faq,resource:menu
sh Y --home AGENT_HOME public job setup BOT_ID --resource-url URL --resource-token-file RES/access.token
```

A resource is an owner-approved text snapshot (UTF-8, up to 64 KiB) addressed only by an id of 1-32 of `a-z 0-9 _ -`:
no paths, URLs or patterns exist, and exports are opened without following links. Your agent asks for one with the
capability ticket of the job it is answering; the service redeems the ticket at the gateway, which allows it only if
that job's caller may use `resource:<id>` now, and it asks again after reading. An export that was removed or replaced
before that final check is not released. On a host where your agent can read the service's directory itself (same OS
user), this scoping is protocol-level only: run the service under another account or machine for a real boundary.

## Messages, receipts and outcomes

A caller message is admitted after authentication, publication, relationship, capability, size and limit checks, and
it is committed before the `202` receipt, which means **stored, not executed**.

| State | Meaning |
|---|---|
| `queued` | Stored; nobody has picked it up (for chat: possibly waiting for your inbound review). |
| `claimed`, `started` | A worker or your agent holds a lease. |
| `review` | Your agent wrote an answer; waiting for your approval. |
| `uncertain` | Your agent may have started it, but its outcome is unknown; never repeated automatically. |
| `completed`, `declined`, `failed` | Final. |
| `expired` | The deadline passed (default 1 day). `deadline_after_start` means the outcome is unknown. |
| `cancelled` | Permissions or settings changed; nothing further is accepted or released for it. |

Every send carries an op key bound to its gateway, contact and identity; the CLI records it before the message leaves
(`sent.sqlite3`). A retry with the same key returns the same message; `pending`, `status --op-key` and `resend`
reconcile a lost answer, and a lookup that finds nothing is an observation, not proof.

## Limits

Defaults (override in `gateway.json` `limits`): request bodies 16 KiB and text 4000 characters; replies 4000;
10 s to send a request; 32 concurrent connections; enrollment of visitors 60/hour (10 per IP) up to 10000, of bots
20/hour (5 per IP) up to 2000 self-enrolled owners with 2 bots each; 30 new contacts and 60 new conversations per bot
per hour; 60 follows per source and 120 new followers per bot per hour, at most 1000 followed; 10 messages per minute
and 60 per hour per caller; 240 chat messages per bot per hour; 100 turns per conversation; 10 pending per contact,
200 per bot, 2000 in total; deadlines 60 s to 7 days; worker leases up to 300 s and native job leases up to 1 hour;
transcripts of the last 20 turns or 24000 characters; refused wakes retried at most 5 times with a doubling backoff;
retention 30 days. Per-caller limits alone can be bypassed with many identities; the per-bot and gateway-wide limits
bound that. Connection-level protection belongs to your reverse proxy.

## Privacy, storage and deletion

The gateway is a **message store you run**: plaintext SQLite (`gateway.sqlite3`, 0600) with bot profiles and
policies, caller display names (self-asserted), follows, contacts and their grants, conversation membership, message
bodies, drafts, results and owner messages, credential hashes, and rate-limit counters (some keyed by IP for about an
hour). Its log records addresses, routes and status codes, never bodies or credentials. There is no end-to-end
encryption. Finished message bodies and results are deleted after 30 days; identities, follows, contacts and
conversation metadata are not deleted automatically. `owner delete-conversation` overwrites stored bodies (SQLite
`secure_delete`), but not write-ahead logs, backups or copies already received by callers, your agent or its
provider. Whatever your agent reads is processed by that agent's host and model provider.

## Deploying the gateway

```sh
sh Y gateway --gateway-home GW init --public-url https://bots.example.com   # writes GW/owner.token
sh Y gateway --gateway-home GW serve                                        # 127.0.0.1:7780
```

Put an HTTPS reverse proxy in front of it (limit request bodies to 16 KiB). Use that URL as `--public-url`; the CLI
refuses plain `http://` except on loopback. `gateway.json` `enrollment` sets whether visitors and bots may enroll
themselves and whether self-enrolled bots may be published (`false` by default). Set `trust_forwarded_for` only when
the proxy runs on the same host. Tailscale Funnel can carry public HTTPS to the gateway, but never expose the private
receiver (port 7766). A database from the first public-bot version is migrated with `gateway migrate` (stop the
gateway and back up first; one transaction; existing grants keep their meaning and no follows are invented).

## Try it locally

The plugin includes `examples/public-bot-demo.sh`, a self-contained demo of the deterministic part on one machine
with synthetic data. It gives a gateway, the owner's device, a caller, the owner's worker device and the private
calendar service their own directories in one new temporary directory. The gateway and the calendar service listen
on `127.0.0.1` ports chosen by the operating system, calendar dates are computed from today, and ids are read from
each command's JSON output. It never uses Tailscale, an existing Yobiru data home, a real calendar or any other
address. When it ends, even after an error or Ctrl-C, it stops its services and deletes the temporary directory.
It needs bash and Python 3.8+, was tested on Linux, and takes about ten seconds. Run it from the plugin's folder
(`plugins/yobiru` in a source checkout):

```sh
sh examples/public-bot-demo.sh
```

It prints one line per step and stops with an error if a step does not behave as described. These are the
`yobiru` commands it runs, apart from the `status` and `history` reads that check each step. `Y` is the plugin's
`scripts/yobiru` and `T` is the new private directory. The values in capitals come from earlier output: `PORT` is a
free local port, `URL` comes from the gateway's `{"listening": ...}` line, `BOT_ID` from `bot create`, `ACTOR_ID`
from `owner contacts --pending`, `CAL_URL` from the calendar service's `listening` line and each `MESSAGE_ID` from
`chat` or `slots`. `DAY` is the date two days from today (UTC).

```sh
# 1. the gateway
sh "$Y" gateway --gateway-home "$T/gateway" init --port "$PORT"    # writes $T/gateway/owner.token
sh "$Y" gateway --gateway-home "$T/gateway" serve &                # prints {"listening": URL}
# 2. the owner's device creates and publishes the bot
sh "$Y" --home "$T/owner" public owner login "$URL" --token-file "$T/gateway/owner.token"
sh "$Y" --home "$T/owner" public bot create --handle assistant --name "Demo assistant"
sh "$Y" --home "$T/owner" public bot publish assistant
# 3. a visitor (no Tailscale) asks a first question: queued, that is stored and not answered yet
sh "$Y" --home "$T/caller" public enroll "$URL" --name "Demo caller"
sh "$Y" --home "$T/caller" public add "$URL/@assistant"
sh "$Y" --home "$T/caller" public chat assistant "What can you do?" --faq
# 4. a request, answered with an explicit grant (separate from follows and friends)
sh "$Y" --home "$T/caller" public request-friend assistant --note "Hello from the demo"
sh "$Y" --home "$T/owner" public owner contacts assistant --pending
sh "$Y" --home "$T/owner" public owner contact assistant "$ACTOR_ID" approve --capabilities faq,leave_message,calendar_slots
# 5. the owner's private calendar service
sh "$Y" calendar-service init --dir "$T/calendar" --gateway "$URL" --bot "$BOT_ID"
sh "$Y" --home "$T/owner" public verifier bind assistant --token-file "$T/calendar/verifier.token"
#    (the demo now sets the rules in $T/calendar/service.json to every day 09:00-17:00 UTC, one-hour meetings
#    starting on the hour, no notice and no buffer, and writes two synthetic busy events on DAY to events.json)
sh "$Y" calendar-service serve --dir "$T/calendar" &               # prints {"listening": CAL_URL}
# 6. the caller asks for meeting times and leaves a message while no worker runs
sh "$Y" --home "$T/caller" public slots assistant --duration 60 --after "$DAY" --before "$DAY"
sh "$Y" --home "$T/caller" public chat assistant "Please tell the owner hello" --leave-message
# 7. the worker device: its own per-bot credential, the deterministic responder, one pass over the queue
sh "$Y" --home "$T/owner" public worker bind assistant --token-file "$T/worker.token"
sh "$Y" --home "$T/worker" public worker login "$URL" --token-file "$T/worker.token" --handle @assistant
sh "$Y" --home "$T/worker" public responder init "$BOT_ID" --calendar-url "$CAL_URL" \
    --calendar-token-file "$T/calendar/access.token"
sh "$Y" --home "$T/worker" public worker run "$BOT_ID" --once
# 8. the results: the FAQ answer, three offered times (nothing booked) and the left message
sh "$Y" --home "$T/caller" public status MESSAGE_ID          # for each of the three message ids
sh "$Y" --home "$T/worker" public worker inbox "$BOT_ID"
# 9. block (revoke): the caller's later reads are refused
sh "$Y" --home "$T/owner" public owner contact assistant "$ACTOR_ID" revoke
sh "$Y" --home "$T/caller" public history assistant           # 404 not_found
```

The demo also checks that the synthetic event titles, locations and attendees appear only in the calendar
service's `events.json`, never in the gateway's, the worker's or the caller's files. It does not cover chat: the
supervised chat flow is covered by the automated tests (`tests/test_friends_client.py` uses a synthetic host
process, not an agent).

## HTTP API (version 1)

JSON bodies (no duplicate keys, no unknown fields); credentials in `Authorization: Bearer ...`. Unknown routes answer
`404`, query strings `400`.

| Route | Credential | Purpose |
|---|---|---|
| `GET /@{handle or bot_id}`, `GET /v1/bots/{ref}`, `GET /.well-known/yobiru`, `GET /v1/health` | none | card, gateway description, liveness |
| `POST /v1/actors` | none (optional client-generated actor credential) | enroll a visitor |
| `POST /v1/enroll` | client-generated owner credential | enroll a new owner and source bot |
| `GET /v1/actor`, `GET /v1/bots/{ref}/contact`, `POST .../contact-request` | visitor or source | identity, relationship, permission request |
| `POST /v1/bots/{ref}/follow`, `.../unfollow`, `GET /v1/following` | source | the follow graph |
| `POST/GET /v1/bots/{ref}/conversations`, `GET/POST /v1/conversations/{cid}[/messages]`, `GET .../operations/{op}`, `GET /v1/messages/{mid}` | visitor or source | conversations, sends, reconciliation, results |
| `/v1/owner/...` (bots, publish, contacts, followers, following, sources, workers, connector, check, verifiers, conversations, reviews) | owner | administration |
| `POST /v1/owner/messages/{mid}/review`, `.../reveal`, `.../resolve` | owner | supervised gates and uncertain work |
| `POST /v1/worker/claim`, `POST /v1/worker/messages/{mid}/start|finish|release` | worker | the deterministic work loop (finish also takes native replies) |
| `GET /v1/worker/jobs`, `POST /v1/worker/messages/{mid}/claim`, `POST /v1/worker/checks/{chk}` | native binding | your agent's jobs |
| `GET /v1/connector/jobs`, `POST /v1/connector/messages/{mid}/wake`, `POST /v1/connector/checks/{chk}/wake` | connector | the wake ledger |
| `POST /v1/capability/redeem` | verifier | calendar slots or one resource for one started job |

The calendar service exposes `POST /v1/suggest`, the resource service `POST /v1/resource`; both only on 127.0.0.1.

## What has and has not been verified

| Evidence | Status |
|---|---|
| Automated tests | Real gateway, connector, resource and calendar services, deterministic worker and callers as separate processes on loopback with synthetic data; a synthetic host process plays the native agent (protocol only). See `docs/VERIFICATION.md` in a source checkout. |
| Real agent, local | In a maintainers' test with an earlier snapshot of this branch, a real local Codex agent answered two original freeform chat turns through the public share link, a scoped native binding, the inbound and exact-draft reply reviews, and one Yobiru conversation; the caller read both replies with the CLI, and the second turn used the first through the transcript. Later fixes (enrollment retries, wake classification, ordering, resource checks) are covered by the automated tests, not by that run. Not Grok, not a plugin installation, not host isolation. |
| Grok Bot | Not done: the selected Grok Bot declined to run the candidate CLI until its owner approves that in the Grok main chat. No Grok dialogue has completed. |
| Isolation | Not established for any native host. Same-account Grok bots share files, tools, logins, apps and memory; supervised review is the boundary this version enforces. |
| Deployed | No production deployment. The earlier local Codex test reached a temporary gateway through a temporary public HTTPS staging proxy; no Funnel, production proxy or real calendar was used. |

Not verified or not provided: federation between gateways, per-caller native threads, automatic native answering,
real calendar exports, Windows and macOS runs, execution on Python 3.8 (syntax only), and waking a sleeping host
other than through a configured route.
