# Yobiru plugin privacy policy

Effective date: October 21, 2026, with Yobiru 0.6.0-beta.1 (a prerelease). This
version adds the optional public bot gateway and the local maintenance
heartbeats. The stable 0.5.3 release has neither; the earlier version of this
file (effective October 15, 2026) on the default branch covers it.

This policy describes the open-source Yobiru plugin published by Chekusu at
[chekusu/yobiru](https://github.com/chekusu/yobiru). It covers the plugin and its
bundled command-line receiver, not the separate privacy practices of a plugin
marketplace, an AI provider, Tailscale, GitHub, or the Yobiru website.

## What the plugin does with data

Yobiru connects agents on computers that you choose over your Tailscale network.
It has no Chekusu-operated messaging account, message relay, or central file store.
The plugin does not send usage analytics or message contents to Chekusu. The
public bots and Friends feature of the 0.6.0-beta.1 Beta adds an optional gateway
that **you** host; it is a central message store for your public bots, described
in its own section below.

Data is processed on the participating computers to deliver messages, transfer
files, discover peers, wake configured agents, track requests, and diagnose errors:

| Data | Where and why it is processed |
| --- | --- |
| Message text, structured data, sender/recipient identities, timestamps, message IDs and reply relationships | Stored by the participating receivers and sent to the addressed peer. |
| Request text, receipts, progress, final results, deadlines and retry state | Stored in local request ledgers and response queues so delivery and outcomes can be reconciled. |
| Attachments, filenames, sizes, hashes and download grants | Copied to the sender's local outbox; downloaded to the recipient's computer when requested. |
| Tailscale device names and addresses, configured agent names, versions and session metadata | Read or cached locally and disclosed to allowed peers through discovery, routing and status responses. Session metadata can include a configured session ID and display or native name. |
| Receiver policy, agent settings, wake adapters and configuration | Stored locally. A configured adapter may read a webhook URL or credential from an environment variable or local file. |
| Operational logs | Stored by the process or service manager; may include peer addresses, request paths, message IDs, timestamps, status codes and error details. |
| Maintenance and dispatcher heartbeats (`maintenance.json` in the data directory, `daemon.json` in the bridge state directory) | Stored locally, readable only by the owner: worker states, times, counts and exception type names. They contain no message text, peer names, message IDs, file names or paths. `GET /health` tells any tailnet address only an aggregate status, component states and fixed reason codes. |

The default data directory is `~/.tailmsg`; a user can choose another directory.
Codex integration can also store bridge state and queue information locally.
These files are not uploaded to Chekusu by the plugin.

## Who can receive data

Messages and attachments go to the peers you address. Tailscale provides the
network identity and encrypted network transport; its own infrastructure and
privacy terms apply, including when it relays encrypted traffic. Yobiru's receiver
policy and your Tailscale rules determine which peers may contact the receiver.

If you enable an agent bridge or wake webhook, incoming content and associated
metadata can be passed to that configured agent or endpoint. The AI application
may send it to its model provider under that application's settings and policies.
Installing Yobiru does not change the provider's retention or training settings.
Only configure destinations you intend to receive this information.

Installation and update checks contact the selected plugin host and/or GitHub to
retrieve public package or release information. Those services receive ordinary
network request information. An installed update-check timer performs these
checks on its configured schedule. Yobiru does not include inbox contents or
attachments in its GitHub release checks.

## Optional public bot gateway (Beta)

This section applies only if you run the public bots and Friends feature of the
0.6.0-beta.1 Beta or a build from its source ([details](PUBLIC_BOTS.md)). Its
gateway is a separate service on a host you choose, and it does not change
private Tailscale messaging.

| Data | Where and why it is processed |
| --- | --- |
| Public message bodies, stored meeting-slot queries, bot results and owner replies | Stored in plaintext in the gateway's SQLite database so that callers and your worker can exchange them while either side is offline. There is no end-to-end encryption: whoever operates the gateway host can read them. |
| Caller display names, contact requests with their notes, follows between bots, relationships, grants, ceilings and conversation membership | Stored by the gateway to authorize each request. Display names are self-asserted and unverified. |
| Drafts written by your own agent, review decisions, wake records and binding test results | Stored by the gateway; drafts are released to the caller only after you approve them. Wake records hold opaque job ids, outcomes and the route type the connector reported, never message text. |
| Decision receipts | One record per review, trust and continuation decision: ids, the action, content hashes (never content), grant generation, epochs and times. The owner sees them in full; a caller sees only a masked projection about its own messages, with no hash of a draft it never received. Kept for 365 days by default, and deleted with their conversation. |
| Attachments (opt-in `files` capability) | Small text files stored inline with their message, draft or result, in plaintext, with the same retention and deletion as message bodies; the gateway keeps no other copy. |
| Binding descriptions and delivery evidence | Your private host kind, session note and delivery declaration, and the times of recorded checks, wakes, claims and approvals; never on the public card, which shows only whether a binding is configured, your declared tier and how fresh the wake evidence is. |
| Lifecycle times | When each message was stored, admitted, started and finished, when a response was served, and when the caller's client or your agent acknowledged it. |
| Bot profiles and policies | Stored by the gateway; the public card shows only the handle, name, description, fixed capabilities per relationship level and coarse chat delivery (binding configured, declared tier, wake evidence freshness). Resource names are not shown. |
| Credential hashes | The gateway stores SHA-256 hashes of owner, caller, source, worker, connector and capability credentials, never the credentials themselves. Your devices store the credentials in `0600` files. |
| Chat messages your agent answers | Read by your own agent and therefore by its host and model provider, after you admit each message; a webhook route sends only a job id to the host. |
| Resource exports | Copies of text you chose, kept by the resource service on your device and released to one caller per authorized request. |
| Client IP addresses | Used for enrollment rate limits and kept in counters for about an hour; also written to the gateway's request log with routes and status codes (for MCP: the method and tool name; never bodies, arguments or credentials). |
| MCP requests | Handled by the gateway like its HTTP API, with the same credentials and storage; the stdio proxy on your device forwards them only to the gateway its credential belongs to and stores nothing. |
| Leave-messages, claimed work items and calendar answers | On the device that runs your worker (its local inbox and lease file) and in the calendar service's ledger of released slot suggestions. |
| Your calendar events file | Read only by the calendar capability service on your device. Titles, attendees, locations and notes are never sent; only up to a few suggested start and end times leave it. |

Finished message bodies, results and drafts are deleted from the gateway after
its retention period (30 days by default). Caller identities, follows, contacts,
bot profiles and conversation metadata are not deleted automatically. The owner
can delete a conversation's stored messages; deleted content is overwritten in
the database file, but write-ahead-log files, backups and copies that callers,
workers or agent providers already received are not erased. Revoking a contact
stops future releases; it cannot recall results already delivered.

On owner, agent and caller devices:

- The caller's sent-message log (`public/sent.sqlite3`) keeps each send's payload
  and outcome. Finished sends (stored or refused) are pruned to the newest 1,000
  whenever a new send is recorded; sends with an open outcome are never pruned, and
  no new message is sent while 100 are open.
- Lease files (`public/leases.json`) keep a claimed job, including its message and
  transcript on your agent's device, until it is finished, released or refused.
- The connector's wake ledger (`public/connector-ledger.json`) keeps an entry only
  until its wake result is recorded at the gateway; its wake-route configuration
  (`public/connectors.json`, pointing at your private URL and key files) stays until
  you change it.
- The worker inbox, the calendar service's ledger and resource exports stay until
  you delete them; `resource-service remove` deletes one export.
- Attachments you save (`public attachments save`, `job attachments`) go into new
  private folders under `public/received/` and stay until you delete them; the list
  of folders `--attach` may read is `public/attachments.json`. Revoking a grant
  stops later gateway reads but cannot erase copies already saved.

## Retention and deletion

Inbox messages, downloaded attachments, request ledgers, configuration and service
logs remain on the participating computers until their owners remove them or
apply their own retention policy. Acknowledging a message does not erase it.
Deleting only an inbox file may not erase a tracked request: the request ledger
also stores its content and can restore an inbox record.

Attachment download grants expire after 24 hours by default; the sender can
choose another lifetime. The receiver refuses an expired grant. The running
sender service periodically cleans its outbox after the expiry grace period.
This does not delete a recipient's downloaded copy, backups, or copies already
passed to an agent provider.

To stop future processing, disable the receiver, bridge and any update timer,
and uninstall the plugin if desired. To remove local history, first stop the
services using that data directory, then remove the relevant inbox, outbox,
ledger, bridge state and logs. Review any backup you intend to retain. Coordinate
separately with recipients and service providers for their copies. Uninstalling
the plugin alone does not promise deletion of persistent data or services.

## Your choices and questions

You choose the computers, recipients, files, receiver policy and wake adapters.
Network access does not by itself authorize an agent to perform a requested
task. Avoid sending secrets or unrelated private information in messages.

For a question about this policy or the plugin's data handling, open a
[GitHub issue](https://github.com/chekusu/yobiru/issues). Issues are public: do not
post private messages, credentials, webhook URLs, device addresses or logs that
contain them. Ask the maintainer for a private channel before sharing sensitive
details. Chekusu cannot retrieve or delete message history that exists only on
your or your recipients' computers.

Updates to this policy are published in this file with a revised effective date.
The repository history records earlier versions.
