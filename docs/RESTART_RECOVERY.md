# Restart, relogin and receiver identity recovery

This record separates an observed Grok Bot recovery from proposed fixes. The 0.5.2 release did
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

## Causes verified in the current code

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

## Proposed fixes, not implemented in 0.5.2

- Treat the Tailscale online flag as a signal independent from bounded receiver reachability probes.
  Make an unprobed/unknown state distinct from a failed probe; retain deadlines and concurrency bounds.
- Refresh the receiver's own identity from trusted local Tailscale state, with a defined fail-closed
  response when it cannot be established. Define how in-flight requests and stored response/file
  authorization retain their original bindings before changing live identity behavior.
- Split tracked-send diagnostics into identity mismatch, missing capability, access denial and
  transport/malformed-response failures. Preserve preflight rejection before creating request state.

The following regression matrix is required for such a future change; it is a test plan, not a
claim of implemented coverage or completed validation:

| Scenario | Required evidence |
| --- | --- |
| `Online=false` with a reachable receiver | Bounded discovery reports both the flag and successful authenticated receiver probe, without a false offline conclusion. |
| `Online=false` with an unreachable receiver | Probe terminates within its budget and reports unreachable or unknown without fabricating a version/session; no unbounded retry. |
| Rename or relogin while the receiver runs | Trusted identity refresh or a clear recovery requirement; no stale `/info` identity silently accepted. Distinguish same-node rename from replacement identity. |
| Requests, replies and file grants spanning identity changes | Existing authenticated recipient/sender bindings remain enforced; no reassigning old work or attachments to a similarly named replacement. |
| Missing identity, spoofed/mismatched `/info`, policy denial or unavailable capability | Fail closed with the correct diagnostic, no request ID/POST from rejected preflight, and no automatic legacy or permission bypass. |
| Receiver restart with existing data and adapters | Agents, webhooks, sessions, permissions and ledger survive; uncertain delivery is reconciled by the original ID without duplicate execution. |

See the [Grok Bot guide](GROK_BOT.md) for setup and the [verification record](VERIFICATION.md) for
the narrower scope of released tests and live observations.
