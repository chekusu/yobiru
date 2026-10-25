# Marketplace submission and reviewer guide

Yobiru uses one plugin directory, `plugins/yobiru`, for Grok Bot, Cursor, Claude Code, Codex
and compatible ChatGPT desktop execution. The portable manifest and host
overlays are generated from `packaging/product.json`. Desktop plugin installation needs no separate
hosted MCP service or hosted messaging account; there is no duplicate ChatGPT codebase. The
Codex and Cursor overlays register no MCP server. The Claude Code overlay registers the bundled local
stdio server (`yobiru mcp`), which calls the same CLI and opens no network port. The optional public-bot
`/mcp` endpoint is separate and runs on a gateway: one the user hosts, or the official bots.yobiru.com.

Official submission routes checked on October 15, 2026:

| Host | Official route | Package |
| --- | --- | --- |
| Grok Bot / Cursor | [Grok plugin submission form](https://x.ai/bot/marketplace/submit-a-plugin), with individual host checkboxes | Public [GitHub repository](https://github.com/chekusu/yobiru); root Cursor marketplace points to `plugins/yobiru`. |
| ChatGPT / Codex | [OpenAI plugin portal](https://platform.openai.com/plugins), [submission guide](https://developers.openai.com/plugins/deploy/submission) | `yobiru-VERSION-plugin.zip` from the matching GitHub release. |

These links are submission routes, not evidence of an approved marketplace
listing. The current native-host verification record is in
[VERIFICATION.md](VERIFICATION.md). Record a successful submission receipt
before calling a submission complete; review approval and publication are
separate steps.

## Listing information

- Name: Yobiru
- Publisher: Chekusu; organization handle: `chekusu`
- Subtitle: Connect your personal agents
- Website: <https://yobiru.com>
- Source: <https://github.com/chekusu/yobiru>
- Support: <https://github.com/chekusu/yobiru/issues>
- Privacy policy: <https://github.com/chekusu/yobiru/blob/main/docs/PRIVACY.md>
- Terms of use: <https://github.com/chekusu/yobiru/blob/main/docs/TERMS.md>
- License: MIT
- Icon: `plugins/yobiru/assets/icon.png` (square PNG).

Suggested short description:

> Connect your personal agents across home computers and cloud machines over
> Tailscale. Exchange messages and files, send requests with receipts and final
> results, and wake configured receivers. Works with the host's local command
> execution and your own network. Private messaging has no central message or file store; public bots
> and Friends use a gateway.

Provide the publisher's actual name and reachable work email in private
submission fields. Do not put personal contact details, credentials, reviewer
tailnet identities, device addresses or test transcripts in this public file.

## Host-specific packaging

### Grok Bot and Cursor

The repository's `.cursor-plugin/marketplace.json` points to the shared plugin.
The plugin includes a portable `plugin.json`, a `.cursor-plugin/plugin.json`
overlay, skills, and bundled Python scripts. The Grok Bot skill explains the
conversation's computer, persistent data, stable device names, and recovery after
a cloud-computer rebuild. See [GROK_BOT.md](GROK_BOT.md).

The submission form requires local and in-app testing. Check that declaration
only after exercising the plugin through the actual target application's plugin
or supported development-import mechanism. A manual CLI install or a successful
webhook alone does not establish that the host loads plugin skills and assets.
Select only hosts for which the submitted package has been tested. Grok chat's
"coming soon" checkbox is not evidence that local shell execution is available.

### ChatGPT and Codex

Yobiru is a skills-only plugin that needs local execution. It declares
`requires_local_executor: true` in `extensions.com.openai`, which also contains
the complete OpenAI interface and setup onboarding skill. The legacy
`.codex-plugin/plugin.json` is retained for compatible Codex versions. When the
portable OpenAI extension is present, it takes precedence over that legacy file.

Submit the ordinary plugin ZIP through the
[desktop-only route](https://developers.openai.com/plugins/deploy/desktop-only).
Do not invent a remote MCP URL (a self-hosted public bot gateway's `/mcp` is the
user's own service and not part of the desktop plugin submission) or mark Yobiru as working in a
cloud conversation that cannot run Python and join the user's tailnet. Directory review, publisher
business verification and portal permissions are managed by OpenAI. Local
installation does not grant publishing access.

The ZIP has a single top-level `yobiru/` directory, with `plugin.json` directly
inside it; this is a supported [archive shape](https://developers.openai.com/plugins/deploy/submission-errors).
English metadata and Chinese/Japanese publication translations are included.
OpenAI currently documents that translations are stored and validated but do
not yet change rendered listing text; do not promise a localized live listing.

## Reviewer setup

Use two disposable computers on a reviewer-controlled Tailscale network, with
Python 3.8+ and the target application's local execution available. Reviewers
use their own Tailscale account; Yobiru has no separate account or API key.
No access to a maintainer's personal network is required or provided.

1. Install/import the shared plugin using the host's supported mechanism.
   Confirm that its Yobiru skills are visible and its bundled command can run.
   In Grok Bot, run the skill on the computer attached to the conversation.
2. Ask: "Set up Yobiru on this computer." The setup skill should inspect Python
   and Tailscale, explain missing prerequisites, then help with authorized setup.
   Interactive Tailscale login remains with the reviewer. Choose stable, unique
   names such as `review-laptop` and `review-bot`.
3. Configure and start the receiver on each computer, preserving any existing
   data and policy. Use an explicitly approved receiver/wake adapter if testing
   agent wake-up. Discovery should show the peer and its actual agent/session
   metadata without claiming that an unknown session is active.
4. Through the sender's plugin, ask the other agent to calculate `19 + 23` and
   return the result. Use a tracked request with a 120-second deadline. Expect a
   durable `received` receipt, agent-reported progress when execution begins,
   and a final result of `42`. Query the same request ID to verify the outcome.
5. Send a small text file containing only `yobiru review fixture`. Fetch it on
   the recipient through the plugin; compare contents and SHA-256. This uses
   the sender's receiver, so keep that computer online until the fetch finishes.
6. Send a notification and verify that no application reply is required. Send a
   request to a receiver with no execution adapter, use a short deadline, and
   verify that receipt and timeout are distinguishable from a completed result.
7. In Grok Bot, ask the recovery skill to inspect the current setup without
   changing it. It should distinguish Tailscale connectivity, receiver service,
   plugin assets, persistent data and configured agent wake permissions. It must
   preserve existing webhooks and report missing prerequisites honestly.

After testing, stop the disposable receivers and remove reviewer data according
to [PRIVACY.md](PRIVACY.md). Do not delete or restart an existing personal agent's
data or service as part of an isolated package review.

## Build and record evidence

```sh
python3 tools/build_plugin.py sync
python3 tools/build_plugin.py check
python3 -m unittest discover -s tests -v
python3 -m unittest discover -s . -p 'test_*.py' -v
python3 tools/build_plugin.py release
```

Keep the release tag, commit, ZIP hash, test output and host-specific smoke-test
result with the submission record. Keep publisher emails, portal screenshots
containing account information, form response IDs and private network details
outside this public repository. A successful build is package evidence; an
in-app test, submission receipt and approved listing are distinct evidence.
