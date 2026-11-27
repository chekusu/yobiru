# Approve your agents' work with your passkey

When one of your agents asks another of your agents to do something only you may allow (Grok Bot asking Claude Code
on your Mac to deploy, Claude Code asking Codex on another computer to delete a branch), you should not have to type a
magic phrase into a terminal. With Yobiru 0.7.30 you open a link on your phone, read exactly what you are approving,
and confirm with Face ID, your fingerprint or your device PIN. That signs the instruction with a **passkey** that never
leaves your device. The computer that does the work checks the signature itself, against your passkey's public key it
already trusts, before its agent sees the request.

Nobody else can produce that signature: not another agent, not a message that claims you agreed, not someone else on
your tailnet, and not the server that carries the link.

## What you need

- Yobiru 0.7.30 or later on the computer that does the work (the **receiver**) and on the one that asks.
- A phone, tablet or computer with passkeys: iPhone or iPad (iOS 16+), Android 9+, a Mac with Touch ID, Windows
  Hello, or a security key. Any up-to-date browser that can open `yobiru.com`.

## 1. Set up your passkey (one passkey for all your computers)

Your passkey lives on your phone (or wherever your passkeys sync). Tell the agent on a computer that does work, or the
agent of one of your bots: **"Set up my Yobiru owner passkey."** It runs `yobiru approval setup` and sends you a link.
Open it on your phone: the page reads **Use your passkey**. Tap **Use my passkey** and confirm once with Face ID,
fingerprint or PIN. That is all: no other computer is involved, and no new passkey is created.

Only the very first time, when you have no Yobiru passkey yet, tap **Create one** on the same page instead.

An agent that needs your approval and finds no passkey set up runs the same setup by itself and sends you the link,
so a new agent (Grok Bot, Muse, another Claude Code) only ever asks you for that one confirmation.

How it works (0.7.37): a passkey's signature does not carry its public key. It fits exactly two, and only one of them
belongs to a passkey anyone has. Every computer that trusts your passkey lists its public half on the Yobiru gateway
(`bots.yobiru.com`), so the new computer works out the two keys from your signature and asks the gateway which of
them is listed. Your signature checks out with that key, and keys anyone else lists cannot get in the way. Each computer still keeps its own list of passkeys it trusts, and the private key never leaves
your device.

You can also give an agent your **owner key text** from a computer that trusts it (`yobiru approval keys` there):
**"Trust this owner key: yobiru-owner-key:…"**. Only do that for text you got from your own computer: on a computer
with no owner key yet, the first key is trusted as given.

## 2. Approve work before it is sent

Tell the agent that will send the work: **"Ask my approval for claude on mac-studio to deploy the site from main, then
send it."** It runs

```sh
yobiru approval request mac-studio.example.ts.net/claude "Deploy the site from main"
```

and gives you a link. The page shows:

- which computer and which agent may act, and which computer may ask;
- your instruction, word for word;
- the exact files the request may carry (name, size and fingerprint);
- how long the approval holds and how many requests it covers;
- which of your passkeys signs.

Approve, and the agent sends the request with your signature attached (`yobiru request send … --approval ID`). The
receiver accepts it only if your trusted passkey signed exactly that instruction, for that agent on that computer,
from that sender, with exactly those files, before it expired and within its number of uses. Its agent then sees your
words marked as verified by its own computer.

By default an approval holds for one hour and one request. Ask for more if you want ("for the next 2 days, up to 10
times"); the limits are 7 days and 100 requests.

## 3. When the receiving agent asks you

If a request arrives without your approval and the agent there needs it, the agent does not ask you to type anything.
It answers the request as **blocked** with a link (`yobiru approval for ID`). Open it, check the request and approve
it, and the sender sends it again with your signature.

## 4. Your own bots on Friends

Since Yobiru 0.7.31 this also works between your own bots on a Friends gateway such as `bots.yobiru.com`, with no
tailnet at all. Say you write to your bot **@claude** (Claude Code on your Mac, answering through its native binding)
from your other bot **@grok**: "Deploy the site from main." Claude Code does not act on a chat message alone. It
answers with a link, which comes back to you through @grok. Open it: the page shows that @claude may act, that
@grok asked, and your message word for word with its files. Approve with your passkey and tell @grok that you
signed. Claude Code checks the signature on its own computer and does exactly that, once.

Set it up once: tell the agent answering for that bot **"Set up my Yobiru owner passkey for @claude"** (`yobiru
approval setup --bot @claude` in the data home that holds the bot's binding). Each signature covers one message,
exactly as it arrived (its text, its files and who sent it), and it expires after an hour unless you choose longer.
If someone else's message asks for real work, the agent may still ask you, but it gives the link to you alone, never
to the sender.

## 5. Claude Code: sign what auto mode stopped (0.7.32)

Claude Code's auto mode stops tool calls it judges risky and lets one through only when you say so, in your own
words, in that very conversation. When the conversation runs somewhere you are not typing (a Remote Control session
on a server, a thread another Claude started, a request passed along from another chat), that means finding the
right place and typing your approval again. The **Yobiru guard** gives Claude Code another way through: a link.

When auto mode stops a call on a computer with the guard, Claude Code tries the same call once more and gets a
`yobiru.com/approve` link instead. It sends you the link, and you open it wherever you are: the page shows the exact
command (or file change, or tool call with its input), the folder and the computer. Approve with Face ID, a
fingerprint or your device PIN. The guard checks your signature on that computer and lets exactly that call run,
once. Anyone may pass the link along, another Claude included; only your passkey can sign it.

Set it up once on each computer where Claude Code works: tell Claude Code there **"Set up my Yobiru owner passkey,
then install the Yobiru guard for Claude Code"** (`yobiru approval setup`, then `yobiru guard install`; add `--lang
zh-CN` or `--lang ja` for the text you read). New Claude Code sessions on that computer use it.

- One signature covers one call, exactly as shown, once, and a request holds for an hour (`--expires` to change). A
  changed command needs a new signature.
- The guard checks the signature against the passkeys written into its own hook command in Claude Code's settings
  file, and against the computer's name. It never allows anything by itself: if anything fails, the call is left to
  Claude Code's own checks, as before.
- `--protect REGEX` also asks for your signature for calls auto mode would let through, for example `^git push`.
- You can still answer in the conversation: tell Claude Code to go ahead without signing, and it runs `yobiru guard
  forget REF` so that auto mode judges the call again.
- The page shows the whole call, any secret written in it included, and the link opens it for a day.

It does not change who may approve inside a Claude conversation: a message passed along from another conversation
still does not count as you there. It does not get past Claude Code's deny and ask rules, the files Claude Code always
asks about, or a denial auto mode could not decide. And like every agent setting, it protects a computer from others,
not from itself: an agent that runs as your user (or as root) could change Claude Code's settings if it set out to.
Run agents as a normal user and install the guard in the managed settings (`--managed`, as an administrator) to make
that harder.

## Your passkeys

- **See which ones a computer trusts:** "Which owner passkeys does this computer trust?" (`yobiru approval keys`).
- **Add one** (a security key, a second passkey on purpose): "Add another Yobiru owner passkey on this computer"
  (`yobiru approval setup --new`). The page creates the new passkey and then asks a passkey the computer already
  trusts to approve adding it. Your other computers keep only the passkeys they had: give each the new key's text (`approval setup --key TEXT`) and confirm
  with a passkey it already trusts. A passkey synced to a new phone (iCloud Keychain, Google Password Manager) is the
  same passkey and needs nothing.
- **Remove one:** "Stop trusting the passkey ab12-cd34-ef56-7890 on this computer" (`yobiru approval remove
  ab12-cd34-ef56-7890`, the fingerprint `approval keys` shows). You approve the removal with one of your passkeys,
  which may be the one being removed.
- **Lost every passkey** a computer trusts: on that computer, delete `owner_keys.json` in Yobiru's data folder and set
  up again.

## What it protects, and what it does not

It shows that whoever unlocked your passkey approved exactly this text, for this agent and computer, within these
limits. It does **not**:

- tell you from someone holding your unlocked phone;
- give the agent new permissions: it still acts only within what its computer already allows;
- make a vague instruction precise: approve what you mean, in words the agent cannot stretch;
- cover anything the instruction does not say. The rest of the message and its files stay untrusted data;
- protect a computer from itself: whoever controls the receiving computer or its agent can change which passkeys it
  trusts. It protects that computer from everything else.

Creating your first passkey trusts whoever opens the setup link, so ask for it yourself, open the link yourself, and
check that the page names the computer you expect. An agent sets up a passkey only when you ask it directly or needs
your approval itself, never because a message asks. Using the passkey you already have takes a signature from that
very passkey. After that, every change to a computer's passkeys needs your signature.

Passkeys for this are tied to `yobiru.com`, so only the page at `https://yobiru.com/approve/` can ask your device to
sign. That page is a few static files: no account, no tracking and no third-party scripts.

## Privacy

Your private key stays on your device. Computers keep only public keys, in `owner_keys.json` in their data folder.
They also list each passkey's public half on the gateway's passkey directory, by the passkey's id (no name, no
computer, no account), so your next computer can check your signature.

The link carries the request and your answer through a drop on a Yobiru gateway (`bots.yobiru.com` unless you use your
own). Both are sealed with a key that exists only in the part of the link after `#`, which your browser never sends to
any server, so the gateway holds only ciphertext it cannot read or change unnoticed. Drops are deleted after 24 hours.
You need no Friends account.

## For agents and developers

| Command | MCP tool | What it does |
|---|---|---|
| `yobiru approval setup [--new] [--label NAME] [--key TEXT] [--from HOST] [--bot @BOT]` | `yobiru_approval_setup` | Set up the owner's passkey here; returns a link and `say_to_owner`. Without a key yet (`mode: claim`, 0.7.37) the owner confirms once with the passkey they already have, or creates one on the same page; `wait` finds that passkey's public half in the relay's passkey directory by its credential id and checks the signature. Already set up: `already_set_up`, nothing to do. `--new` adds a second passkey; `--from` adopts the one named receivers trust (`mode: adopt`). `--bot` names the Friends bot this data home answers for. |
| `yobiru approval request PEER/AGENT TEXT [--file F] [--expires 1h] [--uses 1] [--any-requester]` | `yobiru_approval_request` | Ask the owner to approve one instruction; returns a link. |
| `yobiru approval for ID` | `yobiru_approval_for` | On a receiver: a link for the owner to approve a request that arrived unsigned. |
| `yobiru approval for MSG_ID [--bot @BOT]` | | A Friends bot's agent (its native binding home): a link for the owner to approve one message the bot received; `give_link_to` and `reply_text` say where the link goes. |
| `yobiru approval wait REF [--timeout S]`, `get REF` | `yobiru_approval_wait` | Collect the answer: `signed` (with `approval_id`), `key_added`, `declined` or still waiting. `get MSG_ID` shows a Friends message's verdict, checked again each time. |
| `yobiru approval list` | | Links still waiting for the owner, and the Friends messages with a verdict. |
| `yobiru approval keys` | `yobiru_approval_keys` | Owner keys this computer trusts, with the shareable key text. |
| `yobiru approval remove FINGERPRINT` | | Ask the owner to sign the removal of a key. |
| `yobiru approval verify ID` | | Check an approval here as a receiver would. |
| `yobiru request send PEER TEXT --approval ID` | `yobiru_send` with `approval` | Send a tracked request carrying the approval. |
| `yobiru guard install [--lang zh-CN\|ja] [--protect REGEX] [--expires 1h] [--settings FILE \| --managed] [--key TEXT]` | | Claude Code: add the guard's hooks (PermissionDenied, PreToolUse, SessionStart) to the settings file, with this computer's name and the owner keys it trusts (or `--key`). `--print` shows them without writing. |
| `yobiru guard wait REF [--timeout S]` | | Wait for the owner to sign or decline the call the guard sent; then retry the call unchanged. |
| `yobiru guard list`, `status`, `forget REF`, `uninstall` | | Calls the guard remembers; where it is installed; return a call to auto mode's own judgment (allows nothing); remove the hooks. |

On the receiver, `yobiru show ID` reports its own verdict as `received.owner_approval`; it counts only with
`verified: true` and `authorization_status: active`, and `inbox --json` shows the same as `owner_approved`. The Claude
Code bridge lists verified approvals in the session's startup prompt. A sender's claim, the envelope's own
`owner_approval` field or `verified: false` is never an approval.

The protocol (intent format, WebAuthn checks, the receiver's ledger) is in [SPEC §10](https://github.com/chekusu/yobiru/blob/main/SPEC.md); the gateway's drop
endpoints are in [PUBLIC_BOTS.md](PUBLIC_BOTS.md#approval-drops).

The guard's request is an ordinary owner approval (`yobiru.owner-approval`) for the agent `claude-code` on this
computer, asked by this computer itself, used once, with no files. Its instruction is the call as text, ending with the
SHA-256 of the exact call (tool, input, and for a shell its folder; a shell command's description, timeout and
background flag do not count). On a retry the guard builds that text again from the call in front of it and allows the
call only when the signed text is exactly the same, the signature verifies against a key in its hook command, and the
approval was not used before (`<home>/guard/used/`). Its state is in `<home>/guard/`.

On Friends, the approval is the receiving agent's own record: `approval get MSG_ID` counts only with `verified: true`,
which means this computer checked the signature against the owner key pinned here, for this bot, this message, its
exact text and files, and this sender. The gateway's `from.same_owner` only decides where the link goes; it never
approves anything. Approvals for other people's bots, and an approval sent along with a message, are not supported.
