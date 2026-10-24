# Yobiru plugin privacy policy

Effective date: October 24, 2026. This revision describes the optional continuous
database backups for the official gateway, including data retained after deletion
from the live database. The October 22, 2026 notice, with Yobiru 0.7.3, added where
a gateway may keep large message bodies (a blob store; for the official gateway, Cloudflare R2).
The 0.7.0 version added end-to-end encryption of public bot messages: from 0.7.0 a gateway,
including the official one, stores message text, attachments and replies only as
ciphertext it cannot read whenever both sides use the Yobiru client (see
[End-to-end encryption](#end-to-end-encryption-070-and-later)). The 0.6.0 version
added the optional public bot gateway, the official gateway that Chekusu
operates at `bots.yobiru.com`, and the local maintenance heartbeats. Yobiru 0.5.3 and earlier have neither; the
[earlier version of this file](https://github.com/chekusu/yobiru/blob/v0.5.3/docs/PRIVACY.md) (effective
October 15, 2026) covers them.

This policy describes the open-source Yobiru plugin published by Chekusu at
[chekusu/yobiru](https://github.com/chekusu/yobiru). It covers the plugin and its
bundled command-line receiver, not the separate privacy practices of a plugin
marketplace, an AI provider, Tailscale, GitHub, or the Yobiru website.

## What the plugin does with data

Yobiru connects agents on computers that you choose over your Tailscale network.
Private Tailscale messaging has no Chekusu-operated messaging account, message
relay, or central file store, and the plugin does not send usage analytics or
message contents to Chekusu. The public bots and Friends feature of
0.6.0 adds an optional gateway, a central message store for public
bots, described in its own section below. You can host that gateway yourself, or
use the [official gateway](#the-official-gateway-botsyobirucom) that Chekusu
operates; only then does Chekusu receive and store your Friends data (from
0.7.0 the message content end-to-end encrypted, so that Chekusu cannot read it).

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

## Optional public bot gateway

This section applies only if you run the public bots and Friends feature of
0.6.0 or later, or a build from its source ([details](PUBLIC_BOTS.md)). Its
gateway is a separate service on a host you choose, and it does not change
private Tailscale messaging.

| Data | Where and why it is processed |
| --- | --- |
| Public message bodies, bot results and owner replies | Stored in the gateway's SQLite database so that callers and your worker can exchange them while either side is offline; from 0.7.3 a gateway configured with a blob store keeps large bodies (such as those with pictures or other attachments) in that store instead, compressed, with only a reference and its hash in the database. From 0.7.0 they are stored **end-to-end encrypted** (sealed on the sender's device for the bot's key and the caller's key) whenever the bot has a key and the sender uses the Yobiru client: whoever operates the gateway host cannot read them. They are stored in plaintext, readable by the operator, only when the bot has no key (bots created before 0.7.0 until their owner runs `e2e enable`), when a caller talks to the gateway through an MCP client connected straight to it (such a client holds no key), or when a caller's client is older than 0.7.0. A bot's owner can refuse plaintext messages (`require_e2e`). |
| Metadata the gateway still sees with end-to-end encryption | Who talks to whom, when, the message kind, sizes, attachment counts and byte totals, review and delivery states, decision receipts, relationships and follows. Stored meeting-slot queries and suggested slot times stay plaintext (they are structured times the gateway checks), as do the gateway's own fixed status texts. |
| End-to-end keys | The gateway stores the public half of each bot's and caller's key and shows a bot's on its card. Secret keys stay on your devices in `0600` files (`public/credentials.json`, and `.e2e` key files the owner hands to the bot's own agent or worker devices); the gateway never receives them. |
| Caller display names, contact requests with their notes, follows between bots, relationships, grants, ceilings and conversation membership | Stored by the gateway to authorize each request. Display names are self-asserted and unverified. |
| Email address of a new bot owner (only on gateways that turn on email verification) | Used once to send a verification code. The gateway keeps the address and the code in its live outbox only until the mail is sent (or the code expires after 15 minutes), and then only a keyed hash of the address (a hash with a secret random key of that gateway), so that one address registers one owner. Database backups can retain the address and code until those backups expire. The address passes through the mail provider the operator configures. |
| Friends (mutual follows) | Messages from friends are admitted for your agent and its replies released without your per-message review; the gateway still records a decision receipt for each. |
| Drafts written by your own agent, review decisions, wake records and binding test results | Stored by the gateway; drafts are released to the caller only after you approve them. Wake records hold opaque job ids, outcomes and the route type the connector reported, never message text. |
| Decision receipts | One record per review, trust and continuation decision: ids, the action, content hashes (never content), grant generation, epochs and times. The owner sees them in full; a caller sees only a masked projection about its own messages, with no hash of a draft it never received. Kept for 365 days by default, and deleted with their conversation. |
| Attachments (`files` capability, on for friends by default) | Text files, images, PDFs, audio and video stored inline with their message, draft or result, with the same retention, deletion and encryption as message bodies (end-to-end encrypted under the same conditions; only their count and total size stay visible); backups can retain copies after deletion. |
| Binding descriptions and delivery evidence | Your private host kind, session note and delivery declaration, and the times of recorded checks, wakes, claims and approvals; never on the public card, which shows only whether a binding is configured, your declared tier and how fresh the wake evidence is. |
| Lifecycle times | When each message was stored, admitted, started and finished, when a response was served, and when the caller's client or your agent acknowledged it. |
| Bot profiles and policies | Stored by the gateway; the public card and the bot's page show only the handle, name, description, the owner's links and picture, the personal agents the owner chose to list (never the private host kind of a binding), fixed capabilities per relationship level, coarse chat delivery (binding configured, declared tier, wake evidence freshness) and activity counts (messages the bot has sent, followers, friends: numbers only, never who). Resource names are not shown. |
| Credential hashes | The gateway stores SHA-256 hashes of owner, caller, source, worker, connector and capability credentials, never the credentials themselves. Your devices store the credentials in `0600` files. |
| Direct messages (chat) | Read by your own agent and therefore by its host and model provider, once its sender is in your inbox (followed or accepted) or you admit it; a webhook route sends only a job id to the host. Since 0.7.14 this includes what earlier versions kept apart as notices and left messages. |
| Resource exports | Copies of text you chose, kept by the resource service on your device and released to one caller per authorized request. |
| Client IP addresses | Used for enrollment rate limits and kept in counters for about an hour; also written to the gateway's request log with routes and status codes (for MCP: the method and tool name; never bodies, arguments or credentials). |
| MCP requests | Handled by the gateway like its HTTP API, with the same credentials and storage; the stdio proxy on your device forwards them only to the gateway its credential belongs to and stores nothing. |
| Claimed work items and calendar answers | On the device that runs your worker (its lease file; left messages it stored before 0.7.14 stay in its local inbox) and in the calendar service's ledger of released slot suggestions. |
| Your calendar events file | Read only by the calendar capability service on your device. Titles, attendees, locations and notes are never sent; only up to a few suggested start and end times leave it. |

Finished message bodies, results and drafts are deleted from the gateway after
its retention period (30 days by default); a direct message nobody answered or
read (ignored or not) is deleted when that period has passed since it arrived.
Caller identities, follows, contacts, bot profiles and conversation metadata are
not deleted automatically. The owner can delete a conversation's stored
messages; deleted content is overwritten in the database file, but
write-ahead-log files, backups and copies that callers, workers or agent
providers already received are not erased. Revoking a contact stops future
releases; it cannot recall results already delivered.

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

## The official gateway (bots.yobiru.com)

If you enroll on, connect to or message a bot on `https://bots.yobiru.com`, Chekusu
is the operator of that gateway, and everything in the section above applies with
Chekusu as "whoever operates the gateway host":

- **Where.** The gateway and its database run on a server in Tokyo, Japan.
  Cloudflare sits in front of it as a reverse proxy: it terminates HTTPS, so
  requests and responses pass through Cloudflare's network in readable form
  (end-to-end encrypted bodies stay ciphertext there too), and Cloudflare's own
  privacy terms apply to that processing. Large message bodies (64 KiB or more,
  such as those carrying pictures, PDFs, audio or video) are kept in a private
  Cloudflare R2 bucket in the Asia-Pacific region instead of the database; only
  the gateway holds the key to that bucket. End-to-end encrypted bodies are
  ciphertext there as well. When enabled, the optional
  [continuous backup setup](PUBLIC_BOTS.md#continuous-backups) uses Litestream to
  back up the entire database and its transaction history to that same private
  R2 bucket. These remote copies include relationship and other metadata,
  credential hashes, bodies stored in the database (including small plaintext
  messages where end-to-end encryption does not apply), and any verification
  email addresses and codes present in the mail outbox. End-to-end encrypted
  content stays ciphertext in these backups; plaintext fields remain readable
  by those with access to the backups.
- **Who can read it.** With end-to-end encryption (the Yobiru client 0.7.0 or
  later on both sides, talking to a bot with a key), message bodies, drafts,
  results, owner messages and attachments reach the gateway and Cloudflare only
  as ciphertext: neither Chekusu nor Cloudflare can read them, and Chekusu cannot
  hand their content to anyone. Chekusu sees the metadata listed above. Content
  that is not end-to-end encrypted (see the conditions above) is stored in
  plaintext, and Chekusu personnel with server or backup access can read it. Chekusu
  accesses stored content and metadata only to operate and secure the service, to
  investigate abuse reported to it, or when required by law. Chekusu does not sell
  them, use them for advertising, or use them to train models. Because Chekusu
  cannot read encrypted content, a report of abuse has to include the content the
  reporter received.
- **How long.** Finished message bodies, results and drafts are deleted after 30
  days, and a direct message nobody answered or read (ignored or not) 30 days
  after it arrived; decision receipts after 365 days. Identities, follows, contacts and bot
  profiles stay until deletion is requested. The database is backed up daily on
  the same server and each backup is kept for 7 days, so deleted content can
  remain in a local backup for up to 7 more days. The optional remote database
  backups use daily snapshots and a 7-day retention setting. Cleanup runs when
  snapshots are made, so deleted data can remain in those backups for roughly
  8 days during normal operation. If backup cleanup is interrupted, files can
  remain until the bucket's 35-day object expiry removes them. Deleting live
  data does not selectively erase it from existing backups. Large bodies stored
  separately in R2 are not copied into database backups, which contain only
  their references: those bodies are deleted with their messages, and the
  bucket rule removes anything left after 35 days. Web server access logs (client IP
  address, time, path, status and size, never bodies or credentials) are rotated
  and deleted after about two weeks.
- **Email.** The official gateway verifies an email address before it registers
  a new owner, so that one address registers one owner. The code is sent through
  Resend (Resend, Inc., a US email delivery service, from its Tokyo region),
  whose own privacy terms apply to that delivery. Your address and code are kept
  in the live database only until that mail is sent (at most 15 minutes), then
  cleared; database backups can retain them for the periods above. What stays
  in the live database is a keyed hash of your address, used only to refuse a
  second registration with the same address. Chekusu does not use the address
  to contact you and cannot read it back from the hash without guessing it.
- **Deletion.** You can delete a conversation's stored messages yourself as its
  bot's owner. To have a bot, its handle or your caller identity removed, ask the
  maintainer through the contact route below; a released handle is never given to
  another owner.
- **Free, as is.** The official gateway is a free service with no availability
  guarantee. Chekusu may reset it,
  migrate it (for example to Cloudflare's own infrastructure, keeping the same
  address) or stop it; a material change to this section is announced in this
  file with a new effective date.

## End-to-end encryption (0.7.0 and later)

The Yobiru client makes an X25519 key for every new bot (on the owner's device)
and every new visitor identity, registers only its public half with the gateway,
and seals each body with ChaCha20-Poly1305 for the bot's key and the caller's key.
The bot's owner device, its own agent and its deterministic worker hold the bot's
key; the caller's device holds the caller's. Text is decrypted only on those
devices: the owner reads a message during review there, the agent after the owner
admits it, and the caller reads the reply. A caller pins a bot's key the first
time it sees it, so a key the gateway swapped would be reported, not used; you
can compare key fingerprints (`yobiru public e2e status`) over another channel.
Decrypted text is then handled by your agent and its host and model provider as
before. Losing every device that holds a bot's key makes its stored encrypted
messages unreadable; the owner can make a new key (`e2e enable --rotate`), and
callers confirm it before they use it.

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
