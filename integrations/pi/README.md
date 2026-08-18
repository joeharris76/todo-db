# `@todo-db/pi-adapter`

Pi extension for the claim-coordinated `todo-db` agent workflow.

## Safety contract

- Registers one `todo_db` model tool only after Pi marks the project trusted and a valid `.todo-db/config.json` is discovered.
- Executes the project wrapper with `shell: false`, a bounded output buffer, and an environment allowlist.
- Keeps tracker state in `todo-db`; Pi session entries are never a second source of truth.
- Never executes stored verification commands from the model tool. Model `finish` requires a passing verification ladder attested to the current Git workspace.
- Serializes mutations and does not retry an unknown mutation outcome automatically.
- Performs no heartbeat or background write. Status refreshes are read-only.

Trusting a project permits its configured wrapper to execute. Review the checkout and `.todo-db/config.json` before granting Pi project trust.

## Installation

From this repository:

```sh
cd integrations/pi
npm install
npm run build
npm pack
pi install ./todo-db-pi-adapter-0.1.1.tgz
```

The project must have an initialized tracker:

```sh
todo-db init-project \
  --project-id <project-id> \
  --repository <repository> \
  --wrapper
```

## Tool actions

The single `todo_db` tool exposes:

- `next`
- `take`
- `context`
- `progress`
- `finish`
- `release`

`progress`, `finish`, and `release` require the current `claim_token`. Pi supplies its real session ID during `take`; the stable principal defaults to `pi:<user>@<host>` and can be configured with `TODO_DB_PI_PRINCIPAL`.

Context is paged with `section`, `cursor`, and `limit`. Follow each section's `completeness.next_cursor` until `complete` is true when more context is required.

## Human verification and rebaseline

When model finish reports stale or missing verification, review and run the human CLI command:

```sh
todo agent finish <item-id> \
  --claim-token <token> \
  --run-verifications
```

The CLI prints every stored command before executing the ladder once. Verification subprocesses receive a small environment allowlist. Add an exceptional variable explicitly with `TODO_DB_VERIFY_ENV_PASSTHROUGH`; this reduces ambient-secret exposure but is not a filesystem sandbox.

A diverged baseline requires a clean worktree and explicit audited human action:

```sh
todo agent rebaseline <item-id> \
  --claim-token <token> \
  --reason '<reason>'
```

Neither verification execution nor rebaseline is available through the model tool.

## Hosted status

Agent mutations against direct Turso/libSQL primaries are experimental. `scripts/turso_acceptance.sh` can prove a real two-connection one-winner claim race, but commit-outcome fault behavior remains uncertified. Exit 77 means the live acceptance test did not run and is not passing evidence.

## Development

```sh
npm test
npm run typecheck
npm pack --dry-run
```
