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
`check-scope`, `verify`, `sweep-stale`, and `config` commands. `todo` is a
compatibility alias for `todo-db`.

## Legacy YAML bridge

YAML import is explicit so a standalone project cannot accidentally traverse a
sibling repository's tracker tree:

```sh
uv sync --extra legacy
uv run todo-db --db .todo-db/standalone.sqlite import-yaml \
  --todo-dir /path/to/project/_project/TODO \
  --done-dir /path/to/project/_project/DONE
```

Use `--dry-run` to inspect the import report first. `--replace` is an explicit
destructive tracker-table replacement and should only be used after an export.

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
