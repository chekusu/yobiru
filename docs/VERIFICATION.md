# What has been verified

Yobiru release verification. This page separates what has actually been run from what has not. It is not a
security guarantee: the tests show the behaviors listed below, nothing more.

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
