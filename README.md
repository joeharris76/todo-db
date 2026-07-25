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
checksum-verified. The audit chain uses SHA-256 and is checked on open and
export. Every lifecycle mutation and its audit event commit atomically.

The default standalone path is `.todo-db/standalone.sqlite`. This is
deliberate: an existing `.todo-db/todo.sqlite` from another tracker schema is
detected and rejected rather than silently combined with this database.

The CLI also provides `show`, `list`, `ready`, `stats`, `start`, `release`,
`defer`, `promote`, `dismiss`, `block`, `unblock`, `drop`, `lint`,
`check-scope`, `verify`, `sweep-stale`, `config`, and `finding` commands.
`todo` is a compatibility alias for `todo-db`.

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

Signed export manifests are available through `todo_db.sign_export()` and
`todo_db.verify_signed_export()`. Keep the signing key outside the database;
the signed manifest contains the public key and export digest, while
verification can be pinned to an independently trusted public key.
