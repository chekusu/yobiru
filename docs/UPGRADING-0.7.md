# Upgrading to Yobiru 0.7.18

This is the current release guide. The older UPGRADING-0.6.md page remains as a historical migration record and is not a 0.7.18 procedure.

## Verify the release

Download yobiru-0.7.18-plugin.zip, yobiru-0.7.18-marketplace.zip and SHA256SUMS from the same official GitHub release. Verify both archive hashes before extraction. Check that the archive contains the Muse connector, the yobiru-muse skill and the Claude visible-session bridge. A source checkout or a repository marketplace install follows main and is not proof that the release archive was used.

Keep a new extraction directory and a separate data home for a trial. Do not overwrite an existing plugin, durable runtime, receiver, session ledger or queue merely because the package version changed.

## Upgrade a client

First inspect the installed plugin, durable tailmsg runtime and running receiver independently. Preserve a newer development runtime unless the owner explicitly authorizes a downgrade. Install the verified plugin archive, then run its read-only paths, doctor, update status and Muse help checks. Configure the Muse official Tailscale connector through Muse's own account flow first; use Yobiru Friends only when that connector cannot be installed or used. Muse is outbound-only and must not run a receiver.

0.7.17 and 0.7.18 change only the gateway; clients need nothing new. The 0.7.16 additions stay opt-in. A connector wakes its agent when two bots become friends only after `connector config --wake-on-follows`, and Friends HTTPS goes through an HTTP CONNECT proxy only when `YOBIRU_HTTPS_PROXY_ENV` names the variable that holds the proxy URL. Without either, an upgraded client behaves as 0.7.15 did. An installed Claude bridge keeps its saved settings; `claude-bridge install --model ID` is optional.

For Claude Code, read the bundled Claude bridge guide. The installer previews before writing, preserves an existing custom wake adapter and local dispatcher configuration, and never starts a service during installation. Native Claude login and visible Remote Control acceptance require a separate harmless real-account check.

Restart a receiver only through its existing service mechanism and only after preserving its data home, access policy, agent/session bindings, inbox/outbox and request ledger. After restart, check receiver health, authenticated info, runtime version and each agent binding separately. A successful plugin install is not a receiver or end-to-end acceptance result.

## Upgrade the gateway

Make a private database and gateway configuration backup before stopping services. Inspect the current schema and run the gateway's migration command from the verified release. Yobiru 0.7.14 and 0.7.15 use schema 8, and 0.7.16 to 0.7.18 keep it: the gateway adds its follow-wake columns and (0.7.17) its `hosted_wakes` table by itself when it starts, so `migrate` should be a no-op when the database is current. Hosted wake calls run in short-lived processes the maintenance process starts, at most four at once. The first hosted wake registration creates `wake.key` in the gateway home: back it up with `gateway.json` and the other secrets, since database backups do not include it. Update the gateway before connectors opt in to follow wakes; a newer connector treats an older gateway as having no follows. Keep the backup even when migration reports no changes.

Deploy the exact stable tag through the reviewed release workflow. Verify the source tag, clean gateway checkout, gateway and mail service status, local and public health, discovery metadata and the backup quick check. Record the running source tag separately from the published GitHub release. Do not treat a CI mirror or queued deployment as production completion.

## Verify and rollback

Check the installed plugin, durable runtime, receiver and connector separately. For Muse, a local check or package installation does not prove a live Muse request round trip or directory approval. For Claude, fake-CLI tests do not prove native account startup or Remote Control visibility. Reconcile an uncertain request by its original ID; never create a replacement task because a status query timed out.

If the deployment fails, use the release workflow's rollback and the paired database/config backup. Do not restore an old queue or launch ledger over newer ownership state. Preserve logs and the exact release checksum. Keep the previous stable tag available until the new gateway and clients have passed their independent checks.
