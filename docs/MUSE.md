# Muse custom connector

Yobiru ships an outbound Muse adapter and a dedicated `yobiru-muse` skill. It reuses the existing CLI, identity checks, request ledger and attachment transport. It starts no server, installs no Tailscale daemon and introduces no bearer secret. The implementation is `plugins/yobiru/scripts/yobiru_muse.py`; invoke it through `sh plugins/yobiru/scripts/yobiru muse`.

## Installation version

The `v0.7.15` archive includes this adapter and the `muse` command. Download `yobiru-0.7.15-plugin.zip` and its matching `SHA256SUMS` from the same official release, verify the archive before extraction, and keep any existing package or data home unless the owner explicitly authorizes a separate installation.

## Connect with the official Tailscale connector first

Ask Muse to connect the official Tailscale connector, or use Settings > Connectors. Complete its sign-in and approval flow. Muse joins separately from the user's phone or computer. Its integration allows outbound connections only, so Yobiru uses pull requests and never asks Muse to run a receiver.

The actual execution environment must expose Python 3.8+, `tailscale status --json` with its own StableID/DNSName, a usable local identity lookup and outbound TCP to the receiving Yobiru machine. Official connector availability does not guarantee these programmatic interfaces in every account. Run `muse check` first, then `muse check --peer HOST[:PORT]/AGENT`. The latter probes the receiver without sending a task. It does not prove delivery or inspect the user's tailnet grants.

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
