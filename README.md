# todo-db

Project-isolated, database-backed TODO tracking for local SQLite first and
optional Turso/libSQL hosted backends.

`todo-db` is the canonical command. `todo` remains a compatibility alias for
existing consumers and is not the generic project TODO router.

## Local use

```sh
uv sync
uv run todo-db --db .todo-db/standalone.sqlite init \
  --project-id example-project \
  --repository https://example.test/example-project
uv run todo-db --db .todo-db/standalone.sqlite create example-item \
  --title "Example item" --worktree todo-db --priority medium \
  --description "An example item with executable lifecycle state." \
  --work "w0:Run the tests" --verify "Tests::uv run pytest -q"
uv run todo-db --db .todo-db/standalone.sqlite claim example-item
uv run todo-db --db .todo-db/standalone.sqlite done example-item w0 \
  --evidence "uv run pytest -q"
uv run todo-db --db .todo-db/standalone.sqlite complete example-item
uv run todo-db --db .todo-db/standalone.sqlite audit verify \
  --project-id example-project \
  --repository https://example.test/example-project
uv run todo-db --db .todo-db/standalone.sqlite export \
  --project-id example-project \
  --repository https://example.test/example-project \
  --output export.json
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

The CLI also provides `show`, `update`, `list`, `ready`, `stats`, `start`,
`release`, `defer`, `promote`, `dismiss`, `block`, `unblock`, `drop`, `lint`,
`check-scope`, `verify`, `sweep-stale`, `config`, and `finding` commands.
`todo` is a compatibility alias for `todo-db`.

## Updating items

`update` amends an item after creation without touching its lifecycle:

```sh
uv run todo-db --db .todo-db/standalone.sqlite update example-item \
  --title "Corrected title" --edit-work "w0:Corrected summary" \
  --add-work "w1:Newly discovered unit" \
  --add-only-modify "tests/**" \
  --add-verify "Lint::uv run ruff check ." \
  --drop-verify 1 --reason "tests and lint are now the reviewed boundary"
```

Metadata edits (`--title`, `--description`, `--priority`, `--worktree`,
`--approach`, `--category`) are validated exactly as `create` validates them.
Empty `--approach` or `--category` values clear the stored field. The item
id, state, created timestamps, and project identity are immutable, and
`update` never transitions state — the lifecycle verbs own that.
`--edit-work` applies only while a work unit is pending; a done unit's
evidence attaches to its summary, so it is immutable. `--reason` is required
for any edit to a done/dropped item, every scope change, and any drop of a
verification, item dependency, preserve, anti-pattern, prior-art row, or
work-unit dependency. Every call commits atomically with one hash-chained
`update` audit event carrying exact from/to diffs; verification adds and
drops log the full command text, because `verify --run` executes stored
commands and amendments to them are security-relevant history. A call with
no change flags, or an edit equal to the current value, is rejected (exit 2)
rather than logged as an empty diff.

Scope rules are amended explicitly with `--add-only-modify`,
`--drop-only-modify`, `--add-do-not-modify`, and `--drop-do-not-modify`.
Every scope mutation requires `--reason`, including additions: adding an
`only_modify` rule can broaden an existing allowlist, while removing the last
one can remove the allowlist entirely. Item dependencies use `--add-needs`
and `--drop-needs`; work-unit dependencies on existing units use
`--add-work-need WID:NEEDS_WID` and `--drop-work-need`. Guardrail rows use
`--add-preserve` / `--drop-preserve`, `--add-anti-pattern` /
`--drop-anti-pattern`, and `--add-prior-art PATH::CONCEPT::reuse|extend|supersede`
/ `--drop-prior-art PATH::CONCEPT`. The event records the exact added and
dropped rows, and duplicate, missing, contradictory, or empty values abort
the whole update transaction without a partial metadata/work/verification
change.

## Adopting todo-db in a new project

`init-project` initializes the database and scaffolds the repo in one step:

```sh
uv run todo-db init-project \
  --project-id example-project \
  --repository https://example.test/example-project \
  --wrapper
```

It writes three things into the adopting repository:

- `.todo-db/config.json` — the committed source of the project identity (and
  optionally the database target as a repo-relative path or `libsql://` URL).
  Commit this file; it is the whole point of the scaffold.
- `.todo-db/.gitignore` — ignores the databases (`*.sqlite*`, `replica.db*`,
  `*.lock`) while keeping `config.json` tracked. Do not add a bare
  `.todo-db/` rule to the repository root `.gitignore`; that would hide the
  config file (init-project warns when git ignores it).
- with `--wrapper [PATH]` (default `_project/scripts/todo`), an executable
  wrapper that resolves the tool as `TODO_DB_TOOL` checkout, then `todo-db`
  on PATH, then a sibling `../todo-db` checkout, and points `TODO_DB_CONFIG`
  at the repo's config so it works from any working directory. The wrapper
  hardcodes no identity flags.

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
asserts one. `init-project` refuses to overwrite an existing config or
wrapper unless `--force` is passed.

For a hosted project, provision first, then point `init-project` at the URL
(the CLI never provisions; a first-use connection to a missing database
fails):

```sh
turso db create example-project
turso db tokens create example-project   # export as TODO_DB_AUTH_TOKEN
TODO_DB_AUTH_TOKEN=... uv run todo-db --replica .todo-db/replica.db \
  init-project --db libsql://example-project.aws-us-east-1.turso.io \
  --project-id example-project \
  --repository https://example.test/example-project \
  --wrapper
```

`scripts/turso_acceptance.sh` exercises the full hosted lifecycle against a
throwaway Turso database and destroys it afterwards; it requires an
authenticated `turso` CLI and costs real resources.

## Findings

The `finding` group tracks blind-spot findings (review classes, not single
defects) in a two-step capture/landing flow:

```sh
uv sync --extra findings
uv run todo-db finding create --title "Reviews skip generated files" \
  --finding-kind framework-gap --review-context "release review" \
  --gate class-not-instance
uv run todo-db --db .todo-db/standalone.sqlite finding sync
```

`finding create` and `finding candidates` are credential-free: they only read
and write Markdown drafts under `~/.todo-db/finding-drafts/<project-id>/`
(override with `TODO_DB_FINDING_DRAFTS_DIR` or `--drafts-dir`), never the
database. `finding sync` is the sole credentialed landing step; it validates
each draft, inserts-if-absent by filename-stem id, and fails loudly on a
same-id/different-content conflict instead of merging. Landed findings move
through `list`, `show`, `triage`, `dismiss`, `link`, and `promote` (which
atomically creates a planning item, links it, and flips the finding to
`promoted`). Every finding mutation commits atomically with a hash-chained
audit event plus an append-only `finding_events` provenance row, and the
findings tables are included in the lossless export/restore envelope. Open
findings and unsynced drafts surface as a one-line banner on `ready` and as
counts in `stats`.

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
  --replica .todo-db/replica.db \
  init --project-id example-project \
  --repository https://example.test/example-project

TODO_DB_RO_AUTH_TOKEN=... uv run todo-db \
  --db libsql://project.aws-us-east-1.turso.io \
  export --project-id example-project \
  --repository https://example.test/example-project \
  --output export.json
```

Read-write hosted connections use a per-worktree embedded replica and
`TODO_DB_AUTH_TOKEN`; read-only exports connect with
`TODO_DB_RO_AUTH_TOKEN`. Plaintext `http://` URLs are refused.

`verify --run` executes a command stored in the database. On a shared hosted
database those commands are written by other actors, so running one locally
is a lateral code-execution channel across the trust boundary between
writers. Against a hosted backend `verify --run` therefore refuses (exit 2)
unless `TODO_DB_ALLOW_HOSTED_VERIFY_RUN=1` is set; inspect the command first
with `todo-db verify <id>`, then opt in per invocation. Local databases are
unaffected.

Signed export manifests are available through `todo_db.sign_export()` and
`todo_db.verify_signed_export()`. Keep the signing key outside the database;
the signed manifest contains the public key and export digest, while
verification can be pinned to an independently trusted public key.

## Failure detection and remediation

Exit codes are a contract (also printed in `todo-db --help`):

| Code | Meaning |
| ---- | ------- |
| 0    | success (`doctor`: every check passed; warnings allowed) |
| 1    | findings reported (`check-scope` violations, `lint` findings, `verify --run` failures) |
| 2    | generic error: fix the reported cause and retry |
| 4    | hosted authentication failure |

Hosted connect/sync failures that are auth-shaped (HTTP 401/403,
`unauthorized`/`forbidden`, token/JWT complaints in the underlying libsql
error) raise `HostedAuthError` and exit 4; the message keeps the URL and
token redacted and states the remediation: refresh the token, e.g. `export
TODO_DB_AUTH_TOKEN=$(turso db tokens create <db>)`, or `turso auth login` if
the turso CLI itself is logged out. Non-auth hosted failures stay exit 2.

`todo-db doctor` is a read-only preflight (no writes, no replica sync unless
`--rw` is passed) intended before batch work. It checks config discovery,
identity resolution (with its source tier; failing only when no source
resolves and the database is unbound), the database target (local: file or
creatable parent plus schema version, warning `behind -- run init to
migrate`; hosted: URL scheme and a read-only `SELECT` probe against the
primary using `TODO_DB_RO_AUTH_TOKEN`, else `TODO_DB_AUTH_TOKEN`), turso CLI
availability and `turso auth whoami` when the target is hosted (warning
means automatic token re-mint is unavailable), and finding-drafts dir
writability. Exit 0 when healthy (warnings allowed), 4 on any
auth-classified failure, 2 otherwise. `--json` emits
`{"checks": [{name, status, detail, remediation?}], "exit": N}`.

The wrapper scaffolded by `init-project --wrapper` auto-remediates: when the
wrapped command exits 4 against a `libsql://` target and the turso CLI is
authenticated, it resolves the database name from `turso db list`, mints a
fresh token with `turso db tokens create`, exports `TODO_DB_AUTH_TOKEN`
(never echoing it), and retries the command exactly once. When remediation
is impossible (turso CLI missing or logged out, database name unresolvable,
or the retry still exits 4) it prints a delimited `TODO-DB AUTH ALERT` block
to stderr stating that tracker writes are blocked, the two remediation
commands, and that batch work must not continue until resolved, then exits
4. Batch drivers should treat exit 4 from the wrapper as a hard stop.
