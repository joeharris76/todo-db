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

- credential resolution in `src/todo_db/backends.py`, including
  `resolve_credential`, the provider, and `auth_remediation`;
- the `TODO_DB_AUTH_CONTRACT` handshake or the credential/connection path in
  `src/todo_db/cli.py`;
- the per-tool credential scoping or connection-per-call path in
  `src/todo_db/mcp/` — specifically `server.py` (database_config +
  HostedAuthError), `identity.py` (clientInfo + TODO_DB_ACTOR), `target.py`
  (TODO_DB_PATH/URL/CONFIG), `dbpool.py` (CredentialMode + E_AUTH_REJECTED
  retry);
- `docs/adr/0004-hosted-credential-lifecycle.md`,
  `docs/adr/0005-hosted-credential-provider.md`,
  `docs/adr/0006-mcp-sole-agent-interface.md`, or
  `docs/operations/hosted-credentials.md`;
- the environment allowlist the MCP server passes to the tracker.

Releases that touch none of these are not subject to the downstream consumer
gate. When in doubt, run both; they cost minutes.

## Gate 1: hosted authentication acceptance

Run the harness with the release candidate installed:

```sh
TODO_DB_ACCEPTANCE_URL=libsql://<host> scripts/hosted_auth_acceptance.sh --require
```

`--require` is not optional here. Without it an unconfigured machine exits 77
and the gate would pass without testing anything, which is the failure mode this
page is about.

## Gate 2: real downstream consumer

Upgrade one real downstream consumer to the release candidate and run one real
floor-CLI command against its database, in a session that was not specially
prepared for the test.

"One real command" means a surviving `todo-db` floor verb that opens the
database and does real work — `todo-db audit verify`, `todo-db export --output
<path>`, or `todo-db doctor` — not `--help` and not `--version`. The `agent`
CLI group and the per-verb planning commands (`todo list` / `ready` / `show`,
etc.) were removed in 0.6.0; those consumers now drive the tracker through the
MCP server (`todo-db-mcp`), which is not a release gate — the gate exercises the
floor CLI a bootstrap or CI step actually runs.

Run the gate against a **local-SQLite** consumer. It must not depend on the
hosted (Turso/libSQL) path, which stays experimental and uncertified
(ADR 0006 Consequences; ADR 0003 §2.9). A hosted run may be recorded as
additional evidence but never as the only evidence.

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
- the consumer, the floor-CLI command run for gate 2, whether the consumer's
  database was local SQLite, and whether it succeeded;
- the date and the operator.

Record no credential value, no hosted URL, and no consumer secret.

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

Gate 1's full run and gate 2 stay local operator steps. CI validates the
harness's syntax and its skip contract only. Giving CI a hosted read-write
credential to automate gate 1 would contradict ADR 0004 and would defeat the
harness, whose entire premise is running without an injected credential.
