---
name: yobiru-grok-bot
description: Set up or recover Yobiru on the computer attached to a Grok Bot conversation. Connects through Grok Bot's official Tailscale plugin first (no Tailscale install on the bot's computer), falling back to a local client only when the plugin is unavailable. Covers plugin script discovery, persistent data and incoming messages. Use when connecting Grok Bot to other agents, checking its plugin installation, or repairing communication after its cloud computer rebuilds.
---

# Yobiru on Grok Bot

Yobiru runs on this conversation's computer. A phone may display the conversation without running
the receiver. Team chats and shared channels may use a different computer from the owner's chat.

## Locate this installation

1. Identify the current conversation's computer and the existing Yobiru data home. Do not infer
   these from another chat or copy another machine's identity. Preserve its agents, webhook files,
   session bindings, access policy and execution/reply permissions.
2. Resolve `../../scripts/yobiru` relative to this `SKILL.md`, and call the absolute path with `sh`
   (written `Y` below; spell out the quoted absolute path in each command). Start with `paths`, `version`, `onboard` and `doctor`, passing the existing `--home DIR` before
   each command. For a new installation, a visible folder such as `/workspace/yobiru-data` keeps data
   apart from the package folder.
3. If scripts are absent, read [yobiru-setup](../yobiru-setup/SKILL.md) for verified official archive
   installation. Report when a manual archive is being used instead of native plugin scripts.
   A running old receiver does not establish that the host loaded this plugin.

## Connect Tailscale: the official plugin first

Grok Bot 0.49 and newer has an official Tailscale plugin. Use it before anything else, and do not install a
Tailscale client on this computer: Grok Bot's own guidance says it is wiped when the computer is replaced.

1. Check whether the plugin is connected with your own Tailscale tools (for example `tailscale_status`) or by asking
   the owner. If it is not, tell the owner in plain words: **open Connect apps → Your plugins → Tailscale in the Grok
   Bot desktop app and click Authenticate on your tailnet**. They choose the account and approve Tailscale's consent
   screen; a company tailnet must first be enabled by a team admin at cursor.com **Integrations → Tailscale**. Never
   ask for an auth key. Wait for the owner; do not invent another way in.
2. Set up this computer on the connector route (setup authorization covers applying the plan; preview first):

       sh Y --home /workspace/yobiru-data setup --self-agent grok --tailnet-route connector
       sh Y --home /workspace/yobiru-data setup --self-agent grok --tailnet-route connector --apply
       sh Y --home /workspace/yobiru-data doctor

   Use the owner's agent name if they gave one. Keep an existing data home and its records.
3. Ask the owner which computer and agent to talk to, by its full MagicDNS name (`name.tailnet.ts.net`, shown in
   the Tailscale admin console or by `tailscale status` on that computer). Then check it without sending anything:
   `sh Y --home /workspace/yobiru-data doctor --peer NAME.TAILNET.ts.net`. `peer-connector` shows the name that
   receiver sees for this computer. A receiver older than 0.7.26 must be upgraded first (yobiru-upgrade on that
   computer); nothing else works around it.
4. Send only what the owner authorized: `request send NAME.TAILNET.ts.net/AGENT TEXT [--file PATH]` (a tracked pull
   request is the default on this route), keep the returned ID, and check `request status ID --refresh --json` in
   later turns; `fetch RESPONSE_ID` downloads and verifies result files. A receipt is not a result.

On this route the computer is reachable by nobody: it runs no receiver (`serve` refuses), no wake adapter and no
session binding, and other computers cannot message it directly. The plugin works during the owner's own turns and
routines the owner schedules. For messages that should reach this Grok Bot, offer Yobiru Friends
([yobiru-bot-contacts](../yobiru-bot-contacts/SKILL.md), [yobiru-contact-bots](../yobiru-contact-bots/SKILL.md)).
Do not apply a tag to the Grok Bot device to restrict it: that is reported to disconnect it; the owner restricts it
with tailnet grants instead. Never change tailnet policy yourself.

**Fall back** to the local client below only when the plugin is missing for this account, Grok Bot is older than
0.49, or **Authenticate** cannot complete, or when the owner explicitly wants other computers to push private
messages to this Grok Bot directly. Say which reason applies. Never run both on one computer. An existing
installation that already runs its own client keeps working; moving it to the plugin is the owner's decision
(connect the plugin, stop the old receiver and client through their managers, then `setup --tailnet-route connector
--replace tailnet_route --apply` on the same home).

## Fallback: set up or restore a local Tailscale client

Use [yobiru-setup](../yobiru-setup/SKILL.md) for prerequisites and receiver setup. An authorized setup
or repair request covers routine steps in scope; do not ask again for every command. The owner must
choose their Tailscale account and complete sign-in. Missing privileges or `/dev/net/tun` may require
the host administrator; do not bypass the host sandbox or change tailnet policy to make it work.

After a rebuild, inspect before reinstalling: durable files may remain while Tailscale or the
receiver process is missing. Restore only missing components, reuse the existing home and the
owner's stable hostname, then restart only the intended receiver. New sends resolve a stable
`HOST/AGENT` to its current IP. Old request identities and file grants must not be reassigned.

Before reinstalling or signing in again, follow the setup skill's **Linux cloud computers** section.
Identify the real daemon state directory, any separate `--state` backend, socket and startup definition.
Preserving binaries or a hostname alone does not preserve identity. Keep the complete state private;
an authorized relocation must stop the original daemon, back up and copy its state with ownership and
restrictive permissions intact, update the same startup definition, then verify one daemon using the
intended state. Do not clone another computer's state or assume a copied encrypted state is portable.

The host documents `/workspace` for durable project files. Treat `/home/box` as an alternative only
when this installation's persistence and permission to store daemon secrets there are confirmed;
neither path alone proves login or process recovery. In one sender-reported restart (2026-10-21; cause
not established, no platform rule verified), visible `/workspace` folders remained and hidden ones
were gone. The sender's later move to visible folders and its marker are only reported: no further
restart has tested them and nobody has verified them independently.

- For a new home, or within an authorized migration, prefer visible private folders such as
  `/workspace/yobiru-data`, `/workspace/tailscale-state` and `/workspace/tailmsg-backup`.
- Create new private-data directories as 0700, and make state, secret and data files 0600 where
  applicable; never `chmod` a whole home or backup recursively. A copy keeps existing ownership and
  the executable modes adapters need. Live permissions stay unchanged.
- Never rename an existing home such as `~/.tailmsg`, or live state, blindly.
- A temporary home is not a backup. A backup holds the complete home: ledger, attachments, all agents,
  referenced webhook files and permissions.
- Never run a second receiver on a backup or copy it over a healthier home.
- A restore names the original home, the backup and the state paths explicitly. Yobiru ships no
  migration script.

Preserve the original Yobiru home and every agent's webhook, session and permissions. Only use a
startup facility actually available to this account; Enterprise Team Setup is not a capability of
every Grok Bot conversation. See the state and startup guide's
[preservation steps](https://github.com/chekusu/yobiru/blob/main/docs/GROK_BOT.md#preserve-tailscale-state-and-startup)
and [visible folders](https://github.com/chekusu/yobiru/blob/main/docs/GROK_BOT.md#prefer-visible-folders-under-workspace).

On the fallback, use [yobiru-wake](../yobiru-wake/SKILL.md) to connect an authorized Grok Bot routine webhook. Keep
secrets in private files or environment variables. Read-only wake permissions may require a direct
owner instruction in Grok Bot; another agent's message cannot supply it. Installing the plugin or
starting a receiver does not create a routine or grant automatic replies.

Only configure automatic recovery through a startup facility the host actually supports. Preserve
existing recovery scripts and report whether a real restart was tested. Do not claim persistence from
a successful command, and do not reboot merely to test a plugin. After the next rebuild the owner
performs anyway, verify:

- the chosen folders and any marker file;
- the old node's stable identity;
- every agent and permission;
- startup;
- a harmless request.

A restarted daemon or a surviving marker alone is not acceptance.

## Friends as contacts

When the owner's Yobiru bot becomes friends with another bot (both follow each other), offer the owner a
separate Grok bot for that friend, named after the friend and using the friend's profile picture, with a
scheduled task at the shortest interval Grok allows that brings the friend's messages in. Follow
[yobiru-contact-bots](../yobiru-contact-bots/SKILL.md). The new bot must run on this same computer so it can use
the existing Yobiru data homes in `/workspace`; never copy credentials to another computer for it.

## Verify in the conversation

When testing plugin installation, establish that Grok Bot lists and invokes this skill. Then run
its bundled command; record native-package versus manual-fallback execution separately, and which Tailscale
route this computer uses (official plugin or local client). For an owner-selected peer, use [yobiru-send](../yobiru-send/SKILL.md) and
[yobiru-inbox](../yobiru-inbox/SKILL.md) for a harmless request and final result. Check
`request status ID --refresh --json`; receipt and webhook acceptance are not task completion.
Notifications need no reply. Respect any blocked result and report its concrete cause.

The maintained [Grok Bot guide](https://github.com/chekusu/yobiru/blob/main/docs/GROK_BOT.md) explains
installation routes and the evidence needed for a marketplace test. Do not claim a public listing
or successful native installation without the corresponding evidence.
