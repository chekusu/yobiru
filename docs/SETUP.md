# Setting up Yobiru on a new machine

**English** | [简体中文](SETUP.zh-CN.md) | [日本語](SETUP.ja.md)

Installing the Yobiru plugin runs nothing: there is no install hook. A working setup needs Tailscale,
a configuration, a durable receiver, a way to wake each agent, and (optionally) the session each agent
uses. `yobiru onboard` checks all of it and prints the next steps for this platform; it is read-only
and works before anything exists. The `yobiru-setup` skill follows the same steps when you tell an
agent "set up Yobiru".

Tailscale facts below come from the official pages, checked on 2026-10-11:
[Linux](https://tailscale.com/docs/install/linux), [macOS](https://tailscale.com/docs/install/mac),
[Windows](https://tailscale.com/docs/install/windows), [downloads](https://tailscale.com/download),
and the official Linux script <https://tailscale.com/install.sh>.

## How to run the commands

The plugin puts no `yobiru` program on PATH. Below, `yobiru` stands for the plugin's launcher called by
its quoted absolute path, as in the README's `Y` convention:

```sh
Y="/absolute/path/to/yobiru/scripts/yobiru"     # inside the installed plugin or an unpacked release
sh "$Y" onboard
```

On Windows: `py -3 "C:\path\to\yobiru\scripts\yobiru.py" onboard`. A non-default data home goes
before the command: `sh "$Y" --home /workspace/yobiru-data onboard`.

## Who does what

| Step | Agent (after you asked for setup) | You |
|---|---|---|
| Install Tailscale | Runs the official Linux script with the host's root/sudo | macOS/Windows app install and approvals; anything needing an administrator |
| Sign in | Runs `tailscale up` on Linux and shows you the login URL | Open the URL (or the app) and choose the account and tailnet; never paste auth keys into chat |
| Peers and names | Applies the plan | Decide who may send and what agents are called |
| Receiver service | Installs and starts the printed definition | — |
| Delivery and sessions | Configures the adapter and binding you chose | Name the existing session for each agent |

Yobiru never changes Tailscale ACLs, shields-up or DNS. New installations accept authenticated
Tailscale peers permitted by those network controls, except local deny rules. Message access never
authorizes task execution or wake-ups. Ordinary
`help`, `doctor`, `status`, `onboard`, `agents` and `update check` are read-only.

## 1. Tailscale

- **Linux**: `curl -fsSL https://tailscale.com/install.sh | sh` (root or sudo), then `sudo tailscale up`,
  which prints a login URL. `onboard` reports a blocked host instead of guessing when there is no root
  or sudo (ask the administrator), no `/dev/net/tun` (containers, some cloud sandboxes: userspace
  networking gives the receiver no Tailscale address to bind, so use a TUN-capable host), or no
  systemd to keep `tailscaled` running (start it under a supervisor).
- **macOS**: install the Tailscale app from the Mac App Store or the download page, approve the
  system extension / VPN configuration prompt, and sign in from the menu bar. The app's CLI is
  `/Applications/Tailscale.app/Contents/MacOS/Tailscale`; if `tailscale` is not on PATH run
  `yobiru setup --tailscale-cmd /Applications/Tailscale.app/Contents/MacOS/Tailscale` (add
  `--replace tailscale_cmd` for an existing config).
- **Windows**: run the installer (administrator rights) and sign in from the notification-area icon.
  If the CLI is not on PATH: `yobiru setup --tailscale-cmd "C:\Program Files\Tailscale\tailscale.exe"`.
  In PowerShell call it as `& "C:\Program Files\Tailscale\tailscale.exe" status`. Use
  `py -3 "<plugin>\scripts\yobiru.py"`; the Codex bridge is not available on Windows.

`doctor` names the state it finds: not installed, daemon not running, permission denied, logged out,
stopped, waiting for device approval (a tailnet administrator approves it), or running.

On a cloud computer that may be rebuilt, also review [Tailscale state and startup](GROK_BOT.md#preserve-tailscale-state-and-startup).
Keeping binaries or a stable hostname is insufficient: inspect the daemon's actual `--statedir`, any
separate `--state` backend and its startup definition. Preserve private identity state on storage the
host confirms survives the relevant rebuild, and use a supported startup mechanism. Keep the existing
Yobiru data home and every agent's webhook, session and permissions. Any state relocation needs a
coordinated, authorized stop/backup/reconfigure/start with identity verification; the plugin does not
do this automatically. `onboard` and `doctor` cannot certify persistence or recovery after a rebuild.

## 2. Peers and agent names

```sh
yobiru setup --agent NAME --self-agent NAME --default-agent NAME          # plan
yobiru setup --agent NAME --self-agent NAME --default-agent NAME --apply
```

New configurations use `tailnet` mode without a per-peer allow list. To restrict a new setup, add
`--allow PEER` (hostname, FQDN, `tag:NAME` or `user:LOGIN`); `--send-only` starts with an empty allow
list. Existing access policies are preserved. Changing an identity needs `--replace FIELD`; changed
configurations are backed up.

For an existing installation, `yobiru policy show` explains the effective mode. To migrate a legacy
allow list, preview `yobiru policy set --mode tailnet`, then add `--apply` when the owner has authorized
that policy. `--deny RULE` replaces the entire deny list; repeat it for several rules. Deny wins in
both modes. A 0.4 receiver and bridge reload policy for the next authorization check, so a policy-only
change needs no restart. The initial runtime upgrade still needs a restart. See
[0.4 migration](UPGRADING-0.4.md).

## 3. Durable receiver

```sh
yobiru install-runtime --apply
yobiru service-template systemd-user     # or systemd, launchd, windows (onboard names the right one)
```

Install the printed definition where its header says and start it. To receive, shields-up must be
off and your tailnet policy must let the chosen peers reach `tcp:7766`.

## 4. How each agent is woken, and which session it is

These are separate. Delivery: the Codex bridge (`yobiru codex-bridge install --thread-id UUID ...`) or
`yobiru wake-config --agent NAME --exec PROGRAM | --webhook ...`. Without an adapter messages wait in
`inbox --pending`. Session: `yobiru session bind --agent NAME --session-id EXACT_ID
[--display-name ALIAS] [--confirmed-title TITLE]`, dry run then `--apply`. A name alone never selects
or renames a session; with the Codex bridge the dispatcher UUID is reported automatically and a
conflicting binding is refused.

For a Codex dispatcher using `workspace-write`, also follow [minimum sandbox access](CODEX_SANDBOX.md):
use absolute writable roots for the configured inbox and handled directory, plus outbox only for
attachment replies. Prepare these directories before starting the restricted session. Keep policy,
runtime and queue files read-only; never make the entire data home writable. Network access is only
needed for authorized replies/downloads, and does not override `allow_replies=false`.

## 5. Verify

```sh
yobiru doctor
yobiru agents --online
yobiru send PEER/AGENT "hello"
```

`agents` shows each machine's receiver state, whether you may send there, its Yobiru version, and each
agent's session name (with its source) and exact id. To test a wake-up, send with `--wake now` from a
peer whose wake policy allows `now`, then check `show ID` for `wake_delivery` on the receiver.

## Updating later

Update the plugin the way it was installed: the Codex/Cursor/Grok Bot plugin page or marketplace, a
new official release archive (verify it against `SHA256SUMS` and extract it into a new folder, never
over the data home), or a checkout of an official tag. Then run the NEW package's `scripts/yobiru`
(`yobiru version` shows the new version); the data home is not touched.

See the `yobiru-upgrade` skill and `yobiru update --help`: a plugin update does not change the
durable runtime (`install-runtime --upgrade --apply`, then restart the receiver), and
`yobiru update notify` tells your other known installations once this machine runs a published
release.

Existing installations keep legacy allow lists; policy migration is separate from installing the new
version. `update maintain --apply` records update checks and retries only previously queued upgrade
notices. For an opt-in 15-minute/startup job, generate `update reminder-template systemd-user
--retry-notices` (or `launchd`/`cron`), then install the printed definition. Regenerate it when the
plugin path changes. It never installs software or changes policy. See [0.4 migration](UPGRADING-0.4.md).
