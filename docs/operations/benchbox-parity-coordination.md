# BenchBox Parity Coordination for 0.6.0

## Context

`scripts/parity_conformance.py` enforces CLI parity between BenchBox's compatibility adapter (`_project/scripts/todo_db_standalone_compat.py`) and standalone `todo-db`.

In 0.6.0, the following CLI commands are removed from `todo-db` in favor of MCP tool calls:

- `agent` (and `agent instructions`)
- `create`, `update`, `list`, `show`, `ready`, `stats`, `deps`
- `start`, `done`, `defer`, `promote`, `dismiss`, `block`, `unblock`
- `release`, `claim`, `check-scope`, `verify`, `lint`, `drop`
- `refresh-wrapper`
- `finding` (all subcommands except `sync`)

The surviving floor CLI verbs are:
- Bootstrap: `init`, `init-project`, `migrate`, `doctor`
- Forensic/CI: `audit` (verify), `export`, `restore`, `restore-legacy`, `import-yaml`
- Human recovery / credentials: `finding sync`, `config` (get, set), `sweep-stale`, `complete`
- Gated recovery: `verify-run` (attests only), `rebaseline`
- Discoverability: `mcp` floor alias for `todo-db-mcp` is **proposed** (`docs/design/mcp-interface-migration.md`) but **not implemented** in 0.6.0; use the `todo-db-mcp` entry point or `python -m todo_db.mcp`.

## Parity Allowlist Expansion

To prevent parity failures before BenchBox cuts its corresponding adapter update, `scripts/parity_allowlist.json` has been expanded ahead of the 0.6.0 deletions with:

```json
"help: BenchBox command '(agent|create|update|list|show|ready|stats|deps|start|done|defer|promote|dismiss|block|unblock|release|claim|check-scope|verify|lint|drop)' not in standalone"
```

and the new floor CLI verbs (`verify-run`, `rebaseline`) are recorded in the freeze and help allowlists (`mcp` stays out until the alias lands).

The DDL tables, exit codes (0, 1, 2, 4), and epilog keyword requirements remain 100% synchronized and enforced without exception.
