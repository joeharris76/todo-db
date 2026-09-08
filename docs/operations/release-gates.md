# Release gates

Artifact correctness is not evidence of usability.

v0.4.2 was tagged, released, and pushed into a downstream consumer with every
gate green: six merged PRs, Python 3.10 and 3.13 CI, the Pi adapter CI, 219
tests, Ruff, scope checks, isolated wheel and sdist consumer smoke tests, and
SHA-256 verification of the downloaded release assets. Every one of those was
true and none of them executed the changed authentication path. The first time a
person used the release it failed. This page exists so that cannot recur
silently.

## Trigger

Both gates below are mandatory for a release whose diff touches any of:

- the publication protocol in `src/todo_db/git_backend.py`, including tip
  resolution, conflict classification, retries, and reconciliation;
- the state schema, validation, or claim rules in `src/todo_db/store.py`;
- the agent surface in `src/todo_db/service.py` or `src/todo_db/mcp/`;
- the floor CLI in `src/todo_db/cli.py` or migration in
  `src/todo_db/migrate.py`.

Releases that touch none of these are not subject to the downstream consumer
gate. When in doubt, run both; they cost minutes.

## Gate 1: scratch state-branch lifecycle

With the release candidate installed, run a full lifecycle against a scratch
bare remote — bootstrap, create, take, renew, finish, plus a conflicting
write and an offline read:

```sh
git init --bare /tmp/gate-state.git
todo-db bootstrap --state-remote /tmp/gate-state.git
todo-db validate --state-remote /tmp/gate-state.git
```

then drive one `take` → `finish` round through the MCP server. A gate that
passes without touching the publication path the release changed is the
v0.4.2 failure mode; `--help` output is not evidence.

## Gate 2: real downstream consumer

Upgrade one real downstream consumer to the release candidate and run one real
floor-CLI command against its state branch, in a session that was not specially
prepared for the test.

"One real command" means a surviving `todo-db` floor verb that does real work
— `todo-db validate`, `todo-db list`, or `todo-db recover --limit 5` — not
`--help` and not `--version`. Agent verbs live only on the MCP server
(`todo-db-mcp`), which is not a release gate — the gate exercises the floor
CLI a bootstrap or CI step actually runs.

## Ordering

Both gates run **before** the tag.

A release is not tagged, published, or announced to a consumer until both have
passed. A failed gate blocks the tag; it is not recorded as a follow-up item and
worked around. v0.4.2 dispatched its downstream upgrade after publication, which
is why the defect reached the consumer before it reached anyone who could stop
the release.

## Evidence

Record in the release PR body:

- the command line used for gate 1 and its final line;
- the consumer, the floor-CLI command run for gate 2, the state branch it
  addressed, and whether it succeeded;
- the date and the operator.

Record no consumer secret and no private remote URL beyond what the release
already names.

## What does not satisfy these gates

State this plainly when reviewing a release PR. None of the following is
evidence that the release is usable:

- green CI on every matrix entry;
- a passing unit and integration suite, at any count;
- isolated wheel, sdist, or package smoke tests;
- checksum verification of downloaded release assets;
- a clean `git status` and no open PRs.

All five held for v0.4.2.

## Automation boundary

Gate 1's full run and gate 2 stay local operator steps. CI runs a scratch
state-branch smoke (bootstrap/validate/list against a disposable bare
remote) plus `uv sync --locked`, which fails the build when the committed
lockfile drifts from the manifest. CI never touches a real state branch:
routine automation must not write to authoritative task state.
