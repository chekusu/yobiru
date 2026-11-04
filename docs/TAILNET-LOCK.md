# Tailnet Lock

[Tailnet Lock](https://tailscale.com/kb/1226/tailnet-lock) makes your own devices, not only the Tailscale
coordination server, decide which devices may join your tailnet. A new device must be signed by one of the
*signing devices* you chose; until then every other device ignores it. A stolen Tailscale login or a compromised
coordination server can then no longer add a device on its own.

Yobiru messages travel over your tailnet, so the lock decides which machines can reach your receivers. Yobiru
helps you see the lock's state and gives you the commands; it never changes the lock.

## Principles

- **Signing keys stay on your signing devices**, held by `tailscaled`. Yobiru and the official gateway
  (bots.yobiru.com) never store them, never sign for you, and never see disablement secrets.
- **No message can trigger a signature.** Nothing received over tailmsg or a gateway runs `tailscale lock init`,
  `sign`, `add`, `remove` or `disable`. You run those yourself, on a signing device.
- **Agent machines should not be signing devices.** An agent that follows injected instructions could sign an
  attacker's device. `doctor` warns when a signing device also wakes agents.

## Commands (all read-only)

```sh
yobiru doctor            # includes the tailnet-lock checks below
yobiru lock status       # lock on/off, this device's keys, signing keys, peers hidden for lack of a signature
yobiru lock guide        # the steps and exact commands for your situation
yobiru lock guide --signer tlpub:... --signer tlpub:...   # prints the exact `tailscale lock init` command
```

`doctor` reports:

| check | meaning |
|---|---|
| `tailnet-lock` info | the lock is off; `lock guide` explains how to turn it on |
| `tailnet-lock` error | the lock is on and **this device is not signed**: to other devices it looks offline. The fix names the `tailscale lock sign nodekey:...` command to run on a signing device |
| `tailnet-lock` warn | only one signing key: losing that device stops you adding devices |
| `lock-unsigned-peers` | peers hidden because they are not signed. They are not offline. Sign them only if you recognize them |
| `lock-signer` | this machine is a signing device and also wakes agents |

## Turning the lock on

1. Choose 2 or 3 of your own devices as signers, such as a laptop and a desktop. Do not use machines where an agent
   runs commands. On each, `tailscale lock status` shows its `tlpub:` key.
2. On one signing device: `tailscale lock init --gen-disablements 3 --confirm tlpub:A tlpub:B tlpub:C`.
3. Store the 3 printed disablement secrets offline, for example in a password manager. They are the only way to
   turn the lock off if every signing device is lost.
4. Sign later devices with `tailscale lock sign nodekey:...` on a signing device. A new device's `doctor` prints
   its node key.

## Assistants that rejoin often

Assistant machines rebuilt from images lose `/var/lib/tailscale` and join as a new device each time. Prefer keeping
that directory on a persistent volume: the device keeps its identity, tags and signature. When that is not
possible, create an auth key in the admin console that is tagged (for example `tag:assistant`), ephemeral and
pre-approved, then pre-sign it on a signing device:

```sh
tailscale lock sign tskey-auth-...
```

Keep the signed key in your own secret store. Never send it to a gateway or commit it.

## Recommended policy for assistant machines

```jsonc
{
  "tagOwners": {"tag:assistant": ["autogroup:admin"]},
  "acls": [
    {"action": "accept", "src": ["autogroup:member"], "dst": ["*:*"]},
    {"action": "accept", "src": ["tag:assistant"], "dst": ["autogroup:member:7766", "tag:assistant:7766"]}
  ]
}
```

Assistants then reach only Yobiru receivers, not SSH or other services. Tagged devices do not expire by default;
re-enable key expiry for them in the admin console. With tags in place, a receiver can use
`policy set --mode allowlist --allow tag:assistant` (review, then `--apply`) instead of trusting every tailnet
peer.

## Planned

- Approving a new device from the signing device: `lock approve` with a short verification code.
- Join requests relayed through the official gateway, carrying public keys only, for devices that cannot reach the
  tailnet until signed. The gateway can pass a request on but cannot approve it.
