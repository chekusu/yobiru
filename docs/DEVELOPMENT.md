# Development and handoff

The maintained development workspace is on Sandbank Tokyo. Use the `codex` account for source work;
root is reserved for system administration. The Mac checkout is a sync and platform-verification copy.
Mac-specific validation remains necessary: Linux passing does not establish macOS behavior.

## Choose one writer

Before editing, read the server workspace instructions and the installed VAS handoff guide:

- `$HOME/workspace/AGENTS.md`
- `/opt/codex-skills/skills/vas/SKILL.md`
- `$HOME/workspace/repos/github.com/chekusu/cloud.sandbank.dev/docs/agent-handoff-workflow.md`

The main source checkout is `$HOME/workspace/repos/github.com/chekusu/yobiru`.
Create an independent worktree per task and machine, based on an inspected exact main commit, and
record `vas handoff status` before editing. Keep one active writer per task branch. For normal work
use `codex/<task>`; VAS handoff exports and resumptions use its required `agent/` or `checkpoint/`
prefix. Never reset or clean an existing dirty checkout to begin a task.

## Verify and preserve

Run the checks required by `.github/workflows/ci.yml` and the relevant behavior tests. Keep the exact
commit, commands, outcomes and any unverified host behavior in the handoff. Use a coherent local
commit as a checkpoint; an unstaged directory, a stash or a summary alone is not a transferable
checkpoint. Transfer through a verified Git ref or bundle, then resume the exact SHA in a new
worktree. Keep unrelated work, untracked research and media with a hash manifest and recoverable
archive before retiring an old checkout.

## Runtime and release boundaries

Development checkouts are not receiver data homes. Preserve each installation's data, credentials,
agents, permissions, sessions, queues and adapters; never copy credentials between machines or
replace a customized runtime as an incidental source update. Production gateway code is deployed
separately under `/srv/yobiru-gateway/src` by the release workflow.

A main-branch merge, a published tag and a completed production deploy are separate states. Keep
release tags immutable, verify the running version and database migration after deployment, and
retain the deployment's rollback backup. See [Releasing](RELEASING.md).

A GitHub checkpoint is a handoff; AGS is the server validation/release path. Inspect workflow triggers
before a push, and use only the publishing/deployment scope the owner has authorized. Record the
actual runner outcome and endpoint evidence, not just push acceptance.
