# Tailnet Lock with Yobiru

Yobiru runs on your own tailnet. By default the Tailscale coordination server decides which machines join it:
anyone who takes over the account that owns the tailnet, or the coordination server itself, could add a machine,
and that machine would reach every Yobiru receiver the tailnet policy allows. [Tailnet
Lock](https://tailscale.com/kb/1226/tailnet-lock) moves that decision to devices you own: a machine joins only
when one of your **signing devices** has signed its node key with a key that never leaves the device.

This document is the design for supporting owners who turn it on, and what ships now.

## Rules

- **Signing keys stay on the owner's signing devices**, inside `tailscaled`. Yobiru, the official gateway
  (bots.yobiru.com) and any receiver never hold them and never sign for you. The gateway may at most relay
  public data (node keys and `tlpub:` keys are public).
- **Assistants and agent machines are never signing devices.** An agent acts on what it reads; a message could
  talk it into running `tailscale lock sign` for an attacker's machine. `yobiru doctor` warns when a signing
  device also runs an agent that tailnet messages wake.
- **Nothing that arrives over tailmsg or the gateway triggers `lock init`, `add`, `sign`, `remove` or `disable`.**
  Those run only when the owner types them on their own device. Yobiru prints the commands, as it already does
  for `tailscale up`.
- **Disablement secrets are the owner's alone.** Yobiru tells you to store them offline and never asks for them.

## What ships now (phase 1, read-only)

| Command | What it does |
|---|---|
| `yobiru doctor` | Reads `tailscale lock status --json`. Reports lock off (info), this machine unsigned (error, with the node key to sign), fewer than two signing keys, a signing device that also runs a woken agent, and unsigned machines that are hidden from this one (they look offline, but they are filtered). |
| `yobiru lock status [--json]` | The lock state and this machine's **public** keys: the `tlpub:` key to name it as a signing device, and the `nodekey:` to sign it. Signatures and the lock head are not printed. |
| `yobiru lock setup --signer tlpub:... [--include-self]` | Prints the steps and the exact `tailscale lock init --gen-disablements N --confirm ...` (or `tailscale lock add ...` when the lock is already on). Warns with fewer than two signers. Runs nothing. |
| `yobiru lock acl [--assistant-tag T] [--server-tag T] [--port P]` | Prints a recommended tailnet policy: your own devices keep full access, assistant machines reach only the receiver port, servers answer assistants only on it. You paste it into the admin console after review. |

### Turning the lock on

1. Choose two or three devices only people use (a laptop, a desktop, a server you administer).
2. On each, `yobiru lock status` and copy its `tlpub:` key.
3. On one of them: `yobiru lock setup --include-self --signer tlpub:... --signer tlpub:...`, review, then run the
   printed `tailscale lock init` yourself. The machines in the tailnet at that moment are signed.
4. Store the disablement secrets that `init` prints offline.
5. `yobiru doctor` on each machine. An unsigned machine is an error with its node key.

### Adding a machine later

On the new machine, `yobiru lock status` shows its node key. Check that it is the machine you expect, then on a
signing device: `tailscale lock sign nodekey:...`.

### Machines that are rebuilt often

A machine that loses `/var/lib/tailscale` joins as a new node every time (and gets a `-1`, `-2` suffix while the
old node lingers). Prefer keeping that directory on persistent storage: the node keeps its key, name, tags and
signature. Where that is impossible, create an auth key in the admin console that is **tagged** (for example
`tag:assistant`), **ephemeral** and **pre-approved**, sign it on a signing device (`tailscale lock sign <auth key>`)
and keep the result in your own secret store. Anyone holding a pre-signed key can join, so prefer single-use keys
with a short expiry. Auth keys minted at boot through an OAuth client cannot be pre-signed; choose one or the
other.

### Receivers under a tagged policy

Trust tags instead of machine names, so a rebuilt assistant keeps its access:
`yobiru policy set --mode allowlist --allow tag:assistant` (dry run; add `--apply`).

## Later phases

- **Phase 2:** `yobiru lock approve`: paste the request a new machine prints (node key, `tlpub:` key, a short
  check code); Yobiru verifies the code, asks for confirmation and runs `tailscale lock sign` locally. Also
  `yobiru lock presign` to walk through the pre-signed auth key above, and collecting signing devices' `tlpub:`
  keys over tailmsg for `lock setup` instead of copying them by hand.
- **Phase 3:** join requests relayed by the official gateway. A machine that is locked out cannot reach the
  tailnet, so it posts its public keys to the gateway under the owner's Yobiru account; the owner's signing
  device is notified, shows the machine name and check code, and signs locally after confirmation. The gateway
  only carries the request: compromising it lets an attacker ask, never approve.
- **Protocol:** stable agent identities, so replies and pending messages follow an assistant to its new node
  instead of the old node name.
