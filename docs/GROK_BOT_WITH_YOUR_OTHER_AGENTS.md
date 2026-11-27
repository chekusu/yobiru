# Let Grok Bot work with Codex or Claude Code on your other computers

With **Yobiru**, Grok Bot sends requests and files to Codex, Claude Code or another agent on your own computers and
brings back their results, over your own **Tailscale** network. It uses Grok Bot's official Tailscale plugin, so
nothing is installed on Grok Bot's computer except Yobiru itself, and there is no relay to host and no Yobiru account.
Each agent stays in its own app: you keep talking to Grok Bot in its conversation, and the other agent answers in its
own.

Yobiru runs on the cloud computer attached to Grok Bot's conversation, not on the phone that shows it. The examples use
the fictional computer `mac-studio.example.ts.net` with an agent named `codex`. Use your own.

## What you need

- Grok Bot 0.49 or later, with its official Tailscale plugin available on your account.
- A tailnet you own or administer. A company tailnet must first be enabled by a team admin at cursor.com
  **Integrations → Tailscale** (Tailscale Premium or Enterprise).
- On each of your other computers: Tailscale, Yobiru 0.7.26 or later with its receiver running, and an agent such as
  Codex or Claude Code. The [Codex and Claude Code guide](CODEX_AND_CLAUDE_CODE_ACROSS_MACHINES.md) sets those up.

## 1. Connect Grok Bot's Tailscale plugin

In the Grok Bot desktop app, open **Connect apps → Your plugins → Tailscale** and click **Authenticate**. Tailscale's
consent screen opens, and Grok Bot's computer joins your tailnet as a `grokbot-…` device. Grok Bot shows an
Allow/Deny card before it adds a tailnet or removes a device.

Do not install a Tailscale client on Grok Bot's computer: it is wiped when Grok Bot replaces that computer. Applying a
Tailscale tag to the `grokbot-…` device is currently reported to disconnect it
([cursor/plugins#1140](https://github.com/cursor/plugins/issues/1140)), so restrict it with tailnet grants instead.

## 2. Ask Grok Bot to set up Yobiru

Give Grok Bot `https://yobiru.com/install.md` and ask it to install Yobiru. Yobiru has no public marketplace listing
yet, so Grok Bot uses a team marketplace your account already offers, or a clone or the release archive under
`/workspace`, checked against the release's `SHA256SUMS`. If the plugin is already installed, say: **"Use
yobiru-grok-bot to connect this conversation's computer to my other agents."**

Grok Bot's computer needs Yobiru 0.7.27 or later. Setup keeps its data in a visible, private folder such as
`/workspace/yobiru-data` and uses the **connector route**: Grok Bot installs nothing else and runs no receiver.

## 3. Send work to your other agents

Tell Grok Bot: "Send brief.md to codex on mac-studio for a review and bring back the result." Under the hood it runs (`$Y` is Yobiru's launcher on that computer):

```sh
sh "$Y" --home /workspace/yobiru-data request send mac-studio.example.ts.net/codex 'Please review this brief.' --file ./brief.md
sh "$Y" --home /workspace/yobiru-data request status REQUEST_ID --refresh --json
sh "$Y" --home /workspace/yobiru-data fetch RESPONSE_ID
```

Grok Bot addresses your computers by their full MagicDNS name. A receiver in `tailnet` mode accepts the `grokbot-…`
device like any device your tailnet policy lets through; in allowlist mode, add the host name that `doctor --peer`
prints. The request is tracked, and Grok Bot collects the final result and any files by checking its status.

## Messages that should reach Grok Bot

The plugin connection is outbound only: nothing on your tailnet can connect to Grok Bot's computer. To send Grok Bot a
message, use [Friends](TALK_TO_A_FRIENDS_AGENT.md): your Grok Bot gets a link on `bots.yobiru.com`, and your other
agents, or your friends' agents, write to it there. Friends needs only outbound HTTPS. Grok bots of one account share
files, tools, logins, apps and memory with each other; neither Yobiru nor Friends isolates them.

## Where it stands

On 2026-11-12, a Grok Bot on Yobiru 0.7.27 with the Tailscale plugin connected sent a tracked request with a file to a
0.7.26 receiver on the owner's computer. The receiver identified the caller as the owner's `grokbot-…` device, and Grok
Bot fetched the result and its file 76 seconds later, most of it the time taken to answer by hand. The same day, an
existing installation that ran its own Tailscale client was moved to the plugin in place and kept its Friends account,
contacts and request records; its first request after the switch was refused until the receiver could confirm its own
identity, and a retry 40 seconds later went through. Earlier, on 2026-10-21, Yobiru's Claude Code MCP server on a Mac
exchanged a tracked request and messages with a Grok Bot cloud computer (driven over stdio, not from a signed-in Claude
Code session), and on 2026-10-22 a native Grok Bot passed the maintainers' Friends acceptance run.

Not verified yet: installing Yobiru from the Grok Bot marketplace, and recovery after Grok Bot replaces its computer.
Details: [Grok Bot guide](GROK_BOT.md#connect-with-grok-bots-official-tailscale-plugin),
[what has been verified](VERIFICATION.md), [platform notes](PLATFORMS.md).

[简体中文](GROK_BOT_WITH_YOUR_OTHER_AGENTS.zh-CN.md)
