# What has been verified

Yobiru release verification. This page separates what has actually been run from what has not. It is not a
security guarantee: the tests show the behaviors listed below, nothing more.
Dated release results refer to the named release trees, not a later development branch or an installed host.

## 2026-10-26 development validation (after 0.7.14)

These results belong to distinct source snapshots; they are not a claim that every suite ran on the final merged tree.

| Snapshot | Environment and result |
| --- | --- |
| PR #51 `e272431` | [CI run 40242634870](https://github.com/chekusu/yobiru/actions/runs/40242634870): macOS and Ubuntu both passed 834 main-suite tests and 209 bridge tests; main-suite skips were 54 on macOS and 6 on Ubuntu. |
| Integrated features `39b7b522` | Mac, CPython 3.12: 876 main-suite tests (53 skipped) and 209 bridge tests passed; package check passed. |
| Integrated features `0f980208` | Linux: 883 main-suite tests (2 skipped) and 209 bridge tests passed. |

The subsequent test-fixture changes `776135e` / `e272431` passed their affected focused tests. The CI row above covers the PR #51 tree; it does not include all later integrated features.

Muse tests use synthetic Tailscale identities and a real loopback receiver to verify outbound pull requests, original-ID recovery, results and attachments without a reverse connection. Native Muse acceptance remains blocked by the account's age-verification screen; no real Muse round trip or directory approval is claimed. See [MUSE.md](MUSE.md).

Claude bridge tests use a fake Claude executable to verify durable launch intent, exact-session reconciliation, no relaunch after uncertain outcomes, current-policy checks and installer preservation. Native Claude account startup and Remote Control visibility have not been tested. Recovery tests exercise saved identity, expiry and outcome checks before resuming an existing request. These automated results do not establish native-host acceptance. See [CLAUDE_BRIDGE.md](CLAUDE_BRIDGE.md) and [RESTART_RECOVERY.md](RESTART_RECOVERY.md).

Production deployment of the immutable `v0.7.14` release was verified separately. The new `v0.7.15` release packages the Muse/Claude adapters and later recovery changes described above. Native Muse account round-trip, directory review and native Claude account/Remote Control acceptance remain separate gates; this page records automated and hosted evidence only.

## Native Grok Bot acceptance run (after 0.6.0-beta.1)

On 2026-10-22, `tools/friends_acceptance.py --host remote` ran the Beta guide's acceptance checklist with a real
agent. The admin device was the owner's Mac (macOS), on commit `fbcbd33` of this branch, whose plugin is identical
to `v0.6.0-beta.1`. Its gateway and resource service were temporary and reached over two Tailscale Serve HTTPS routes
(tailnet only). The native binding of the test bot was held by one owner-controlled Grok Bot cloud computer (Linux)
with the 0.6.0-beta.1 plugin from the release. That agent followed `RUNBOOK.md` through the CLI after its owner
approved in the Grok main chat. The token files reached it as Yobiru attachments, never in message text. The callers
were CLI homes on the Mac, not other people's agents.

- First run, 11:04 to 11:18 UTC: **13 of 14 checks** passed. Item 4 failed because every resource read returned
  `503 not_configured`: the run's folder was under macOS `/var/folders`, and `/var` is a link to `/private/var`. The
  resource service now resolves its folder at start (`fix: serve resources from a folder whose path passes through
  a link`, with two unit tests); the whole suite passed **463 tests** (1 skipped) on Linux. Cleanup passed.
- Second run, 12:40 to 13:00 UTC, same commit, with `TMPDIR` set to its real path so the folder had no link:
  **35 of 35 checks** passed, all twelve items, exit 0. The owner credential made 6 inbound admissions and 4 reply
  approvals. The binding was revoked, the bot unpublished and both routes removed afterwards.

Not shown by these runs: the resource fix on macOS (the second run avoided the link instead), a Grok Bot reached by
someone else's agent or through a public URL, waking the agent through a route (it was told when to act), a
restart during a run, the Yobiru skill instead of the CLI, host isolation, Muse, Dots and Windows.

## Acceptance checklist driver (after 0.6.0-beta.1)

On 2026-10-21, on a branch based on the `v0.6.0-beta.1` tag (a new maintainer tool and its tests; no runtime,
plugin or gateway change; nothing released), on Python 3.13 on Linux, with synthetic homes and data and loopback HTTP:

- `python3 tools/friends_acceptance.py` (its scripted host) passed **43 checks** covering all twelve items of the
  Beta guide's acceptance checklist, through the plugin's own `scripts/yobiru` launcher. A run whose scripted host
  put the friend's export into the follower's reply failed item 4 and still cleaned up completely.
- `python3 -m unittest test_friends_acceptance` passed **2 tests**: the whole checklist with the scripted host, and
  a remote host that never acts, which ends as a timeout (exit 3) after a complete cleanup that leaves only
  `RUNBOOK.md` in the handoff folder. The whole suite, `python3 -m unittest discover` in `tests/`, passed **461
  tests** (1 skipped).
- A rehearsal of `--host remote` on the same machine (`http://127.0.0.1` routes instead of HTTPS, and a scratch
  script, not an agent, that ran `RUNBOOK.md` literally in its own home) passed **35 checks**. Its first attempt
  showed that the runbook must `chmod 600` the copied token files, because the CLI refuses any other mode; the
  runbook now does.

Not tested here: a native Grok Bot or any other real agent following the runbook, a restart of the agent's device
during a run (the runbook's resume section), Tailscale Serve or any HTTPS route, macOS (the intended admin device),
Windows and Python 3.8 execution (syntax only).

## 0.6.0-beta.1 release preparation

On 2026-10-21, on the release branch for 0.6.0-beta.1 (the Friends schema-3 work below plus the version and
prerelease changes; local commits only, nothing pushed, published, deployed or installed), on Python 3.12, with
synthetic homes and data, injected release metadata, loopback HTTP and subprocess stdio only:

- `python3 -m unittest discover -s tests -p 'test_*.py'` passed **459 tests** on the committed tree (`c98ec0a`).
  A run just before that commit had one failure: the guide link check read the changelog's link pinned to the
  `v0.6.0-beta.1` tag as a relative path. The check now accepts tag-pinned links and checks their anchors.
- `python3 -m unittest test_codex_bridge test_codex_queue_client test_codex_resident test_install_codex_bridge
  test_install_codex_bridge_linux` passed **194 tests** on the same tree.
- `python3 tools/build_plugin.py sync` and `check` passed; `release` wrote `yobiru-0.6.0-beta.1-plugin.zip`,
  `yobiru-0.6.0-beta.1-marketplace.zip` and `SHA256SUMS`, and every manifest in them carries `0.6.0-beta.1`.
- The Beta guide's archive commands ran as written against the built plugin archive (the `SHA256SUMS` line,
  extraction, `version`), and the unpacked launcher's `install-runtime --upgrade --apply` refused to replace a
  `0.6.0-dev.1` runtime.

What the new tests cover:

| Area | Behavior shown |
| --- | --- |
| Prerelease updates (`test_update.py`) | The stable channel never offers `0.6.0-beta.1`, even when GitHub's prerelease flag is missing; the preview channel offers its exact tag; a Beta installation is `ahead` on stable and `current` on preview, and a final `0.6.0` supersedes it; `0.6.0-dev.1` sorts above the Beta and is never offered it. Steps for a prerelease name its tag page, archive, `SHA256SUMS`, a version check and a Codex marketplace pinned with `--ref`, never a branch upgrade; a newer runtime is never advised to be replaced; prerelease notices carry `prerelease: true` and the opt-in, stable ones neither; a stable maintenance run holds a queued prerelease notice, says why, and a preview run sends it. |
| Runtime downgrade guard (`test_cli.py`) | `install-runtime --upgrade` refuses to replace a newer (development) runtime and writes nothing; with `--allow-downgrade` it replaces it and keeps it as the backup; an older runtime needs no flag. |
| Build (`test_build.py`) | `release` writes exactly this version's two archives and a matching `SHA256SUMS`, with the exact version in every manifest and in the bundled runtime; the product version must be SemVer 2.0 (`0.6.0-beta.01`, `0.6.0-beta..1`, `0.6.0-` and others are refused); a prerelease must link the privacy notice in its own tag, not `main` or an earlier tag. |
| Version line (`test_public_bundle.py`) | The plugin README's version statement keeps a dotted prerelease whole (`0.6.0-beta.1`, not `0.6.0-beta`). |

Not tested here: the live GitHub release list, publishing, installing the archive on another machine, the Codex
marketplace route pinned with `--ref`, native hosts (Grok Bot, Muse, Dots), Windows, macOS and Python 3.8 execution.
`selftest.sh` was not run.

## Friends schema 3: routing, delivery evidence, decisions, loop budgets, attachments and MCP (in 0.6.0-beta.1)

On 2026-10-21, on a development branch based on the delivery reliability branch (version unchanged; nothing
released, deployed, installed or restarted), on Python 3.12, with synthetic homes and data, loopback HTTP and
subprocess stdio only:

- `python3 -m unittest discover -s tests -p 'test_*.py'` passed **448 tests** (in 260 s).
- `python3 -m unittest test_codex_bridge test_codex_queue_client test_codex_resident test_install_codex_bridge
  test_install_codex_bridge_linux` passed **194 tests**.
- After that run, one MCP test was added (files withdrawn, as seen through the tools), unused test imports were
  removed and documentation was edited, without any runtime change. The affected suites (`test_friends_mcp`,
  `test_friends_decisions`, `test_friends_migration`, `test_public_bundle` and `test_build`) then passed
  **55 tests**.
- `python3 tools/build_plugin.py sync` and `check` passed; `git diff --check` was clean; the changed Python files
  parse with the Python 3.8 grammar (parsing, not execution on Python 3.8).

What the new tests cover (real `bot_gateway.py` subprocesses, the real CLI, and `bot_client.py mcp` as a real
stdio subprocess; the native host and connector are played by the tests through their credentials):

| Area | Behavior shown |
| --- | --- |
| Routing and directory (`test_friends_routing.py`) | A renamed bot keeps its saved alias and conversation and `contacts --refresh` reports the new handle, while the old handle names nothing; a reused handle never retargets a saved contact; a rebind keeps the bot and fences the old binding; a renamed handle stays reserved to its owner; the owner directory shows bindings, hosts and session notes that no card, caller view, job view or MCP lookup contains. |
| Delivery evidence (`test_friends_routing.py`) | Declarations are validated and stay claims; the card reports `binding_configured`, never online; a bare acknowledgement or an accepted wake alone is not wake evidence, a correlated one is; quiet-wake correlation needs an unused credential; evidence only moves forward and keeps the winning event's route; a replaced binding, 8-day-old evidence and a future time read `unknown`, `stale` and `unknown`; lifecycle stages stay separate, and a GET is recorded as served, only an explicit acknowledgement as acknowledged. |
| Decision receipts (`test_friends_decisions.py`) | Each review writes one idempotent receipt whose digest covers the record; review and receipts are the owner credential's only; a decision id authorizes nothing; a caller's projection hides rejected and unreleased draft hashes (no oracle) and verifies itself; other callers, owners, issuers, revoked callers and trust receipts get the same 404; history stays while the current authorization ends; trust receipts follow the current state and are never reused across generations; an admission past its deadline is not current even before any sweep (fake clock, in-process); attachment names and contents are bound into both the request and the draft hash. |
| Loop budgets (`test_friends_loops.py`) | Exact retries succeed after a budget is exhausted and are charged once, changed ones (text, kind, cause) are refused before any charge; the pair budget survives new and deleted conversations and a restart; `human`, `reset` and `hop` are refused and only the owner continues; 8 concurrent admissions are charged exactly, 4 concurrent identical retries store one message; continuations are bounded, idempotent, refused when changed or with a stale anchor, and voided by a rebind or a narrowed grant; causes are validated and hops bounded; an old client's notice is a direct message charged like any other, what an owner sends as their bot with the owner credential is never charged (and only in that bot's own conversations), and the old notice inbox answers an empty list with a note. |
| Direct messages (`test_friends_direct_messages.py`) | A real schema-7 database with unread and read notices and left messages from visitors, friends and trusted contacts migrates to schema 8 once: each becomes a direct message (unread ones wait in the inbox or the message requests by their sender, read ones are `completed` with code `read`, an expired left message is offered again), `leave_message` in policies, grants and ceilings becomes `chat`, and the retention index exists; the well-known card says messages are one kind; an older client's `message`, `notice` and `leave_message` are read as a direct message without a deadline; a retry from before the upgrade, with its old request hash, is the same message; an unread direct message waits until the 30-day retention ends, then is deleted; the old `--leave-message` and `--notice` flags send a plain message and `--faq --notice` is refused. |
| Attachments (`test_friends_attachments.py`) | `files` is checked by presence (also for an empty file) on sends and replies; invalid names, contents, counts, sizes, duplicates and extra fields fail before any effect; daily and retained quotas (the caller's whole exposure, reply included) are prechecked, a retry is free, and deleting a conversation releases stored bytes; before admission the worker learns nothing about attachments; withdrawing `files` withholds them from every view and transcript and widening again revives nothing; the sender CLI refuses unsafe roots, hidden, secret-looking, oversized, non-UTF-8, control-character, device, FIFO, directory, symlinked (final or intermediate), traversing and outside paths, and a replaced root; the recipient revalidates every item before writing and never follows a planted link; a full CLI roundtrip (send, admit, claim, export, reply with a file, approve, receive, save, verify), then a revoked caller saves nothing new. |
| End-to-end encryption (`test_friends_e2e.py`) | X25519 and ChaCha20-Poly1305 match the RFC 7748 and RFC 8439 vectors (and the `cryptography` package when installed); envelopes open only for their recipients, only against the real sender's key and context, and a forged sender is refused; through the CLI a chat with a file, the owner's reveal and review, the host's claim and transcript, the draft, the released reply, `verify`, an owner message, a decline note and an FAQ answered by the deterministic responder reach the gateway database only sealed and open on the right devices; keys are made at `bot create`, `enroll-bot` and `enroll` and handed over next to the binding credential; the gateway refuses a plain reply to a sealed message, a body not sealed for the bot's current key, a malformed envelope, a sealed body to a bot without a key, and plain text under `require_e2e`; a body moved to another conversation, or the caller's own message shown as the reply, is reported unverified; a rotated key fails sends until `e2e trust`, and a device without the new key reports what it cannot open. |
| MCP (`test_friends_mcp.py`) | Version negotiation, notifications (202, empty), ping, unknown methods and cursors; 405 without SSE, Origin (403, an allowed origin passes), missing, malformed, unknown and revoked credentials (401), owner, connector, verifier, ticket and deterministic-worker credentials (403), Content-Type (415), Accept (406), batches, response objects, parse errors, protocol header and body size; a disabled endpoint; 17 caller and 8 host tools (no notice tools) with closed input schemas, output schemas and annotations, no administration tools, and isolation between roles; invalid arguments and other-gateway links refused before anything runs; idempotent sends, a conversation that must belong to the named bot (chat, FAQ and an existing op key), and a retry after the handle moved that reaches the same bot; the supervised flow with attachments, acknowledgement, decisions, verification, check-in, and a message that needs no reply showing up as a job (kind `notice` refused); withdrawn files and revocation as seen through the tools; the stdio proxy answering one JSON line per request with clean stdout and no credential, refusing `--host` next to the owner credential and `--as` with `--host`, and reporting an unreachable gateway as an unknown outcome. |
| Migration (`test_friends_migration.py`) | A real schema-2 database (from `tests/fixtures/bot_gateway_v2.py`, the gateway at 961f065) is refused by `serve`, migrated once (idempotent), and keeps capabilities, follows, the queued chat and its request hash, wake evidence and no invented decision or declaration; a failing migration rolls back to schema 2 completely. |

Independently of the suite, the maintainers ran the official MCP SDK client against a local gateway over HTTP and
through the stdio proxy, including the packaged launcher from a built plugin archive in a path with spaces (17
caller and 10 host tools, role and protocol negatives, idempotence and the replay after a handle moved), and
independent identity, delivery and attachment reviews. Not tested: a hosted MCP platform (Grok, Muse, Dots), an
OAuth-only host, a real native agent answering through MCP, file attachments on Windows (they need POSIX
`dir_fd`), execution on Python 3.8, a production gateway or real Tailscale. `selftest.sh` was not run.

## Delivery and maintenance reliability (in 0.6.0-beta.1)

On 2026-10-21, on a development branch based on the Friends branch (version unchanged; nothing released,
deployed, installed or restarted), on Python 3.12, with temporary homes, fake clocks, mocked transports and
loopback sockets only:

- `python3 -m unittest discover -s tests -p 'test_*.py'` passed **394 tests**. The first full run had one error:
  a new supervisor test let its worker threads keep writing while its temporary home was removed. The test now
  stops and joins its workers; the rerun passed all 394.
- `python3 -m unittest test_codex_bridge test_codex_queue_client test_codex_resident test_install_codex_bridge
  test_install_codex_bridge_linux` passed **194 tests**.
- `python3 tools/build_plugin.py sync` and `check` passed; `git diff --check` was clean.

What the new offline tests cover (`tests/test_maintenance.py`, `tests/test_doctor_maintenance.py` and new cases
in `test_codex_bridge.py` and `test_install_codex_bridge_linux.py`):

| Area | Behavior shown |
| --- | --- |
| Supervision | A worker that exits (including `SystemExit`) gets exactly one replacement and stays reported until a full cycle succeeds; a worker alive in a simulated 660+ second cycle is reported stalled, never restarted or cloned; a thread that cannot start leaves the listener running, degraded, and is retried. |
| Phase isolation | A failing inbox recovery does not stop expiry (persisted `timed_out`) or response delivery; the cycle is not a full success. `request maintain` attempts every phase and reports `busy` or the failed phase. |
| Delivery | One flush per data home across processes (`busy`, nothing sent); lock errors other than contention are failures; an offline backlog does not starve a healthy peer; passes are bounded; a ledger failure after a possible POST keeps the same response ID pending and the peer records one event. |
| HTTP bounds | A valid prefix followed by a stall, a trickling peer, an oversized body and a body shorter than its Content-Length all fail closed. |
| Attachments | Expired grants are deleted, active and in-preparation ones kept, failed deletions and unreadable metadata counted as failures; a missing outbox fails the cycle without ending the worker. |
| Heartbeat | 0600, atomic, metadata only; a write or directory-sync I/O error degrades health while the previous record stays; recovery writes a genuinely completed cycle; a new process never inherits an earlier heartbeat. |
| Ledger evidence | Start, progress and final times are written with their responses and survive restart; migration backfills in response order even after a clock step back; the projection joins the exact final response of the same request; older writers and invalid times never become evidence; committed WAL writes are seen without changing rows; unreadable ledgers are unknown, never an empty backlog. |
| Bridge | The queue receipt and acceptance time are kept and nothing is resent across a day and a restart; expiry after official acceptance is flagged `deadline_passed_without_final` without a resend, ack or invented result; a running request is never queued again; a history marker means seen, not started, with one history read per pass; durable lifecycle evidence outranks an old transport state; rows close only on positive evidence and never borrow evidence across homes or threads; `status` and `diagnose` create and migrate nothing; daemon phases are isolated; 429-like failures stay `uncertain`; an unavailable desktop is known-unsent and its capability gaps are reported; two dispatchers send once; old rows keep their states after migration and installer upgrades. |
| Doctor | Transport reachability and maintenance readiness are separate; degraded, stalled, starting, older-runtime, malformed and foreign heartbeat cases are reported distinctly; the never-attempted warning uses valid timestamps of due responses only. |

Not tested here: a real Codex desktop or app-server recovering, a real rate limit or unsubscribed thread, a real
network outage, real Tailscale, or a production receiver. `selftest.sh` was not run. These tests establish local
lifecycle and diagnostic behavior, not that any of those real conditions recover automatically.

## Public bots and Friends (in 0.6.0-beta.1)

On 2026-10-20, on a development branch based on 0.5.3 (version unchanged; nothing released, deployed to
production or installed), on Python 3.12:

- On an intermediate snapshot, taken before the final client, resource and documentation fixes,
  `python3 -m unittest discover -s tests` passed **346 tests**, and the bridge, resident and installer suite
  passed its **146 tests** (that code is unchanged since). A later full run on another intermediate snapshot
  (347 tests) had one failure: a check that the bundled guide's walkthrough runs the demo, which was restored.
- On the final snapshot, the affected suites (`test_friends_gateway`, `test_friends_client`,
  `test_public_gateway`, `test_public_calendar`, `test_public_client`, `test_public_e2e` and
  `test_public_bundle`) passed **127 tests**; `python3 tools/build_plugin.py check`, `git diff --check` and
  Python 3.8 syntax parsing of every Python file passed (parsing is not execution on a Python 3.8 interpreter).
  The full suite was not rerun on the final snapshot.

New tests (real gateway, connector, deterministic worker, calendar and resource services and callers as separate
processes on loopback ports, synthetic homes and data; where a native agent is needed, the synthetic
`tests/support/synthetic_host.py` process plays it through the real CLI, which proves the protocol only):

- `test_friends_gateway.py` (real HTTP): retry-safe visitor and source-bot self-enrollment that can never claim an
  existing bot, owner or handle, also after enrollment closes; credential realms and identity taken only from the
  credential; share-link cards that equal the profile and expose no private data; directed follows, exact reverse
  edges for friends, idempotent and concurrent follows, block dominating both edges; directional presets, a
  per-contact ceiling surviving mutual follows, explicit grants surviving unfollow, and fail-closed scopes;
  inbound review hiding bodies from the binding and the owner's own views, drafts invisible until an exact-hash
  approval that rechecks permissions, rejections releasing no host text; per-conversation order and a single
  in-flight turn, also behind an uncertain turn and under concurrent claims; unfollow cancelling queued and
  in-flight work and withholding earlier results and owner messages from readback and transcripts; rebind,
  revoke and lease expiry making native work uncertain instead of requeued, with fenced late answers; the wake
  ledger never repeating an intent-only or uncertain wake; check acknowledgements kept apart from correlated route
  evidence and monotonic check results; an owner removing a follower, republishing that revives no queued,
  in-flight or earlier answer, a wrong source bot (also one of the same owner) refused another bot's
  conversation, injected display names and bodies kept out of every metadata listing, replayed and conflicting
  drafts, and a hard gateway restart in the middle of a native job; migration of real first-version databases
  (made by `tests/fixtures/bot_gateway_v1.py`) preserving every contact's capabilities, and a rolled-back failed
  migration.
- `test_friends_client.py` (the CLI): connecting from only a share link, source bots following and following
  back, two supervised chat turns through a connector `exec` route and the synthetic host with transcript
  continuity and no duplicate wakes, binding-check evidence, connector-ledger recovery after a crash, wake
  transport classification (only pre-send failures count as refused), resource exports released only to the
  granted caller and never after a removal, replacement or title change before the final check, op keys bound
  to one identity, a native rebind validated before any change, and enrollment credentials kept after a lost
  answer, a later refusal or a concurrent late refusal.
- The earlier `test_public_*.py` suites, adapted to the new defaults (chat for visitors) and the new policy
  format, still cover the deterministic worker, calendar service, transport, retries and the archive demo.

Separately, a maintainers' test drove a real local Codex agent through an earlier snapshot of this branch: two
original freeform chat turns over a temporary public HTTPS staging route, a scoped native binding, inbound and
exact-draft reply approval and one Yobiru conversation, read back by the caller CLI; the second turn used the
first through the transcript. Later fixes (enrollment retries, wake classification, ordering, owner-message
generations, resource checks) are covered by the automated tests above, not by that run. No Grok Bot dialogue
has completed: the selected bot waited for its owner's approval in the Grok main chat before running the
candidate CLI. None of this establishes host isolation, production deployment, real calendar exports, macOS or
Windows runs, or use from an installed plugin in a native host.

## Public bot contacts, first version (in 0.6.0-beta.1)

On 2026-10-19, on a development branch based on 0.5.3 (version unchanged; nothing released or deployed),
`python3 -m unittest discover -s tests` passed **294 tests** (the 228 existing plugin tests plus 66 new
public bot tests) on Python 3.12, and the bridge, resident and installer suite passed its **146 tests**.
`python3 tools/build_plugin.py check`, `git diff --check` and Python 3.8 syntax parsing of all 52 Python
files passed. Syntax parsing is not execution on a Python 3.8 interpreter.

The new tests run a real gateway, the constrained worker, the private calendar capability service and callers
as separate processes on loopback ports with synthetic homes and synthetic calendar events:

- `test_public_e2e.py` (through the unpacked plugin archive): owner bootstrap, publish, caller enrollment,
  visitor FAQ while no worker exists (durable `queued` receipt), friend request, approval with an explicit
  capability list, slot query and a direct message while the worker is absent, a killed and restarted gateway,
  a dedicated per-bot worker credential (no owner credential on the worker device), deterministic answers,
  conflict-aware slots, owner reply, idempotent retries across processes and restarts, sentinel-free gateway
  and caller state, an untouched private tailmsg installation in the same data home, relocation to a new
  worker device that fences the old one with the same bot id, and revocation and unpublish through the CLI.
  A running private receiver with a wake-everything adapter shares the worker's data home: no public message
  reaches its inbox or adapter, and its `/info` reveals nothing about the bot.
- `test_public_gateway.py` (real HTTP): local-only bootstrap that never overwrites, credential realms that
  never substitute for each other, other owners, handle rules and renames, allowlisted public cards, no
  enumeration or private-receiver routes, cross-contact and cross-bot isolation, per-friend capability subsets,
  spoofed identity fields, revocation of queued and in-flight work and later reads, result withholding after
  scope removal, demotion and policy narrowing, mute and unpublish semantics, atomic concurrent claims,
  expired and stale leases, a finish whose body arrives after its lease expired, worker replacement,
  malformed, oversized, slow and concurrent connections, deadlines, per-caller, per-bot and gateway-wide
  admission limits, unknown kinds and scopes, receipts and claims across hard restarts, rejection with a
  cooldown, and owner deletion and the retention purge removing stored bodies from the database file.
- `test_public_calendar.py`: conflict-aware suggestions (busy, cancelled, transparent and all-day events,
  buffers, notice, horizon, work days, fixed offsets, daylight-saving transitions), malformed calendars that
  refuse, ticket authorization through the gateway, levels, budgets and budget-free retries, revocation while
  an answer waits for or holds its lock, stale cached answers after rule, calendar or age changes, encoded
  sentinel checks of every caller-visible surface and stored file under hostile prompts, an unreachable
  capability service reported as unconfirmed, and the worker's configuration and process inputs.
- `test_public_client.py`: the HTTP client's total deadline for slow close-delimited bodies, refused
  redirects, credentials bound to their exact gateway, refused plain `http://`, op-key retries, lost requests
  and lost responses through a dropping proxy, crashes after a real commit, lookups during an in-flight
  send, refusals that cannot settle earlier uncertain sends, large multibyte send logs, the open-outcome
  quota, concurrent processes and kernel locks, private file modes, the per-bot worker lock and lease
  revalidation before any computation.

These tests do not establish a deployment behind a real HTTPS proxy, behavior with real calendar exports,
macOS or Windows runs, OS-level isolation between same-user processes, or use from a native Grok Bot,
Codex or Cursor session. See [public bot contacts](PUBLIC_BOTS.md#what-has-and-has-not-been-verified).

## Cloud-computer setup guidance (0.5.3)

On 2026-10-19, the 0.5.3 release candidate passed **228 plugin tests and 146
bridge/resident/installer tests (374 total)** on Python 3.12. Build consistency,
public-file hygiene, `git diff --check` and Python 3.8 syntax parsing passed.
Syntax parsing is not execution on a Python 3.8 interpreter.

The package includes updated setup/Grok Bot skills for inspecting and preserving Tailscale
identity state, keeping existing Yobiru data, and configuring supported startup recovery.
The runtime changes only its reported product version. No automatic state migration,
startup installer, discovery fix or live receiver identity refresh was added.

These isolated tests verify packaging and existing protocol/installer behavior. They do not
establish that a chosen cloud directory survives a rebuild, encrypted identity state is portable,
or a host restores network login and agent wake-ups after a real VM image replacement. See
[the state and startup guide](GROK_BOT.md#preserve-tailscale-state-and-startup).

## Resident session workflow (0.5.2)

On 2026-10-19, the 0.5.2 release candidate passed **228 plugin tests and 146
bridge/resident/installer tests (374 total)** on Python 3.12. The build consistency and
public-file hygiene checks, `git diff --check`, and Python 3.8 syntax parsing passed.
Syntax parsing is not execution on a Python 3.8 interpreter.

This release updates the dispatcher template, inbox/wake/upgrade skills and maintenance
documentation. Runtime code changes only its reported product version. The tests rerun
packaging, isolated request lifecycle, service, bridge, installer and rollback behavior;
they do not establish that a live host follows every workflow instruction or keeps a
business session alive after handoff. No automatic business scheduler or resource-lock
implementation was added. A stable resource key must still use the host's actual lock or lease.

Existing custom dispatcher files are preserved by the installer. Applying these rules requires
a reviewed merge into the configured file and verification of the installed skills and host
capabilities. Keep newer development runtimes intact and report policy adoption separately
from software versions. See [resident session maintenance](DISPATCHER_SESSIONS.md).

## Restart and identity recovery observation (2026-10-19)

A reachable Grok Bot receiver on 0.5.0 was displayed offline because discovery skipped its probe
when Tailscale reported `Online=false`. Separately, the running receiver reported an older node name
than fresh Tailscale identity checks, causing normal tracked-send preflight to reject the mismatch.
After an operator restart with the existing configuration, `/info` matched the current identity and
a request using the same stable hostname was accepted. This is limited recovery evidence, not proof
of final task execution, a tested rebuild, or a runtime fix in 0.5.2. The
[recovery record](RESTART_RECOVERY.md) separates observed facts, code causes, temporary remediation
and proposed tests. No private node names, addresses or message records are included here.

## Host packaging and marketplace verification (0.5.1)

The package adds the Grok Bot setup/recovery skill and declares the local execution needed by
OpenAI desktop hosts. Submission portals and test requirements were rechecked on 2026-10-15;
see [MARKETPLACE_SUBMISSION.md](MARKETPLACE_SUBMISSION.md). The package remains one implementation.
The dated test totals below describe earlier releases and must not be relabeled as a 0.5.1 run.

The 0.5.1 release tree passed **228 plugin tests** on Python 3.12, including the eight
new metadata/archive checks, packaged CLI execution and isolated loopback workflows.
The build consistency check and installed Codex compatibility-manifest validator passed.
The runtime change is its reported product version; the bridge implementation is unchanged.
These tests do not establish native Grok or ChatGPT desktop plugin installation.

Native Grok/Cursor plugin discovery, bundle script availability and in-app invocation need distinct
evidence from the manual runtime test below. The [Grok Bot guide](GROK_BOT.md) gives the test steps.
A submission receipt is not review approval; a prepared archive is not a submission. Public listing
and clean ChatGPT desktop installation are not established merely by valid metadata.

On 2026-10-15, the actual Grok Bot returned a new authenticated tracked result with the requested
challenge and `19 + 23 = 42`, and reported its installed CLI as 0.5.0. Its conversation's plugin
catalog did not expose Yobiru; release skill files existed on the computer but were not injected as
native skills. No supported install/import route was established through that conversation's tools.
This reconfirms manual runtime execution, while native in-app plugin testing remains **unverified**.
Do not use this observation to satisfy a declaration that native plugin installation was tested.

The same Grok Bot subsequently fetched the 0.5.1 candidate archive, verified its download hash
and bundled-source hash, extracted it into an isolated temporary directory, and successfully ran
`version`, `paths` and `doctor`. It returned the authenticated tracked outcome using the candidate's
`request respond` command against its existing data home. Its receiver remained on 0.5.0.
Native loading was reported **blocked**: no supported development plugin importer was exposed,
and writing the host's global user-skill library was outside that wake entry's permissions.
This is a passed manual-package smoke test, not native marketplace acceptance.

## Historical live acceptance after the 0.5.0 release

On 2026-10-13, after the isolated release suites completed:

- The actual Linux receiver and existing Codex bridge were upgraded to 0.5.0. With the owner's
  authorization, automatic replies were enabled while preserving the fixed session and other
  configuration. A real model turn reported progress and a verified final result for a harmless
  test request. Incoming and outgoing ledgers agreed, followed by local acknowledgement.
- A manually deployed Grok Bot receiver advertised 0.5.0 and `request-lifecycle-v1`. A real request
  from another machine progressed from received to running to completed, returning the requested
  harmless challenge and arithmetic result. An authenticated remote status refresh also succeeded.

These observations establish those particular runtime paths, not native Grok marketplace plugin
installation, every operating system, future availability or automatic recovery after a VM rebuild.
Private operational records are intentionally not included in this public repository.

## Tracked requests, receipts and final results (0.5.0)

The 0.5.0 release tree passed **220 plugin tests and 146 bridge/resident/installer tests
(366 total)** on Python 3.12.3. `tools/build_plugin.py sync` and `check` passed, including
bundled-source consistency, manifest/skill validation and public-file hygiene checks.
Python 3.8 syntax parsing passed; this is not a test run on a Python 3.8 interpreter.

- `tests/test_requests.py` adds 18 focused tests for durable receipt/final records, ack guards,
  authenticated peer and agent matching, duplicate and conflicting IDs, reordered responses,
  original-sender-only status access, unknown agents, timeout/late-result reconciliation,
  restart recovery and identical response retries. It also covers capability negotiation,
  unknown POST outcomes without replaying task requests, malformed/oversized results and refresh
  responses, invalid HTTP confirmations, read-only status/list, and deadline checks before wake
  retries. Protocol responses cannot be acknowledged into the ordinary inbox or manually woken.
- `tests/test_request_e2e.py` adds 10 tests using separate receiver subprocesses and real loopback
  HTTP with a fake Tailscale CLI. They cover receipt → running → final → ack, querying after receiver
  restarts, offline response queuing and recovery after both receivers restart with the same result
  ID, unknown timeout followed by a late result, existing `send --kind request` / `reply` behavior,
  result attachments fetched through `show` / `fetch RESPONSE_ID` with byte verification, and no
  ordinary-inbox delivery or reply loop for protocol responses. Further cases cover notifications
  without response records, offline/malformed status refresh, cross-peer status and forged-response
  rejection, already-expired requests never invoking a generic adapter, and explicit legacy opt-out.
- Seven new bridge tests run in both the base and inherited routing suite (14 executions). They
  check fixed `blocked` results without a model when replies are disabled, reply consent revoked
  or its configuration removed after ingestion, terminal/expired requests not starting a turn,
  expiry between ingestion and dispatch, the lifecycle contract injected into the session prompt
  (including incoming status lookup), and instructions not to reply to notifications. These use
  mocked lifecycle/client interfaces; the core persistence and HTTP paths are exercised separately
  by the tests above.
- An additional extracted-package CLI regression checks `request list` and `request status` against
  a configured temporary home without network calls or ledger/cache writes.
- The complete suites also rerun existing packaging, extraction, service-path, transport,
  permission-preservation, queue, resident, installer and rollback regressions. A passing bridge
  prompt assertion establishes the instructions sent to the model, not proof that every agent
  follows them or that a real model task completed.

**Verification boundary:** these request tests use isolated temporary data homes, simulated
Tailscale identities and local HTTP processes. The suites alone do not prove real tailnet delivery,
Grok Bot routine execution, native macOS/launchd or Windows behavior, or a production upgrade.
The separate dated live acceptance above supplies limited runtime evidence. Existing resident
tests use a fake WebSocket server; other historical live probes below retain their original dates.
A timeout means the actual task result remains unknown; it is not cancellation or a safe-retry
signal. Durable response retries do not establish exactly-once task execution, guaranteed offline
completion, or new authority to execute a received request.

## Service paths and transport diagnostics (0.4.4, historical validation)

The 0.4.4 release tree passed 191 plugin tests and 132 bridge/resident/installer tests (323 total)
on Python 3.12.3. Package consistency, archive checksums and integrity, exact bundled-source
hashes, private-marker checks and Python 3.8 syntax parsing also passed. The syntax check
does not establish execution on a Python 3.8 interpreter.

- Receiver and update-reminder templates preserve stable Python entry points, virtual
  environments and explicit receiver `--python` overrides. If Python reports a versioned
  Homebrew Cellar executable, an executable same-file path within that Homebrew prefix is
  selected when available. Existing installed definitions must be regenerated and reloaded.
- Eleven regressions cover saved plist interpreter paths after a simulated Homebrew upgrade
  and old-keg removal, candidate mismatches, and a real isolated virtual environment. These
  run on Linux; native Homebrew/launchd upgrades were not exercised. The separate macOS
  Codex bridge installer is outside this template fix.
- Send/reply, file fetch, health and discovery failures include peer/endpoint context and
  a bounded `tailscale ping` suggestion. Failed sends report their message ID and unknown
  delivery; no automatic resend or network restart is performed.
- Eight transport regressions include real loopback response loss after accepting one POST,
  packaged-CLI connection refusal, health/doctor/discovery diagnostics, local file errors,
  and bounded exception output that excludes malformed HTTP response contents.
- These checks establish diagnostic behavior, not the cause or repair of a stale Tailscale
  path. The reported cross-machine daemon-restart failure was not reproduced.

## Current-IP name resolution (0.4.3, historical validation)

- New sends read a fresh Tailscale snapshot. Short names match the DNS short name or reported
  HostName, selecting the unique online match over old offline entries after a reinstall.
- Multiple online matches, or multiple offline matches with none online, fail with an ambiguity
  error. Full FQDNs and explicit IPs stay exact; no suffix stripping or fuzzy matching.
- Sending, `agents --peer` and explicit upgrade notices share the selector. Replies, existing file
  grants and queued notices retain their original recipient identities.
- Nineteen regression tests cover changing IPs, stale offline nodes in either order, ambiguity,
  exact addresses, missing IPs, actual send-path recipient/agent routing, discovery and notices.
  The 0.4.3 plugin suite had 172 tests. These cases inject Tailscale snapshots and HTTP responses;
  they do not claim that a real Grok Bot VM was rebuilt or exchanged messages after a live IP change.
- A rebuild still needs a Tailscale login, its chosen stable hostname, and a running receiver.
  Yobiru cannot rediscover a machine that lost both its name and its identity without a new target.

## Maintenance fixes (0.4.2, historical validation)

The 0.4.2 release combined non-UTF-8 CLI output handling, minimum dispatcher sandbox permission
guidance, and the Linux bridge's persistent connection to its existing dispatcher thread.
That release tree passed 153 plugin tests, 132 bridge/resident/installer tests, and all
59 real Tailscale single-host self-tests. Package consistency, deterministic archives and the
private-marker hygiene scan also passed. The Tailscale self-test used temporary receivers on
non-production ports, without accessing the production inbox.
The CLI encoding tests use real cp1252/ASCII subprocess pipes on Linux; they are not native
Windows tests. Resident lifecycle and reconnect tests use an isolated WebSocket fake server;
separate real Codex app-server probes verified resume, connection persistence, restart recovery
and unchanged session permissions without running a model task. A subprocess regression also
checks that tilde-prefixed state paths work for both the daemon and status command.

Upgrading the plugin alone does not update the running receiver or bridge. Follow the
[yobiru-upgrade skill](../plugins/yobiru/skills/yobiru-upgrade/SKILL.md), reinstalling the Linux
bridge with the same thread UUID, socket and Codex home. The resident connection preserves the
session's existing permissions. See [CODEX_SANDBOX.md](CODEX_SANDBOX.md) to verify those permissions;
`resident.state: connected` means subscription, not task completion.

## Branding patch (0.4.1, historical validation)

The 0.4.1 release updated the shared icon, Codex brand color, Cursor logo reference and README images.
Its only runtime change was the reported product version; protocol and behavior remained at 0.4.0.
`tools/build_plugin.py check` and all 16 existing packaging tests passed, including the installed
Codex manifest validator and reproducible archive checks. Both release archives carry the same
approved icon as yobiru.com. README image paths and the updated demonstration were inspected.
The runtime and real-network test results below are retained from 0.4.0, not claimed as new runs.

## Historical policy and maintenance validation (0.4.0)

For 0.4.0, the packaged/loopback suite and bridge suites passed, as did the 59-check self-test
on one host's real Tailscale interface. Access-policy tests cover the fresh tailnet default,
legacy allow-list preservation, backed-up explicit migration, deny precedence, live policy reload,
recipient-bound attachments, and revocation before each exec/webhook retry and bridge dispatch.
The two-receiver workflow includes a fresh tailnet receiver and a legacy receiver, then migrates
and denies/unblocks peers without restarting either process.

Maintenance tests cover offline recovery across invocations, known-unsent retries with the same
envelope, held uncertain outcomes, blocked-peer recovery, no newly discovered recipients or sessions,
release metadata caching, concurrent runs, cancelled notices and damaged/mismatched saved routes.
They use injected release metadata and loopback receivers. Printed timer definitions were checked;
no operating-system timer was installed and no real peer was upgraded in this verification.

## Current automated check commands

The commands below apply to the current tree. Test totals identify their recorded 0.5.0 run;
older version numbers in individual behavior descriptions identify when coverage was added.
The real-Tailscale self-test result remains historical and was not rerun for 0.5.0.

| Check | How to run | What it shows |
|---|---|---|
| Package consistency and hygiene | `python3 tools/build_plugin.py check` | Generated manifests and bundled copies are current; `plugin.json` fits the closed Agent Plugins 1.0.0 schema; the Codex and Cursor manifests and both marketplace files follow the documented fields; skills are well formed; no symlinks; no runtime state, tailnet names, Tailscale addresses, home paths, emails, UUIDs or token-like strings in the plugin or docs. |
| Codex manifest | `python3 <codex skills>/plugin-creator/scripts/validate_plugin.py plugins/yobiru` | Passes the Codex plugin-creator validator (run by `tests/test_build.py` when the validator is installed). |
| Plugin tests | `python3 -m unittest discover -s tests` (220 tests in 0.5.0) | Reproducible archives that contain every skill. The unpacked archive, outside the source tree, in a path with spaces, works from any directory and through a symlinked launcher. Read-only commands create nothing. Setup and wake-config keep existing agents, adapters and keys and refuse silent replacements. Unsafe names and message ids are rejected. The launchd plist parses. On Linux, `codex-bridge install` forwards to the Linux installer without writing into the plugin cache, the installed bridge keeps working after the plugin cache is moved away, `status` reports a listening or stale socket, and file-only install and rollback make no systemctl calls. An addressed message with a file wakes only the named agent; the file is fetched and verified, the reply goes back to the sender's agent, and ack stays local. Receivers run on loopback with a fake Tailscale CLI. |
| Codex bridge, resident and installers (macOS and Linux) | `python3 -m unittest -v test_codex_bridge test_codex_queue_client test_codex_resident test_install_codex_bridge test_install_codex_bridge_linux` (146 tests in 0.5.0) | Queue, official-CLI interface, macOS install, rollback and dry run, all offline. Kind routing (0.2.1): excluded kinds are routed only after path, sender, allow list, target agent, wake and ack checks; only an unacked effective-`now` direct wake runs the local handler, with only the canonical inbox path appended, no shell and empty stdin; none, batch, acked and unverified records never run it; scans and dispatch never queue or forward excluded kinds, including rows queued earlier; malformed routing config fails before any side effect; failing, slow and missing handlers give fixed outcomes, the handler budget ends before the receiver's `hook_timeout_s`, and a receiver-length outer timeout leaves no handler or child process behind. Both installers validate routing in dry runs and keep it, tuned timing, custom dispatcher instructions (path and the bytes of the local file, also after rollback), the recorded Codex CLI and authorized replies on upgrade; the whole existing queue suite also runs again with routing enabled. Linux: a real temporary AF_UNIX socket and a fake `codex` executable run as a subprocess receive the exact `queue --thread UUID --message ... --remote unix:///...` argv and the pinned `CODEX_HOME`; missing, stale, foreign or non-socket endpoints and probe failures are known-unsent retries without running the CLI; failed, timed-out and receipt-less attempts stay uncertain and are never resent; reconciliation reads only the pinned Codex home, without the socket. The installer runs with an isolated HOME and a fake stateful `systemctl`: dry run writes and runs nothing, `--no-start` never calls systemctl, invalid input and unsafe targets are refused before writes, the fixed thread, queue and per-user unit are guarded, paths with spaces, `%` and `$` are escaped, pinned settings survive an upgrade from another ambient account, and upgrade and rollback restore files and service state while keeping inbox and queue (rollback fails closed when the service cannot be confirmed stopped). |
| Onboarding, discovery and updates (0.3.0) | `python3 -m unittest -v test_onboarding test_discovery test_update` in `tests/` | Onboarding per platform with injected facts: missing Tailscale (official Linux script with root/sudo; blocked without privileges or `/dev/net/tun`; native app on macOS/Windows, PowerShell-safe Windows paths), daemon unavailable, permission denied, logged out and device approval, separate delivery and session steps; `onboard` and `doctor` create nothing. `/info`: 403 for peers not on the allow list before any title lookup; projection without paths, argv, URLs, previews or unbound sessions; truncation flag; Codex binding cannot override the bridge. Native names: cache keyed by source, unsupported results never cached, rename seen after the TTL, cold lookups answer `pending`/stale within the wait, timeouts kill the whole process group, bounded output. Clients: legacy 404, 403, unreachable, malformed, hostile escape characters, non-UUID Codex ids, and slow peers (keep-alive, HTTP/1.0 close, slow headers) bounded by a total deadline. Updates: SemVer with prereleases (invalid leading zeros rejected), release parsing that ignores text and foreign URLs, stable/preview channels, no stable release, bounded fetch errors. Notices: dry run writes nothing, verified local upgrade and published release required, 202 receipt with matching id, invalid receipt and interrupted dispatch are uncertain and never resent (reconciliation reuses the identical envelope), 403/400 definite, refused connections stay pending, explicit and remembered offline peers persist without fabricated addresses, legacy receivers keep the explicit agent, stale plans cannot downgrade uncertain state, lock contention refused. Bridge: metadata-only `thread/read` (includeTurns false, never preview), `session-name` creates no queue, macOS and Linux upgrades keep session names. Plus an end-to-end loopback `agents`/`peers` run between two real 0.3.0 receivers. No real Tailscale, GitHub, Codex runtime or peer is contacted. |
| Real Tailscale, one host | `sh selftest.sh` (59 checks) | Temporary receivers on the host's own Tailscale address (ports 7866-7883 and a stub webhook on 127.0.0.1:7879): Tailscale identity and allow list, file transfer with SHA-256, expiry, spoofing and SSRF rejection, wake levels and policies, exec and webhook adapters, @agent addressing, and compatibility with an older receiver (`tests/fixtures/tailmsg_v1_pre_agents.py`). Passed on one Linux host with 0.4.0; production port and data home are not used. |

## Hosts (historical 0.1.0 observations)

The Codex results below were observed with Yobiru 0.1.0 and were not repeated for 0.2.x. The 0.2.x
changes (the Codex bridge, its installers and `codex-bridge` in `yobiru.py`) are covered only by the
offline and extracted-package tests above.

| | Codex | Cursor | Grok Bot | Other agent apps (for example DOTS) |
|---|---|---|---|---|
| Install from the repository marketplace | Passed locally: `codex plugin marketplace add` + `codex plugin add yobiru@yobiru` (codex-cli 0.162.1, Linux); installed copy identical to the source files | Not tested | Not tested | Not tested |
| Skills discovered | Passed: app-server `skills/list` shows `yobiru:yobiru-inbox`, `-send`, `-setup` and `-wake` | Not tested | Not tested | Not tested |
| Bundled command from the install location | Passed: `paths`, and `doctor` against an existing tailmsg setup (no errors, nothing changed) | Not tested | Not tested: whether the plugin's scripts reach the Bot's computer is unknown | Not tested |
| A real conversation using the skills | Not tested | Not tested | Not tested | Not tested |
| Marketplace listing or review | Not submitted | Not submitted | Not submitted | — |

The following acceptance areas were not exercised by the isolated 0.5.0 release suites above.
The later live acceptance section records the limited Linux/Grok runtime checks separately:

- Two real machines exchanging messages, files and wake-ups over one tailnet.
- macOS and Windows runs of the `yobiru` command, and installing the printed service definitions.
- The Codex desktop bridge against a running Codex desktop app in this release.
- A 0.5.0 Linux bridge upgrade against a real Codex app-server and a real user systemd manager;
  the release suites use fakes. The limited live 0.4.2 probes described above remain historical.
- A Grok Bot routine webhook wake end to end, and a long-running receiver on a Grok Bot computer.
- 0.4.0 against real systems: a real Tailscale install or sign-in through `onboard`, `/info` between two
  real machines, a native name read from a live Codex app-server, `update check` against the live
  GitHub API, and `update notify` to real peers.

## Non-UTF-8 command output

`python3 -m unittest discover -s tests -p test_stdio.py -v` exercises the standalone core and
the unpacked plugin with actual cp1252, ASCII and UTF-8 subprocess pipes. Chinese and Japanese
messages, accented text, emoji, agent names and paths no longer raise `UnicodeEncodeError`.
The CLI preserves the selected output encoding and displays unsupported text as backslash escapes;
use `PYTHONIOENCODING=utf-8` when the receiving terminal or program expects UTF-8 characters.
Structured CLI JSON uses ASCII-safe JSON escapes, which decode to the original Unicode values,
including emoji. Stored messages and network payloads remain UTF-8. Imports do not reconfigure
the importing program's streams. These pipe tests run on Linux; they are not a Windows/Git Bash run.

## Reproducing the release archives

`python3 tools/build_plugin.py release` writes `dist/yobiru-<version>-plugin.zip`,
`dist/yobiru-<version>-marketplace.zip` and `dist/SHA256SUMS`. The archives are byte-for-byte
reproducible from the same source tree (see [RELEASING.md](RELEASING.md)), so a published checksum can
be checked by rebuilding.
