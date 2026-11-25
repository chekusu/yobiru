# Let Codex and Claude Code on different computers message each other

With **Yobiru**, Codex on one computer and Claude Code on another send each other messages, JSON and files, and
reply, over your own **Tailscale** network. There is no relay or broker to host and no Yobiru account: each computer
runs a small receiver that accepts messages from your other Tailscale machines, and files are fetched from the
sender and checked for size and SHA-256 on arrival. Claude Code gets a local MCP server with tools for this; Codex
uses the plugin's skills. Each agent stays in its own app.

The examples use the fictional hostnames `agent-laptop` (Codex) and `research-node` (Claude Code). Use your own.

## What you need

- Tailscale installed and signed in on both computers, in a tailnet whose policy lets them reach each other on
  `tcp:7766`.
- Python 3.8 or later on both.
- A receiver that keeps running on each computer that should receive. Setup installs it as a service.

## 1. Install the plugin

Give each agent `https://yobiru.com/install.md` and ask it to install Yobiru, or run:

```sh
codex plugin marketplace add chekusu/yobiru     # on the Codex computer
codex plugin add yobiru@yobiru

claude plugin marketplace add chekusu/yobiru    # on the Claude Code computer
claude plugin install yobiru@yobiru
```

## 2. Ask each agent to set it up

In a new conversation on each computer, say: **"Set up Yobiru on this machine."** The agent checks Tailscale, the
configuration and the receiver, then walks through the missing steps. You choose the Tailscale account (you open the
login link; keys never go into chat), the agent names, and which existing session belongs to which agent.

Name the agents after what they are: `codex` on `agent-laptop`, `claude` on `research-node`. When a computer has an
agent named `claude`, Claude Code's MCP server sends as `claude`, so replies come back to Claude Code and not to
another agent on that computer.

## 3. Send something

Tell Claude Code: "Ask codex on agent-laptop to review brief.md and send back its findings." It uses the `yobiru_send`
tool and attaches the file. Tell Codex: "Send research-node/claude the test log and ask what failed." Under the hood
both run the same command:

```sh
yobiru request send agent-laptop/codex "Please review this brief." --file ./brief.md --timeout-seconds 900
```

A tracked request gets a receipt when it is stored, progress while the other agent works, and a final result
(completed, failed, rejected or blocked), so silence is never mistaken for success. Ask either agent for the
request's status at any time.

## 4. Read and reply

Tell the receiving agent: "Check my Yobiru inbox." It lists what arrived, fetches and checks the files, and replies
with text or result files. In Claude Code, received text reaches the model as data from another agent, never as an
instruction.

## Waking the other agent

By default a message waits in the inbox until that agent checks it. To have it picked up on its own:

- **Codex**: the Codex bridge queues messages into one existing Codex session (the Codex desktop app on macOS, or
  your own Codex app-server on Linux).
- **Claude Code**: the optional Claude bridge starts a visible background Remote Control session for new messages
  (macOS and Linux). See [the Claude bridge guide](CLAUDE_BRIDGE.md).

Being allowed to send never grants a wake-up, task execution or automatic replies: each receiving computer decides
which senders may wake which agent, with quiet hours and rate limits.

## When the other computer is not yours

Tailscale connects your own machines. To talk to an agent that belongs to someone else, use Friends instead: no
shared tailnet, just a link. See [let your agent talk to a friend's agent](TALK_TO_A_FRIENDS_AGENT.md).

## Where it stands

Each side has been run live. On 2026-10-21, Claude Code 2.1.306 loaded the plugin and listed its MCP tools on a Mac;
through that MCP server, a tracked request to a Grok Bot cloud computer completed in about 26 seconds, and a message
from Grok Bot was read and answered. That run drove the server over stdio, because the headless Claude Code session
stopped at sign-in.
On 2026-10-13, a real Codex session on Linux, woken through the Codex bridge, reported progress and a final result for
a test request. Codex and Claude Code have not yet been recorded together as one pair on two machines, and the Claude
bridge is covered by automated tests only. The Codex bridge does not run on Windows, and Claude Code's MCP server
needs `sh` on Windows. Details:
[what has been verified](VERIFICATION.md), [platform notes](PLATFORMS.md#claude-code), [setup guide](SETUP.md).

[简体中文](CODEX_AND_CLAUDE_CODE_ACROSS_MACHINES.zh-CN.md) · [日本語](CODEX_AND_CLAUDE_CODE_ACROSS_MACHINES.ja.md)
