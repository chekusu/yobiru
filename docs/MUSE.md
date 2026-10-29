# Muse custom connector

Yobiru ships an outbound Muse adapter and a dedicated `yobiru-muse` skill. It reuses the existing CLI, identity checks, request ledger and attachment transport. It starts no server, installs no Tailscale daemon and introduces no bearer secret. The implementation is `plugins/yobiru/scripts/yobiru_muse.py`; invoke it through `sh plugins/yobiru/scripts/yobiru muse`.

## Installation version

The `v0.7.15` archive includes this adapter and the `muse` command. Download `yobiru-0.7.15-plugin.zip` and its matching `SHA256SUMS` from the same official release, verify the archive before extraction, and keep any existing package or data home unless the owner explicitly authorizes a separate installation.

## Connect with the official Tailscale connector first

Ask Muse to connect the official Tailscale connector, or use Settings > Connectors. Complete its sign-in and approval flow. Muse joins separately from the user's phone or computer. Its integration allows outbound connections only, so Yobiru uses pull requests and never asks Muse to run a receiver.

The actual execution environment must expose Python 3.8+, `tailscale status --json` with its own StableID/DNSName, a usable local identity lookup and outbound TCP to the receiving Yobiru machine. Official connector availability does not guarantee these programmatic interfaces in every account. Run `muse check` first, then `muse check --peer HOST[:PORT]/AGENT`. The latter probes the receiver without sending a task. It does not prove delivery or inspect the user's tailnet grants.

A connected official connector can still be useful when this private adapter's identity check fails. In the [community field report in #59](https://github.com/chekusu/yobiru/issues/59), the bundled CLI offered `up`, `down`, `status`, `ip` and `help`, but not `status --json`. That did not expose the StableID this adapter requires. Do not infer that the connector is disconnected, invent an identity, or copy one from another device.

When the JSON command fails, `muse check` tries a bounded plain `tailscale status` query without displaying its output. If that succeeds, it reports `tailscale_json_unavailable`: structured identity is unavailable even though the CLI responds. Plain status, `tailscale ip` and HTTP health cannot supply the trusted StableID required by the pull transport, so `local_ready` remains false and the command exits 2.

An explicitly requested `--peer` check still runs after a local identity failure. For a limited CLI, use the intended receiver's exact Tailscale IP, for example `muse check --peer <RECEIVER_TAILSCALE_IP>:7766/AGENT`; hostname resolution still needs JSON status. The read-only `/health` and `/info` probes report route, receiver access and advertised pull support separately from local readiness. A successful probe does not verify delivery or make the private adapter ready to send.

Use the full [packaged Muse skill](../plugins/yobiru/skills/yobiru-muse/SKILL.md) for setup, permissions, files, recovery and the Friends fallback. A new isolated Yobiru data home can be configured with `setup --self-agent muse --allow RECEIVER --apply` after reviewing the plan. The allow rule permits authenticated result refresh from that intended receiver; it does not open a listener.

## Command contract

| Command after `yobiru --home DIR muse` | Effect |
| --- | --- |
| `check [--peer HOST[:PORT]/AGENT]` | Read config and official connector identity, optionally probe peer health/info. JSON, no state created. Exit 2 means incomplete/unavailable. |
| `send [--file PATH] [--wake none\|batch\|now] [--timeout-seconds N] -- PEER/AGENT TEXT` | One tracked `pull` request as `muse`, stored before submission. Default wake is `batch`, timeout 900 seconds. |
| `status [--cached] ID` | Refresh the original outgoing request by default; cached mode reads its local ledger. |
| `list` | Read the existing request ledger, including IDs whose initial CLI output was interrupted. |
| `resume ID` | Resume upload/submission of the same saved pull request; no new ID. |
| `read ID` / `fetch ID` | Read the stored response or download/hash-verify its attachments. |

Private receiver permissions and Tailscale identity are authoritative. The adapter does not install a wake integration or support unsolicited inbound private messages to Muse. A fresh Muse VM that loses its identity or ledger must not silently recreate an uncertain task; reconcile it with the receiver/operator first.

## Copy into Muse

> Install the Yobiru v0.7.15 plugin archive and verify its matching SHA256SUMS before extraction. It includes the yobiru muse command and yobiru-muse skill. Verify muse --help before setup. First help me connect Muse's official Tailscale connector. Check whether your actual execution environment can use its CLI, identity and outbound route. Use the packaged `yobiru muse` adapter and pull requests; do not start a receiver in Muse. Keep the returned request ID and poll its real result. If the official connector cannot be connected or used here, help me connect the intended bot through Yobiru Friends instead. Do not send any task or file until I have specified the recipient and authorized the work. Do not invent an OAuth flow, an installable Muse manifest or a successful round trip.

This is an executable custom-connector adapter plus setup instructions, not a claim that Meta's directory already lists Yobiru. Meta's public guidance supports asking Muse to create a custom connector; it does not establish that a Python adapter is accepted in every account. If the current account accepts only remote APIs, the existing Friends gateway MCP is an alternative when its static bearer flow is supported. The local Friends client preserves its own encryption keys; a remote-only MCP client does not provide the same client-side encryption boundary. No shared OAuth or new public MCP service is added here.

## Friends through the official connector's proxy

Friends does not use the private adapter's StableID authentication. It uses gateway credentials and outbound HTTPS; a self-hosted gateway reachable only through the tailnet can still require the official Tailscale connector's route.

The #59 reporter describes runtime connector documentation for an HTTP proxy on port **3130**: HTTPS first uses **CONNECT** to the destination host and port, then verifies the destination's TLS certificate inside that tunnel. Confirm the actual endpoint and supported route from the current account's connector documentation. This is a reported official connector route, not a public proxy service supplied by Yobiru. Keep proxy credentials separate from gateway bearer credentials; gateway authorization belongs inside destination TLS. Do not disable certificate or hostname verification, guess a different destination port, or silently fall back to another network route.

**Stable 0.7.15 does not implement this CONNECT transport.** Its Friends client connects directly with `http.client`; setting `HTTPS_PROXY` alone does not enable this route. [PR #54](https://github.com/chekusu/yobiru/pull/54) proposes explicit process-local CONNECT support separately and is unmerged as of 2026-10-28. The report's process-only adapter is local integration code, not part of this release. A proxy-only account needs an independently reviewed, compatible transport before running the Friends commands below. This documentation does not install that adapter or bring the pending PR into stable.

Direct Python HTTPS in the two reported environments failed with `SSLError.reason=WRONG_VERSION_NUMBER`. This identifies failure of that tested path; it does not establish its cause, a reason to turn off TLS checks, or a restriction on every Muse account.

## Scheduled Friends receive, wake and reply

Use the packaged [Friends recipe](../plugins/yobiru/skills/yobiru-muse/references/friends.md) for credential provisioning, scheduled checks, native turn handoff, claim/reply, and reply collection. It reuses the Friends chat-host policy: peer content is untrusted data and grants no permission to use tools or private context. The owner keeps admission and reply review; the receive loop gets no owner credential.

The current CLI contracts support scheduled checks, but distinguish them carefully:

| Operation | What it establishes |
| --- | --- |
| `public job list BOT` | Metadata only, including whether a job is claimable. It does not claim the job or start a native turn. |
| `public inbox --as BOT --no-ack` | Content-bearing reply collection without receipt acknowledgement. It still records serving at the gateway. It includes history, so filter acknowledged items; it is not a metadata-only event queue. |
| Muse-native scheduled task or Hook | Account-specific scheduling and delivery. Verify idle silence, a real native turn and visible delivery in that account. No Muse Hook API or installer is supplied here. |
| `public connector run BOT --once` | Existing connector polling and configured wake route. Exec suppresses child output and passes only `PATH`, `HOME`, `LANG`; manual-route `accepted` means nothing was sent. Neither establishes Muse Hook delivery. |

The recipe's automated tests use the current packaged CLI and a loopback gateway with synthetic accounts. They exercise polling without claiming, review gates, claim/reply, and collection without early acknowledgement. They do not run a Muse Hook or verify a CONNECT route.

The #59 field report covers two live Muse accounts, a self-hosted **0.7.13** gateway and local CONNECT/Hook integration: 60-second idle-silent checks, a synthetic automatic exchange, and incoming messages/replies visible in designated native side chats. It does **not** validate 0.7.15 in Muse, #54 on a live account, restart recovery, the official hosted gateway, or directory approval. Keep that historical evidence separate from the current local tests and the account acceptance steps in the recipe.

## Muse review documentation

The [official connector guidelines](https://platform.muse.ai/docs) require a complete task flow, documented permissions and errors, and a dedicated test account/demo for directory review. The adapter supplies submission, original-ID status, same-ID recovery and result/file retrieval. A private self-created connector and a directory-reviewed API/MCP connector are separate distribution paths. This source package does not by itself constitute a directory submission.

| Operation | Classification and side effects |
| --- | --- |
| `check`, `list`, `status --cached`, `read` | Read: inspect local state; peer check also reads authenticated receiver health. |
| `status ID` | Reads the original remote outcome and updates the local request ledger; document both effects. |
| `send`, `resume` | Sensitive write: submits a message/task and optional files to the explicitly authorized peer; may request a wake. Resume uses the original ID, never a fresh task. |
| `fetch` | Reads authorized remote files and writes hash-verified copies locally. |
| Initial setup | Writes an isolated local configuration; official Tailscale enrollment/permissions remain a separate user-controlled connector flow. |

No shared credential should be embedded in a Muse prompt, connector description or this repository. A directory submission using Friends must document the actual per-user static-bearer flow, approved tools, plaintext gateway boundary and available revocation; it must not claim OAuth. Reviewers need their own scoped test identity and representative synthetic data. Describe rate/size limits as those advertised by the chosen receiver or gateway and preserve its explicit permission/deadline errors. Unknown submission or execution outcomes must be reconciled under the original ID.

## Evidence and comparator decisions

The transport tests use a real local receiver and synthetic Tailscale identities, with reverse connections forbidden. They exercise the adapter's one-way request/result flow and same-ID recovery. These tests do not verify a live Muse account, its secure credential store, background execution, regional availability or marketplace review. User-reported Japan availability remains an account observation, not a universal availability promise.

Pinku.chat's remote connector uses an account-bound OAuth flow. Mokutori's connector uses a separate single-installation MCP bridge. Yobiru already has a pull protocol and Friends MCP, so this adapter reuses them instead of adding another server or borrowing those products' authentication contracts.

Official sources checked 2026-10-26:

- [Meta: How Muse works with Connectors](https://www.meta.com/help/superintelligence/1687253048996149/): built-in connection controls, user approvals and custom connector creation.
- [Tailscale: Meta Muse integration](https://tailscale.com/blog/meta-muse-ai-agent-tailscale): separate tailnet node, outbound-only connections and normal grants/tags.
- [Muse Connector Platform](https://platform.muse.ai/): submission, review and directory listing are separate steps.
