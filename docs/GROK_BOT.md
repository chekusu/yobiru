# Yobiru for Grok Bot

Use Grok Bot's existing conversation to exchange requests, results and files with agents on your
home computer or cloud server. Yobiru runs on the computer attached to that conversation, not on
the phone displaying it. It uses the same plugin and tailmsg protocol as Codex and Cursor.

## Install and locate the plugin

The repository contains a portable `plugin.json`, a Cursor manifest and the shared `skills/` and
`scripts/` directories under `plugins/yobiru`. It does not need a separate Grok protocol or an MCP
server. (The public bots of 0.6.0 offer an optional MCP endpoint and stdio proxy; neither has been
tested inside Grok Bot, and a Remote HTTPS setup that requires OAuth cannot use their bearer
credential.) The [official submission page](https://x.ai/bot/marketplace/submit-a-plugin) accepts public
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
sh "$Y" --home /workspace/yobiru-data onboard
sh "$Y" --home /workspace/yobiru-data doctor
```

`/workspace/yobiru-data` is a suggested home for a new installation, kept apart from the package folder
`/workspace/yobiru-<version>/` (see [visible folders](#prefer-visible-folders-under-workspace));
preserve an existing installation's actual home. These checks identify missing Python, Tailscale,
sign-in, receiver or delivery configuration. They do not install software or change permissions. Yobiru
needs a usable Tailscale interface, not just a userspace proxy. If the computer lacks the required
privileges or `/dev/net/tun`, follow the reported administrator action instead of bypassing the host's
restrictions.

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

## Answering a public bot's chat (Friends)

With [public bots and Friends](PUBLIC_BOTS.md) from 0.6.0, a Grok Bot can answer chat sent to one of your
published bots: it holds only that bot's native binding credential and uses the `yobiru-friends-host` skill
(`job list`, `job claim`, `job reply`), and a connector can wake it through a routine webhook with a job id only.
It works like direct messages on X: people your bot follows and people you accepted reach the Grok Bot directly and
get its replies; everyone else's message waits in your message requests, which you accept, ignore or block (with
the owner credential, kept off the Grok computer where you can). Grok bots of one account share
files, installed tools, browser logins, connected apps and shared memory, and one bot has one conversation, so a
dedicated bot is neither a privacy boundary nor a per-caller thread; Yobiru keeps each caller's conversation and
hands the bot its transcript. Automatic answering is not available. Not yet tested on a Grok Bot: in a first
attempt the selected bot waited for its owner's approval in the Grok main chat before running the candidate CLI.

## Recover after a rebuild

Files stored durably and running processes are different things. A rebuild may retain a data home
while removing installed packages or stopping the receiver. Start with `onboard`, `doctor` and
`update status` against the existing home. Inspect the daemon's existing state and startup definition
before reinstalling or signing in again. Restore missing prerequisites through the host's normal
setup path; any needed sign-in remains the owner's action. Then restore the intended Yobiru receiver.

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
recovery steps and proposed regression coverage. These runtime fixes are not part of 0.5.2 or 0.5.3.

### Preserve Tailscale state and startup

Grok Bot documents `/workspace` as durable storage for project files. It separately warns that image
updates recreate the computer and login state may need restoring. Enterprise Team Setup scripts run
on boot and daily, but require the corresponding account capability. A saved script or a process
started with `nohup` does not establish that this account will run it after a rebuild.
Sources: [workspace files](https://cursor.com/docs/grok-bot/work#files-and-the-workspace),
[private networks and Team Setup](https://cursor.com/docs/grok-bot/private-networks).

Keep these four layers distinct:

| Layer | What must survive or be restored |
| --- | --- |
| Software | Verified Tailscale and Yobiru packages; reinstalling binaries does not restore identity. |
| Tailscale identity | The complete actual daemon state and any separately configured state backend. |
| Yobiru data | The original data home, including all agents, webhook files, sessions, permissions and request records. |
| Startup | A supported recovery/service definition that reuses those same paths and starts the intended processes. |

`tailscaled --statedir` selects a directory containing configuration, keys and other state.
`--state` may select another file or backend; when omitted, its file is `<statedir>/tailscaled.state`.
Inspect the actual process and service configuration instead of assuming a default path or copying
only that file. State encryption can depend on the local TPM, so moving files does not guarantee
they can be reused after a host rebuild. See the [daemon reference](https://tailscale.com/docs/reference/tailscaled).

Tailscale's [Docker state guidance](https://tailscale.com/docs/features/containers/docker/docker-params#ts_state_dir)
explains that persistent state prevents a container restart from registering a new node. Applying
that principle to a Grok Bot installation is an operational inference, not a verified guarantee about
Grok Bot's filesystem or rebuild behavior. `TS_STATE_DIR` is a Docker-container setting, not a
replacement for configuring an arbitrary Linux daemon.

For a new setup, choose storage that this host confirms survives the relevant image update and is
suitable for private daemon state. `/home/box` may be used only with that confirmation for the current
installation; the cited official guidance does not guarantee it. Existing Yobiru data homes remain
unchanged: `--home DIR` overrides `$TAILMSG_HOME`, otherwise the default is `~/.tailmsg`. Always pass
the selected absolute home in recovery commands; a different shell user can have a different default.

### Prefer visible folders under `/workspace`

The [workspace guidance](https://cursor.com/docs/grok-bot/work#files-and-the-workspace) recommends clear
project folders under `/workspace` and says normal updates and recovery keep its files. It does not say
how folders whose names start with a dot are treated. On 2026-10-21 a sender reported one event: after a
restart, visible folders under `/workspace` remained while hidden ones there (`.tailscale-state`,
`.tailmsg-backup`, `.config`) were gone. The kind of maintenance action was not independently
established, and no platform-wide rule about hidden paths has been verified. The sender also reports
moving its state and backup to visible folders and leaving a marker file. That change has not yet been
through another restart and has not been verified independently.

Until the host documents this, prefer visible names for what must survive. These are conditional
suggestions, not an automatic migration:

- A new Yobiru data home such as `/workspace/yobiru-data`, separate from the package folder
  `/workspace/yobiru-<version>/`.
- Within an authorized migration only: Tailscale state such as `/workspace/tailscale-state`, and a backup
  such as `/workspace/tailmsg-backup`.
- A visible name does not make data public. Create new private-data directories as 0700, and make state,
  secret and data files 0600 where applicable; this is no recursive `chmod` of a whole home or backup.
  When copying, preserve existing ownership and the executable modes that referenced adapters need, and
  leave the permissions of a live home unchanged. Keep secrets out of messages and repositories.
- Do not rename or move an existing running home (for example `~/.tailmsg`) or live daemon state blindly.
  Change paths only through the sequence below.

A temporary runtime home is not a durable backup. A backup is a complete, consistent copy taken while
nothing writes to the home: the request ledger, attachments and results, every agent's settings, the
webhook and other files they reference, and the permissions. Never run a second receiver on a backup, and
never overwrite a healthy home with an older copy. A restore script must name the original home, the
backup source and the state paths explicitly. Yobiru provides no migration or restore script.

The next rebuild the owner would perform anyway is the time to check the result. Do not restart only to
test. Verify that:

- the chosen folders, including any marker file, survived intact;
- Tailscale kept the old node's stable identity;
- every configured agent and permission is unchanged;
- startup ran the intended processes;
- a harmless tracked request completes.

A restarted daemon or a surviving marker alone is not that acceptance.

For an existing installation, use this sequence only within the owner's migration/repair authorization:

1. Read the actual daemon/service/supervisor configuration, state paths and socket. Record the current
   node identity, startup definition and a rollback plan locally. Do not print key/state contents or
   copy them into a plugin, repository, issue or message. Inventory the original Yobiru home and all
   agents' bindings and webhook references without exposing their secrets.
2. Confirm destination persistence, access restrictions and how the same logical machine is restored.
   Coordinate the network interruption using a working local recovery path. Stop the existing daemon
   through its manager and prevent a second startup while taking a consistent private backup. Do not
   copy a live state file or start a replacement alongside the original.
3. Copy the complete actual state directory and any separately configured file state while stopped,
   retaining ownership, restrictive modes and the backup. Update the same service or supported setup
   definition with explicit `--statedir=<CONFIRMED_STATE_DIR>` and, if used, the intended `--state` path.
   Preserve required socket and other settings. External/managed state backends and host-bound
   encryption need their own supported recovery method; do not convert them blindly to local files.
4. Start one daemon through that manager. Verify its state selection, socket, authenticated node
   identity and chosen hostname. If identity differs or login is required, stop and report that fact;
   do not clone another host, force logout, supply a new auth key, disable expiry or bypass ACLs.
   [Key expiry](https://tailscale.com/docs/features/access-control/key-expiry) can still require owner
   reauthentication even when files survive.
5. Restore only the intended receiver using the original explicit Yobiru home and unchanged bindings.
   Compare its `/info` identity with current Tailscale identity; restart that receiver if it retained a
   stale name after a change. Verify receiver version, session and all agent adapters, then a harmless
   tracked request and final result with an owner-selected peer. For push transport, verify both
   directions. For one-way shared connections using `request-pull-v1`, verify the visitor uploads,
   polls the final result and downloads the result files with matching hashes; no visitor receiver
   or reverse connection is required. See [one-way sharing](SHARING.md). Reconcile existing uncertain
   requests instead of rerunning them.

The skills provide this procedure; Yobiru does not automatically migrate Tailscale state or install
a host recovery script. Report separately what was configured, whether identity was retained and
whether a real image rebuild was observed. Do not reboot merely to test it. A successful manual
package command, native skill discovery and successful rebuild recovery remain separate evidence.

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
