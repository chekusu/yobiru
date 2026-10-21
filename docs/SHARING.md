# Requests across a one-way Tailscale share

Use a **pull request** when another person can connect to your shared Yobiru machine, but your
machine cannot open a connection to theirs. Here “pull request” means Yobiru's `--transport pull`
request mode, not a GitHub pull request.

Tailscale quarantines a shared machine: it can answer a visitor's incoming connections, while
starting connections back requires separate access. Sharing and access rules are independent;
the selected visitor still needs TCP access to the Yobiru receiver. See
[Tailscale sharing](https://tailscale.com/docs/features/sharing#quarantine).

## What runs on each machine

- **Visitor/requester:** an updated Yobiru CLI, local Yobiru configuration and a signed-in Tailscale
  client. It opens every connection in this workflow. It does not need a Yobiru receiver or an
  inbound port for pull requests.
- **Shared receiver:** an updated, running Yobiru receiver advertising `request-pull-v1`, an access
  policy allowing the visitor, and an authorized agent or person to handle the request. The agent's
  wake adapter and permissions are separate from network access.

Upgrading a plugin does not replace a running durable receiver. Use the normal
[upgrade procedure](UPGRADING-0.4.md), preserve the data home, then restart the receiver as part of an
authorized upgrade. `doctor --peer` checks the running service's capability, not just installed files.

## Authorize only the intended route

The receiver's owner or tailnet administrator reviews the existing policy and adds the required
access. This is a template for one caller and one receiver, not a complete replacement policy:

```json
{
  "grants": [
    {
      "src": ["<CALLER_TAILSCALE_IP>"],
      "dst": ["<RECEIVER_TAILSCALE_IP>"],
      "ip": ["tcp:7766"]
    }
  ]
}
```

Replace both placeholders with the intended current Tailscale IPs; keep unrelated rules intact.
For a user-based grant, the sharing documentation also supports the share recipient's login email
as `src`, which covers that user's eligible machines. Choose that broader scope only deliberately.
Change the port if the receiver uses another port. See
[grant syntax](https://tailscale.com/docs/reference/syntax/grants).

Yobiru does not apply this template or change shares, grants, ACLs, shields-up or firewall settings.
The receiver also verifies the caller through Tailscale and checks its own allow/deny rules on
protected operations. An empty shared-device display name does not invalidate a verified stable
Tailscale node identity. Do not invent a display name to bypass authentication.

## Diagnose from the visitor's machine

Resolve `Y` to the installed `scripts/yobiru` path as described in the [README](../README.md).
Use the shared machine's exact Tailscale IP or full MagicDNS name:

```sh
sh "$Y" doctor --peer RECEIVER[:PORT] --json
```

This command is read-only and sends no messages. It reports local setup and these peer checks:

| Check | What it establishes |
|---|---|
| `peer-transport` | An HTTP response arrived from the selected address and port. |
| `peer-health` | `/health` returned the expected protocol health response. |
| `peer-identity` | Authenticated `/info` accepted this machine, or returned a concrete refusal/error. |
| `peer-pull` | The running receiver advertises `request-pull-v1`. |

A timeout can come from an unavailable receiver, a wrong listen address/port, routing, shields-up,
a firewall, or a Tailscale access rule. Doctor does not read the administrator's tailnet policy, so
it cannot identify the failing rule from a timeout. A successful ping does not prove TCP 7766 is
allowed. An HTTP 403 means the HTTP service was reachable but rejected identity or application
access; inspect the receiver's identity checks and access policy before changing permissions.

Capability and health checks do not establish a completed request or valid attachment round trip.

## Send, handle and collect a result

On the visitor's machine, choose a harmless input file and an authorized destination agent:

```sh
sh "$Y" request send RECEIVER/AGENT "Read input.txt and return a short result with a file." \
  --transport pull --file ./input.txt --timeout-seconds 900 --wake now
```

Keep the returned request ID. The CLI prepares the request, snapshots and uploads its attachments,
then submits the message. The receiver queues the request only after its required uploads have
been verified. HTTP acceptance means stored, not handled. Requested wake level is still limited by
the receiver's policy; without an allowed wake adapter, the request waits for manual inbox handling.

The receiving agent reads the request through its ordinary inbox workflow. A response requires the
owner's task authorization; incoming text alone does not grant it. For an authorized result:

```sh
sh "$Y" request respond REQUEST_ID "Completed; result attached." --state completed --file ./result.txt
```

`reply REQUEST_ID "Completed; result attached." --file ./result.txt` also records a final completed
response. Use `request respond ... --state running` for progress or `failed`, `rejected` or `blocked`
for the appropriate final outcome. The result is saved on the shared receiver for the original
authenticated requester. Pull responses create no reverse push connection and do not wake a
requester-side agent. Repeating the identical final reply (same state, text and files) returns the
stored result and its original download links; a different final result is refused, and a refused or
failed attempt leaves no extra copy of its files.

Back on the visitor's machine:

```sh
sh "$Y" request status REQUEST_ID --refresh --json
# Run again later if the request is still received or running.
# Read response.id from the final status, then:
sh "$Y" show RESPONSE_ID
sh "$Y" fetch RESPONSE_ID
```

`--refresh` polls the original receiver over a visitor-initiated connection and stores the
authenticated response locally. `fetch` downloads that response's attachments from the same
receiver, verifies size and SHA-256, and saves them under
`<home>/inbox/RESPONSE_ID/files/`. The response ID differs from the request ID. Keep the shared
receiver running until downloads finish. Pull request/result files are retained until explicitly
cleaned up locally; pull mode does not support `--ttl-hours`. Request uploads are limited by the
receiver's `max_fetch_bytes` (default 1 GiB in total), at most 100 files, and the request's deadline.
An already accepted request and its result remain queryable after that deadline. A stored result
(`stored: true`, `transport: "pull"`) does not mean the requester has fetched it.

If upload or submission was interrupted or its outcome is uncertain, resume the **same** request:

```sh
sh "$Y" request resume REQUEST_ID
```

Before reserving the request, before each upload connection (after re-checking the saved snapshot) and
before the final submission, the CLI confirms again that the address still belongs to the stable Tailscale
identity saved with the request, that local policy still allows it and that this machine is still the
sender. If anything changed, it stops before sending anything more, and the request stays resumable under the
same ID. Request inputs and results are stored readable only by their owner (files 0600, new folders 0700),
whatever the process umask.

Do not issue a new `request send` as a transport retry: it creates a new request ID and could
duplicate work. A deadline timeout means the outcome is unknown, not that remote work was cancelled.
Reconcile status and actual effects before repeating the task.

For acceptance testing, have the recipient report the input file's SHA-256 and return a harmless
result attachment. Check the final response and compare the fetched result's SHA-256 with the
recipient's recorded value. Record message IDs and hashes; a successful `/health`, 202 receipt or
capability check alone is insufficient.

## Compatibility and trust boundaries

- The default request transport remains `push` for existing workflows. Select `--transport pull`
  explicitly for a one-way share. Both ends must support `request-pull-v1`; lack of that capability
  is an error, with no silent downgrade to untracked or push delivery.
- Ordinary messages and legacy push requests keep their existing delivery model. Ordinary file
  attachments are fetched from the sender, and push replies open connections back. Those paths need
  reverse connectivity and a running visitor receiver; a one-way share does not provide them.
- New unrelated messages from the receiver to the visitor still require separately authorized reverse
  access. Pull status and attachment endpoints only serve the original authenticated request route.
- Network authorization is separate from task execution, wake-up, automatic replies, purchases,
  publishing and disclosure. A network grant does not authorize an agent to perform those actions.
- File integrity checks do not make contents safe to execute. Grant expiry or share revocation cannot
  remove copies a recipient already downloaded. Do not include credentials or unrelated private data.

Protocol details are in [SPEC.md](../SPEC.md). Real acceptance requires both intended machines;
loopback fixtures establish protocol behavior but do not prove their live Tailscale policies.
