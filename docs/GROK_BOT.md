# Yobiru for Grok Bot

Use Grok Bot's existing conversation to exchange requests, results and files with agents on your
home computer or cloud server. Yobiru runs on the computer attached to that conversation, not on
the phone displaying it. It uses the same plugin and tailmsg protocol as Codex and Cursor.

## Install and locate the plugin

The repository contains a portable `plugin.json`, a Cursor manifest and the shared `skills/` and
`scripts/` directories under `plugins/yobiru`. It does not need a separate Grok protocol or an MCP
server. The [official submission page](https://x.ai/bot/marketplace/submit-a-plugin) accepts public
repository submissions for Grok Bot and Cursor; Grok itself is marked coming soon. Submission,
review, public listing and installation are separate steps. See the current
[submission record](MARKETPLACE_SUBMISSION.md) before claiming a listing.

If Yobiru is available to your account, open **Connect apps** in the sidebar (mobile: avatar →
**Connect apps**), add it and confirm it under **Installed**. For an account that supports
Cursor team repository marketplaces, import `https://github.com/chekusu/yobiru` using the team's
**Plugins & MCPs** settings, then check whether Grok Bot actually exposes the plugin's skills.
Account and team policy can limit this route. See the host's
[plugin instructions](https://cursor.com/help/grok-bot/connect-plugins).

Ask: **“Use yobiru-grok-bot to connect this conversation's computer to my other agents.”**
The skill first identifies this conversation's computer, existing installation and data home.
It then uses `yobiru-setup` for ordinary setup. If the host exposes only skill text, the agent can
download the official plugin archive and its checksum, verify them, and unpack into a new directory
under `/workspace`. That is a manual installation fallback, not proof of a marketplace install.

For a release archive, choose a published version from
[Chekusu's releases](https://github.com/chekusu/yobiru/releases). Download
`yobiru-<version>-plugin.zip` and `SHA256SUMS` from that same release. Compare the archive's SHA-256
with the exact filename's checksum entry before extraction. Extract into an empty directory such
as `/workspace/yobiru-<version>/`; keep runtime data elsewhere. Run the extracted
`yobiru/scripts/yobiru` with `sh`, so ZIP extraction need not preserve executable modes.

## Prepare the conversation's computer

Each conversation may use a different computer. Owner conversations, team conversations and shared
channels need not share a shell, filesystem or Tailscale identity. Inspect the current environment
before changing anything; do not copy another computer's configuration to make it look connected.

On that computer, use the verified plugin's absolute launcher path, represented below by `Y`:

```sh
sh "$Y" paths
sh "$Y" --home /workspace/.tailmsg onboard
sh "$Y" --home /workspace/.tailmsg doctor
```

`/workspace/.tailmsg` is a suggested new data home; preserve an existing installation's actual
home. These checks identify missing Python, Tailscale, sign-in, receiver or delivery configuration.
They do not install software or change permissions. Yobiru needs a usable Tailscale interface,
not just a userspace proxy. If the computer lacks the required privileges or `/dev/net/tun`, follow
the reported administrator action instead of bypassing the host's restrictions.

When the owner has asked for setup, use the setup skill to install the official prerequisites,
register the intended agent, install a durable runtime and configure its supported service manager.
The owner signs in to their own Tailscale account. Keep authentication keys out of chat. Preserve
existing agents, webhook files, access policy, session bindings and reply permissions.

## Connect incoming messages to a routine

A running receiver stores incoming messages; Grok Bot also needs an authorized routine or other
adapter to process them. Follow `yobiru-wake` to configure the account's routine webhook. Keep its
URL and bearer key in private files or environment variables, not in the plugin or an issue.
The routine should read only this agent's authenticated inbox, report `running` and one final
outcome for an authorized request, then acknowledge it. Notifications need no reply.

An HTTP receipt means storage. A successful webhook call means the adapter accepted the wake.
Only the eventual tracked result reports the agent's outcome. Keep existing execution and reply
restrictions: a request may finish `blocked` when the owner has not authorized the requested work.

## Recover after a rebuild

Files stored durably and running processes are different things. A rebuild may retain a data home
while removing installed packages or stopping the receiver. Start with `onboard`, `doctor` and
`update status` against the existing home. Restore missing prerequisites through the host's normal
setup path, sign in if needed, then restart only the intended Yobiru receiver.

Keep the owner's chosen stable Tailscale hostname. New messages addressed as `HOST/AGENT` resolve
the current address, so a changed IP need not require editing every sender. If the node also lost
its hostname or identity, resolve that explicitly; do not redirect old requests or file grants to
a similarly named machine. Preserve all existing agents and adapters during recovery.

Use a supported host startup facility when available and authorized. A process that runs now does
not prove it will start after a rebuild. Report separately which files persist, which startup
mechanism was installed and whether a real restart was tested. Do not deliberately reboot a live
computer merely to demonstrate plugin installation.

Known limits include discovery treating a false Tailscale online flag as an offline receiver without
probing, and a running receiver retaining its startup node name after a rename or relogin. See
[restart and identity recovery](RESTART_RECOVERY.md) for the observed symptoms, code paths, bounded
recovery steps and proposed regression coverage. These runtime fixes are not part of 0.5.2.

## Test the actual integration

Record the installed version and execution path, without exposing private device names or keys:

1. Confirm the host lists the plugin's skills for this conversation, including `yobiru-grok-bot`.
   Ask the host to invoke the skill; opening a Markdown file manually proves only file access.
2. Confirm the invoked skill finds its bundled command and runs `paths`, `version`, `onboard` and
   `doctor`. State whether scripts came from the native plugin or the manual archive fallback.
3. With the owner's chosen peer, use `yobiru-send` for a harmless tracked request, then verify
   `received`, a final result and `request status ID --refresh --json`. Use `yobiru-inbox` for the
   reverse direction. Keep the challenge and any shared test file free of private data.
4. If testing routine delivery, verify both the wake record and the resulting agent response.
   A queue entry alone is not completion. If credentials or app access block a step, record it.

On 2026-10-13, a manually deployed 0.5.0 Grok Bot receiver completed a real cross-machine tracked
request and an authenticated status refresh. That historical runtime test did **not** establish
marketplace installation, native plugin discovery or rebuild recovery. See
[verification](VERIFICATION.md) for the scope of each test and [privacy](PRIVACY.md) for data handling.
