# todo-db

Project-isolated, database-backed TODO tracking for local SQLite first and
optional Turso/libSQL hosted backends.

`todo-db` is the canonical command. `todo` remains a compatibility alias for
existing consumers and is not the generic project TODO router.

From **0.6.0**, agent planning and workflow go through the MCP server
(`todo-db-mcp`). The `todo-db` CLI is a minimal floor for bootstrap, CI,
audit/export, and human recovery. See
[ADR 0006](docs/adr/0006-mcp-sole-agent-interface.md) and the
[MCP migration design](docs/design/mcp-interface-migration.md).

## Install and upgrade

Release artifacts are available from GitHub with published SHA-256 checksums:

```sh
gh release download v0.6.0 --repo joeharris76/todo-db \
  --pattern 'todo_db-0.6.0-py3-none-any.whl'
uv tool install './todo_db-0.6.0-py3-none-any.whl[mcp]'
# pipx accepts the same downloaded wheel; add the mcp extra the same way.
```

Registry publication uses the same versioned artifacts when PyPI credentials
are configured; the GitHub release remains the canonical fallback.

For local development:

```sh
uv sync --extra mcp
```

Before upgrading an existing writable tracker, take the normal database/export
backup. Opening it with a current `todo-db` applies checksum-verified schema
migrations through 007 (verification workspace attestations). The migrations
are additive; older binaries must not write a database after it has been
upgraded.

## Agent interface (MCP)

Install the optional MCP extra and run the stdio server (`todo-db-mcp`). One
server instance equals one project / worktree:

```sh
uv tool install 'todo-db[mcp]'
# or: uv sync --extra mcp
todo-db-mcp --actor "<principal>" [--repo-root <path>] [--profile agent|full]
```

- `--actor` sets the audit principal. If omitted, the server derives
  `mcp:<clientInfo.name>:<user>@<host>` from the MCP `initialize` handshake.
- `--profile agent` (default) exposes the hot-path workflow tools plus
  read-only queries; `full` adds planning, findings, and admin tools.
- Verification execution and scope rebaseline are **not** MCP tools. A human
  runs them on the floor CLI (`verify-run`, `rebaseline`) with `--actor`
  naming the claim holder.

Client registration snippets (Claude Code, Codex, Cursor, and others) live in
[`docs/operations/mcp-clients.md`](docs/operations/mcp-clients.md).

## Floor CLI

The remaining `todo-db` verbs are:

| Area | Commands |
| ---- | -------- |
| Bootstrap | `init`, `init-project`, `migrate`, `doctor` |
| CI / release | `audit verify`, `export`, `restore`, `restore-legacy`, `import-yaml` |
| Recovery / human | `complete`, `verify-run`, `rebaseline`, `sweep-stale`, `config`, `finding sync` |

Planning and lifecycle mutation (`create`, `claim`, `done`, `update`, `list`,
`ready`, `show`, `stats`, `start`, `release`, `check-scope`, `verify`, `lint`,
and the finding verbs other than `sync`) are MCP tools, not CLI commands.

### Local bootstrap

```sh
uv sync
uv run todo-db --db .todo-db/standalone.sqlite init \
  --project-id example-project \
  --repository https://example.test/example-project
uv run todo-db --db .todo-db/standalone.sqlite migrate
uv run todo-db --db .todo-db/standalone.sqlite doctor
uv run todo-db --db .todo-db/standalone.sqlite audit verify \
  --project-id example-project \
  --repository https://example.test/example-project
uv run todo-db --db .todo-db/standalone.sqlite export \
  --project-id example-project \
  --repository https://example.test/example-project \
  --output export.json
```

Create and claim work through the MCP server after bootstrap. When a human
must run stored verification commands (attest-only; does not complete the
item):

```sh
uv run todo-db --db .todo-db/standalone.sqlite \
  --actor "<claim-holder-principal>" \
  verify-run example-item --claim-token "<token>"
uv run todo-db --db .todo-db/standalone.sqlite complete example-item
```

The database binds to the supplied project identity. Reusing a database for a
different project is rejected before access. Migration SQL is packaged and
checksum-verified. The audit chain uses SHA-256 (`sha256-chain-v2`); on open,
it performs an $O(1)$ audit-head consistency check by default (with full $O(N)$
chain verification during `complete`, `export`, or when `TODO_DB_AUDIT_OPEN_POLICY=full`
is configured). Every lifecycle mutation and its audit event commit atomically.

The default standalone path is `.todo-db/standalone.sqlite`. This is
deliberate: an existing `.todo-db/todo.sqlite` from another tracker schema is
detected and rejected rather than silently combined with this database.

`todo` is a compatibility alias for `todo-db`.

## Adopting todo-db in a new project

`init-project` initializes the database and scaffolds the repo in one step:

```sh
uv run todo-db init-project \
  --project-id example-project \
  --repository https://example.test/example-project
```

It writes two things into the adopting repository:

- `.todo-db/config.json` — the committed source of the project identity (and
  optionally the database target as a repo-relative path or `libsql://` URL).
  Commit this file; it is the whole point of the scaffold.
- `.todo-db/.gitignore` — ignores local database and legacy replica artifacts
  (`*.sqlite*`, `replica.db*`, `*.lock`) while keeping `config.json` tracked. Do not add a bare
  `.todo-db/` rule to the repository root `.gitignore`; that would hide the
  config file (init-project warns when git ignores it).

If an older config still has a `wrapper` key, delete it; current releases
ignore unknown keys rather than failing.

Every `todo-db` invocation discovers `.todo-db/config.json` by walking up
from the current directory (like git discovery; `TODO_DB_CONFIG` overrides
the search). Resolution precedence, per field:

1. explicit flags (`--db`, `--project-id`, `--repository`)
2. environment (`TODO_DB_PATH`/`TODO_DB_URL`, `TODO_DB_PROJECT_ID`/`TODO_DB_REPOSITORY`)
3. the discovered `.todo-db/config.json`
4. for the database, `./.todo-db/standalone.sqlite`; for identity, nothing

There is no default identity. `init` (and `init-project`) without an
identity from one of those sources is a hard error, so a database can never
silently bind to a placeholder project. Commands other than `init` may run
without supplying any identity: they proceed under the identity already
bound in the database, and the mismatch guard enforces only when the caller
asserts one. `init-project` refuses to overwrite an existing config unless
`--force` is passed.

For a hosted project, provision first, then point `init-project` at the URL
(the CLI never provisions; a first-use connection to a missing database
fails):

```sh
turso db create example-project
turso db tokens create example-project --expiration 90d   # export as TODO_DB_AUTH_TOKEN
TODO_DB_AUTH_TOKEN=... uv run todo-db \
  init-project --db libsql://example-project.aws-us-east-1.turso.io \
  --project-id example-project \
  --repository https://example.test/example-project
```

`scripts/turso_acceptance.sh` runs an opt-in, real-primary, two-connection
one-winner claim race against a throwaway Turso database and destroys it
afterwards. Exit 77 means the test did not run and is not certification.
The local fault harness exercises reconciliation after a post-commit transport
failure, but real hosted commit-outcome fault behavior remains unmeasured, so
agent mutations on hosted Turso are experimental rather than certified.

## Findings

Finding capture and triage are MCP tools (`--profile full`). Drafts are
credential-free Markdown under
`~/.todo-db/finding-drafts/<project-id>/` (override with
`TODO_DB_FINDING_DRAFTS_DIR` or `--drafts-dir`). The floor CLI keeps only the
credentialed landing step:

```sh
uv sync --extra findings
uv run todo-db --db .todo-db/standalone.sqlite finding sync
```

`finding sync` validates each draft, inserts-if-absent by filename-stem id,
and fails loudly on a same-id/different-content conflict instead of merging.
Landed findings and their mutations commit atomically with a hash-chained
audit event plus an append-only `finding_events` provenance row, and the
findings tables are included in the lossless export/restore envelope.

## Legacy YAML bridge

YAML import is explicit so a standalone project cannot accidentally traverse a
sibling repository's tracker tree:

```sh
uv sync --extra legacy
uv run todo-db --db .todo-db/standalone.sqlite import-yaml \
  --todo-dir /path/to/project/_project/TODO \
  --done-dir /path/to/project/_project/DONE
```

Use `--dry-run` to inspect the import report first. `--replace` is refused for
local databases and dry runs; it exists only for an explicitly selected live
hosted import after an export and restore plan has been approved.

Legacy event rows are not copied byte-for-byte during YAML import because the
standalone schema requires a hash-chained event envelope. The versioned mapping
is: each imported item emits a standalone `create` event with `item_id` in the
canonical detail object; each imported deferral emits `defer`; each accepted
dependency emits `dependency`. Original item timestamps and lifecycle fields
remain row data, while new event timestamps record the import operation. Shadow
comparison normalizes this documented action/item/detail mapping and must not
discard actor or action provenance to manufacture parity.

Restore is equally explicit and verifies the project identity, schema version,
and audit chain before replacing state:

```sh
uv run todo-db --db .todo-db/standalone.sqlite restore \
  --input export.json --replace \
  --project-id example-project \
  --repository https://example.test/example-project
```

## Hosted use

The initial hosted target is Turso Cloud with one physical database per
project. Provision databases and scoped credentials outside this runtime
package; the CLI never creates a database from a first-use connection.

Install the optional adapters and provide credentials through the environment:

```sh
uv sync --extra hosted --extra audit
TODO_DB_AUTH_TOKEN=... uv run todo-db \
  --db libsql://project.aws-us-east-1.turso.io \
  init --project-id example-project \
  --repository https://example.test/example-project

TODO_DB_RO_AUTH_TOKEN=... uv run todo-db \
  --db libsql://project.aws-us-east-1.turso.io \
  export --project-id example-project \
  --repository https://example.test/example-project \
  --output export.json
```

Hosted read-write connections require `TODO_DB_AUTH_TOKEN`. Read-only commands
prefer `TODO_DB_RO_AUTH_TOKEN` and fall back to `TODO_DB_AUTH_TOKEN` only when
the RO variable is absent or empty; rejection of a present RO credential never
triggers an RW retry. Plaintext `http://` URLs are refused. Provision bounded
database-scoped credentials outside todo-db:

```sh
export TODO_DB_AUTH_TOKEN="$(turso db tokens create <db> --expiration 90d)"
export TODO_DB_RO_AUTH_TOKEN="$(turso db tokens create <db> --read-only --expiration 180d)"
```

When neither an explicit token nor a `TODO_DB_*` variable is present, todo-db
asks the command in `TODO_DB_CREDENTIAL_COMMAND` for the capability it needs, so
a credential provisioned once is reused by every later shell and MCP session
without an interactive step:

```sh
export TODO_DB_CREDENTIAL_COMMAND="security find-generic-password -w -s todo-db-rw"
```

The command is split with `shlex` and executed directly; it never runs through a
shell, and your arguments are passed through exactly as written. The requested
capability (`read-only` or `read-write`) reaches the command only as
`TODO_DB_CREDENTIAL_CAPABILITY` in its environment, so a plain retrieval command
needs no special handling and a script that wants to branch can read it. A
provider-resolved credential is reported as `requested:read-only` or
`requested:read-write`, because the provider may ignore the request and serve
both from one entry; the label records the request, never a proven property.
Exit 0 with
output supplies the token; exit 0 with no output means the credential is absent,
which is the only condition that lets read-only fall back to read-write. Any
non-zero exit, timeout, unparsable command, missing executable, or oversized
output is `E_AUTH_MISSING` and stops resolution, so a broken read-only provider
can never escalate to a read-write credential. Failures report the provider's
program name and exit status only: provider stdout is the token and provider
stderr routinely echoes it, so neither ever reaches an error, log, or doctor
field. The provider is consulted at most once per capability per process, never
for a local database, and never when a credential was supplied explicitly. With
the variable unset, behaviour is exactly what it was before it existed. A caller
that filters the environment it passes to todo-db must forward the variable.

`scripts/hosted_auth_acceptance.sh` proves the whole path end to end: it removes
any inherited `TODO_DB_AUTH_TOKEN` and `TODO_DB_RO_AUTH_TOKEN`, then asserts
that `doctor` resolves the credential from the provider and that an ordinary
read succeeds. Unconfigured it exits 77; `--require` makes an unconfigured run a
failure. Releases that touch credential resolution must pass it and a real
downstream consumer check before tagging; see
[`docs/operations/release-gates.md`](docs/operations/release-gates.md).

`CredentialMode.READ_ONLY` chooses a credential but does not make an RW token
read-only. Server-side least privilege requires a token created with
`--read-only`. ADR 0004 records the lifecycle decision and ADR 0005 records the
credential-provider contract that removes per-session token export
([all decision records](docs/adr/README.md)); see
[`docs/operations/hosted-credentials.md`](docs/operations/hosted-credentials.md)
for provisioning, routine replacement, and compromise response. ADR 0006 amends
the scoping unit for the MCP server: capability-scoped credentials are resolved
per tool call, not held as one standing read-write connection.

`verify-run` executes commands stored in the database. On a shared hosted
database those commands are written by other actors, so running one locally
is a lateral code-execution channel across the trust boundary between
writers. Against a hosted backend `verify-run` therefore refuses (exit 2)
unless `TODO_DB_ALLOW_HOSTED_VERIFY_RUN=1` is set; inspect the stored commands
first, then opt in per invocation. MCP tools never execute stored commands.
`verify-run` previews and runs each rung exactly once, re-checks scope, and
binds a deterministic Git workspace fingerprint attestation — it does not
complete the item. `--actor` must name the claim holder (typically the MCP
server principal). Verification subprocesses receive a small environment
allowlist; extra names require explicit `TODO_DB_VERIFY_ENV_PASSTHROUGH`.
Tracker data-plane credentials (`TODO_DB_AUTH_TOKEN`, `TODO_DB_RO_AUTH_TOKEN`)
and Turso control-plane credentials (`TURSO_AUTH_TOKEN`, `TURSO_API_TOKEN`) are
always rejected from passthrough, with variable names—but never values—
reported. `TODO_DB_CREDENTIAL_COMMAND` is rejected on the same terms: it holds
no secret itself, but a verification command that inherited it could run the
provider and print the token, so the protection covers anything that yields a
credential on demand, not only variables that contain one. Unrelated explicitly
named credentials remain supported. This reduces ambient-secret exposure but is
not a sandbox: an approved command still has the caller's filesystem access.

`rebaseline` is likewise a human floor verb: it requires `--actor`, a claim
token, an audited `--reason`, and a clean worktree.

Signed export manifests are available through `todo_db.sign_export()` and
`todo_db.verify_signed_export()`. Keep the signing key outside the database;
the signed manifest contains the public key and export digest, while
verification can be pinned to an independently trusted public key.

## Failure detection and remediation

Exit codes are a contract (also printed in `todo-db --help`):

| Code | Meaning |
| ---- | ------- |
| 0    | success (`doctor`: every check passed; warnings allowed) |
| 1    | findings reported (for example verification failures from `verify-run`) |
| 2    | generic error, or legacy-safe authentication failure before v2 negotiation |
| 4    | hosted authentication failure under `TODO_DB_AUTH_CONTRACT=v2` |

Missing credentials raise `HostedAuthError` with `E_AUTH_MISSING`; confidently
classified server rejection uses `E_AUTH_REJECTED`. HTTP 401 and explicit
`unauthorized`, `forbidden`, invalid-token, or expired/invalid JWT evidence are
auth-shaped. Bare 403, quota/suspension, network, TLS authority, protocol, and
other ambiguous failures stay generic exit 2. Messages and tracebacks redact
the URL and selected token.

The MCP server sets `TODO_DB_AUTH_CONTRACT=v2` in its own environment. Floor
CLI automation that needs exit 4 on hosted auth failure must set
`TODO_DB_AUTH_CONTRACT=v2` explicitly. Without that handshake, auth failures
return exit 2 so callers that have not opted into the v2 contract cannot
mistake an auth failure for a generic error and auto-mint credentials. Library
callers always receive the coded `HostedAuthError`, independent of CLI exit
negotiation.

`todo-db doctor` is a read-only preflight intended before batch work. Passing
`--rw` adds a hosted read-write connection probe. It checks config discovery,
identity resolution (with its source tier; failing only when no source
resolves and the database is unbound), the database target (local: file or
creatable parent plus schema version, warning `behind -- run init to
migrate`; hosted: URL scheme and a read-only `SELECT` probe against the
primary using `TODO_DB_RO_AUTH_TOKEN`, else `TODO_DB_AUTH_TOKEN`), and
finding-drafts dir writability. It never invokes the Turso CLI. Hosted
database checks include non-secret `source`, `capability`, and auth `code`
fields in JSON; text output names the same provenance but never the token
value. Exit 4 requires both an auth failure and the v2 contract; callers
without the contract receive exit 2. `--json` emits
`{"checks": [{name, status, detail, remediation?, source?, capability?,
code?}], "exit": N}`.

Provision a credential once and point `TODO_DB_CREDENTIAL_COMMAND` at it, or
inject `TODO_DB_AUTH_TOKEN` / `TODO_DB_RO_AUTH_TOKEN` before invoking the floor
CLI or MCP server; an authentication failure is a hard stop for batch work.
