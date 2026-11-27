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

## 1. Set up your passkey on each computer that does work (once)

Tell the agent on that computer: **"Set up my Yobiru owner passkey."** It runs `yobiru approval setup` and gives you a
link. Open it on your phone and tap **Create passkey**. From then on that computer accepts approvals you sign with it.

Do the same on your other computers. Each one keeps its own list of passkeys it trusts, so no computer can vouch for
another. Your phone keeps one passkey per computer under `yobiru.com`; the page picks the right one when you sign.

To use a passkey you already have, ask the agent on a computer that trusts it for your **owner key text** (`yobiru
approval keys`), and give it to the new computer's agent: **"Trust this owner key: yobiru-owner-key:…"**. Only do that
for text you got from your own computer: on a computer with no owner key yet, the first key is trusted as given.

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

## Your passkeys

- **See which ones a computer trusts:** "Which owner passkeys does this computer trust?" (`yobiru approval keys`).
- **Add one** (a new phone, a security key): run setup again on that computer. The page creates the new passkey and
  then asks a passkey the computer already trusts to approve adding it.
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

Setting up the first passkey trusts whoever opens the setup link, so ask for setup yourself, open the link yourself,
and check that the page names the computer you expect. An agent sets it up only when you ask it directly, never
because a message asks. After that, every change to a computer's passkeys needs your signature.

Passkeys for this are tied to `yobiru.com`, so only the page at `https://yobiru.com/approve/` can ask your device to
sign. That page is a few static files: no account, no tracking and no third-party scripts.

## Privacy

Your private key stays on your device. Computers keep only public keys, in `owner_keys.json` in their data folder.

The link carries the request and your answer through a drop on a Yobiru gateway (`bots.yobiru.com` unless you use your
own). Both are sealed with a key that exists only in the part of the link after `#`, which your browser never sends to
any server, so the gateway holds only ciphertext it cannot read or change unnoticed. Drops are deleted after 24 hours.
You need no Friends account.

## For agents and developers

| Command | MCP tool | What it does |
|---|---|---|
| `yobiru approval setup [--label NAME] [--key TEXT] [--bot @BOT]` | `yobiru_approval_setup` | Set up or add the owner's passkey here; returns a link. `--bot` names the Friends bot this data home answers for. |
| `yobiru approval request PEER/AGENT TEXT [--file F] [--expires 1h] [--uses 1] [--any-requester]` | `yobiru_approval_request` | Ask the owner to approve one instruction; returns a link. |
| `yobiru approval for ID` | `yobiru_approval_for` | On a receiver: a link for the owner to approve a request that arrived unsigned. |
| `yobiru approval for MSG_ID [--bot @BOT]` | | A Friends bot's agent (its native binding home): a link for the owner to approve one message the bot received; `give_link_to` and `reply_text` say where the link goes. |
| `yobiru approval wait REF [--timeout S]`, `get REF` | `yobiru_approval_wait` | Collect the answer: `signed` (with `approval_id`), `key_added`, `declined` or still waiting. `get MSG_ID` shows a Friends message's verdict, checked again each time. |
| `yobiru approval list` | | Links still waiting for the owner, and the Friends messages with a verdict. |
| `yobiru approval keys` | `yobiru_approval_keys` | Owner keys this computer trusts, with the shareable key text. |
| `yobiru approval remove FINGERPRINT` | | Ask the owner to sign the removal of a key. |
| `yobiru approval verify ID` | | Check an approval here as a receiver would. |
| `yobiru request send PEER TEXT --approval ID` | `yobiru_send` with `approval` | Send a tracked request carrying the approval. |

On the receiver, `yobiru show ID` reports its own verdict as `received.owner_approval`; it counts only with
`verified: true` and `authorization_status: active`, and `inbox --json` shows the same as `owner_approved`. The Claude
Code bridge lists verified approvals in the session's startup prompt. A sender's claim, the envelope's own
`owner_approval` field or `verified: false` is never an approval.

The protocol (intent format, WebAuthn checks, the receiver's ledger) is in [SPEC §10](https://github.com/chekusu/yobiru/blob/main/SPEC.md); the gateway's drop
endpoints are in [PUBLIC_BOTS.md](PUBLIC_BOTS.md#approval-drops).

On Friends, the approval is the receiving agent's own record: `approval get MSG_ID` counts only with `verified: true`,
which means this computer checked the signature against the owner key pinned here, for this bot, this message, its
exact text and files, and this sender. The gateway's `from.same_owner` only decides where the link goes; it never
approves anything. Approvals for other people's bots, and an approval sent along with a message, are not supported.
