# Restart, relogin and receiver identity recovery

This record separates an observed Grok Bot recovery from proposed fixes. The 0.5.2 and 0.5.3 releases do
not change discovery or live identity refresh. A stable hostname helps new sends find the current
address; it does not guarantee that a receiver, authentication state or wake routine survives a
cloud-computer rebuild. Keep private hostnames, addresses, session IDs and operational logs out of
public reports.

## Observed recovery on 2026-10-19

- A peer's Tailscale snapshot reported `Online=false` and `Active=true`, while a hostname-based
  Tailscale ping and an authenticated Yobiru `/info` request succeeded. The discovery command
  nevertheless displayed the receiver as offline because it had not probed it.
- The running receiver reported version 0.5.0 and an older, suffixed node name. Fresh Tailscale
  status and `whois` identified the intended current name. Reachability and receiver identity were
  separate problems; successful ping did not establish that the request route was usable.
- A tracked send using the stable hostname was rejected before a message ID was created. It did
  not use a remembered old IP. The error suggested missing request-lifecycle support even though
  the receiver advertised that capability; its reported node did not match the resolved target.
- After the operator restarted the existing receiver with its existing configuration, `/info`
  reported the current name and a request through the same standard hostname was accepted.
  Acceptance establishes storage, not final execution, reply delivery or recovery after every rebuild.

The observations do not establish why Tailscale's flags differed from live reachability or which
earlier event produced the stale receiver name. They do not prove that relogin always changes a
node's identity, or that a matching hostname identifies the same authorized recipient.

## Causes verified before the recovery changes

| Path | Current behavior and consequence |
| --- | --- |
| `discovery_rows()` in [the plugin CLI](../plugins/yobiru/scripts/yobiru.py) | Skips the HTTP probe when normalized `online` is false, then sets `receiver=offline` with `offline in Tailscale; not probed`. A reachable receiver can therefore appear offline. |
| `cmd_serve()` / `make_handler()` in [the core](../tailmsg.py) | Capture the local node name when the receiver starts. `/info` and message-recipient validation use that captured name, which can become stale after a rename or identity change. |
| `cmd_send()` in [the core](../tailmsg.py) | Resolves the target from a fresh Tailscale snapshot. For tracked requests, `/info` must match the resolved node and advertise `request-lifecycle-v1` before an ID or envelope is generated. The current combined error also covers identity mismatch and other preflight failures. |

Short-name resolution, exact FQDN/IP selection and persisted recipient identities have different
roles. A new send may follow a stable hostname to its current address. Old requests, responses and
file grants must not be redirected to a replacement node merely because its display name matches.

## Bounded recovery with existing authorization

1. Identify the current conversation's computer, installed command, data home, receiver process and
   service definition. Run `onboard`, `doctor` and `update status` against that explicit home. Keep
   plugin, durable runtime and running receiver versions distinct; preserve newer development builds.
2. Compare a fresh Tailscale snapshot with a bounded hostname-based connectivity check and the
   intended receiver's `/info`. Confirm the authenticated identity and access policy. `Online=false`
   alone does not prove unreachability; `Active=true` or ping alone does not prove receiver health.
3. If the receiver's reported node differs from the confirmed current identity, back up its existing
   configuration and recovery definition, then restart only that existing Yobiru receiver under
   the owner's repair authorization. Reuse the same data home, service command and configuration.
   Preserve all agents, webhook secrets, session bindings, requests, attachments and permissions.
   Do not reinstall Tailscale, change ACLs or disable identity checks to repair this mismatch.
   Restore missing network prerequisites separately only when inspection shows they are absent.
4. Recheck `/info` against the current authenticated identity before a harmless tracked request.
   Require the normal receipt, verified final result and status refresh for end-to-end acceptance.
   The preflight rejection above created no ID; if another attempt did create an ID or its send
   result is uncertain, reconcile that original request instead of issuing a duplicate.
5. Report network reachability, receiver identity/version, wake handling and final outcome separately.
   Retain unresolved restrictions. Never use `--legacy-request`, a renamed target or a broader access
   policy to bypass an identity mismatch. A process restart is not a verified machine-rebuild test.

## Recovery changes introduced after the immutable v0.7.14 tag

These changes are included in the v0.7.15 release. Deploying the original v0.7.14 archive does not include them; use v0.7.15 or a later explicitly identified release.

- Discovery probes every selected peer with the existing HTTP deadline and bounded parallelism, including
  peers whose Tailscale snapshot says `Online=false`. The flag and observed receiver reachability remain
  separate output fields. A failed/unavailable probe never invents a receiver version or agent session.
- A running receiver pins its startup DNS name, StableID and normalized address set. Before protected
  handlers, maintenance phases, response sends and wake attempts it asks trusted local Tailscale state.
  A status subprocess has a one-second deadline; overlapping checks share only an observation completed
  after they arrived, with a 1.25-second lock-wait limit. Calls arriving after completion perform a fresh
  lookup. This is not an authorization cache or a background task that silently changes identity.
- Missing state, a stopped/logged-out daemon or a failed lookup returns `receiver_identity_unavailable`.
  No protected operation starts from that check, and a later healthy lookup may recover. A confirmed
  name, StableID or address change latches `receiver_identity_changed` for the process. `/info`, messages,
  request status and file/upload grants fail closed with HTTP 503, and `/health` reports degraded while
  remaining reachable. A pending upload rechecks before committing its bytes; POST rechecks after its
  body. Maintenance and wake/send checks do not rewrite any persisted request, response or file principal.
- A confirmed change requires the operator to follow the preservation/reconciliation steps above and
  restart the existing receiver. Returning to the old name does not silently unlock that process. The
  new process obtains a new initial identity; this does not migrate or retarget old requests. Reconcile
  original IDs and authenticated bindings explicitly. An already-started external action is not undone
  by a later identity change; the checks are bounded observations, not an atomic transaction with Tailscale.
- A transiently blocked wake keeps the same inbox message pending with zero additional adapter attempts.
  After identity recovery, `wake <original-id>` can retry that original record; no new message is needed.
  Identity recovery does not automatically relaunch an action that may already have started.
- Tracked-send preflight distinguishes access denial, transport failure, unexpected HTTP status,
  malformed response, receiver identity mismatch and missing capability. GET `/info` retains a five-second
  socket timeout, adds a five-second exchange deadline and a 64 KiB response bound. All rejected cases
  precede message-ID creation, attachment staging and durable request state. No automatic legacy fallback
  or permission bypass is added.

`tests/test_receiver_recovery.py` covers offline/reachable and offline/unreachable probes, name/StableID/
address transitions, unavailable-state recovery, a latched change, slow LocalAPI/lock bounds, concurrent
checks, body/upload transitions, unchanged saved work, maintenance/send/wake gates, original-ID wake retry,
slow-but-valid preflight and diagnostic failures before state creation. These are synthetic LocalAPI and
loopback HTTP tests; they do not claim a live relogin, machine rebuild, or real agent wake acceptance.

See the [Grok Bot guide](GROK_BOT.md) for setup and the [verification record](VERIFICATION.md) for
released tests and live observations.
