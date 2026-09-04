# todo-db

Project-isolated, database-backed TODO tracking for coding agents.

`todo-db` keeps a project's work items in a SQLite database (or a hosted
Turso/libSQL one) behind a hash-chained audit trail, and exposes them to coding
agents through an MCP server. Claims are coordinated, so several agents can
work the same tracker without racing. The database is bound to one project
identity and refuses to open under another.

- **Agents** drive the tracker through the MCP server, `todo-db-mcp`.
- **People and CI** use the `todo-db` CLI for bootstrap, audit, export, and
  recovery. It deliberately has no planning verbs (see
  [ADR 0006](docs/adr/0006-mcp-sole-agent-interface.md)).

## Requirements

- Python 3.10 or newer.
- [uv](https://docs.astral.sh/uv/) (or `pipx`/`pip`) to install.
- **git** — the scope and verification gates fingerprint the working tree, so
  the tracked project must be a git repository.

## Install

Every release ships a wheel on
[GitHub Releases](https://github.com/joeharris76/todo-db/releases) with SHA-256
checksums:

```sh
VERSION=0.6.1
gh release download "v$VERSION" --repo joeharris76/todo-db \
  --pattern "todo_db-$VERSION-py3-none-any.whl"
uv tool install "./todo_db-$VERSION-py3-none-any.whl[mcp]"
# pipx accepts the same wheel, with the same extra.
```

From source:

```sh
git clone https://github.com/joeharris76/todo-db.git
cd todo-db && uv sync --extra mcp
```

Optional extras, all independent:

| Extra | Adds | Needed for |
| --- | --- | --- |
| `mcp` | `mcp` SDK | the agent interface (`todo-db-mcp`) |
| `hosted` | `libsql` | Turso/libSQL backends |
| `audit` | `cryptography` | signed export manifests |
| `findings` | `pyyaml` | `finding sync` |
| `legacy` | `pyyaml` | `import-yaml` |

## Quickstart

Adopt the tracker in a project. `init-project` creates the database and writes
the committed scaffold in one step:

```sh
cd your-project
todo-db init-project \
  --project-id your-project \
  --repository https://github.com/you/your-project
todo-db doctor
```

That writes three files:

- `.todo-db/config.json` — the project identity, and optionally the database
  target. **Commit this**; it is the point of the scaffold.
- `.todo-db/.gitignore` — ignores the local database while keeping
  `config.json` tracked.
- `.mcp.json` — registers the server so an agent can reach the tracker. An
  existing file is merged into, never replaced.

That registration is what Claude Code and Cursor read. Codex, Zed, Windsurf,
and Continue keep MCP config elsewhere; snippets for each are in
[`docs/operations/mcp-clients.md`](docs/operations/mcp-clients.md).

Your agent now has the tracker. It creates work with `create_item`, then runs
the loop below. This repository also ships a `todo-db` skill (mirrored into
`.claude/`, `.codex/`, and `.gemini/`) that teaches the workflow.

## Concepts

| Term | Meaning |
| --- | --- |
| **Item** | One unit of tracked work, with an id, scope, and lifecycle. |
| **Work unit** | A step inside an item. `progress` closes one at a time with evidence. |
| **Scope rules** | The paths an item is allowed to touch. Checked on `progress` and `finish`. |
| **Claim** | An exclusive, leased hold on an item. One active claim per principal. |
| **Claim token** | The secret proving you hold the claim; required by `progress` and `finish`. |
| **Verification ladder** | Commands stored with an item. A **human** runs them via `verify-run`; agents never execute them. |
| **Attestation** | A git workspace fingerprint bound by `verify-run`. `finish` rejects a stale one. |
| **Finding** | An observation captured during work, triaged separately and optionally promoted to an item. |

## The agent loop

```
next  ──▶  take  ──▶  context  ──▶  progress ×N  ──▶  finish
                                        │
                                        └──▶  release   (hand the claim back)
```

The loop tools (`next`, `take`, `context`, `progress`) return a
machine-readable `next_action` naming the next tool and its arguments; other
tools return their result alone. Every tool returns one of:

```json
{"ok": true,  "data": {...}}
{"ok": false, "code": "E_...", "error": "...", "recovery": [...], "kind": "gate|error"}
```

`kind: "gate"` is an expected result to act on (a scope violation, a stale
claim, nothing ready). `kind: "error"` is an environment or protocol failure.
Responses are capped at 16 KiB; list tools page rather than truncate silently.

**Profiles.** `--profile agent` (the default) exposes the workflow tools, the
read-only queries, and planning (`create_item`, `update_item`,
`add_dependency`). `--profile full` adds findings, `block`/`unblock`/`drop`,
`init_project`, and `config_get`.

**Not tools, by design.** Verification execution (`verify-run`) and scope
rebaseline (`rebaseline`) have no MCP tool at any profile. A human runs them
from the CLI. `verify-run` executes commands stored in the database; on a
shared hosted tracker those commands are written by other actors, so running
one is a code-execution channel across a trust boundary.

## Floor CLI

| Area | Commands |
| --- | --- |
| Bootstrap | `init`, `init-project`, `migrate`, `doctor` |
| CI / release | `audit verify`, `export`, `restore`, `restore-legacy`, `import-yaml` |
| Human recovery | `complete`, `verify-run`, `rebaseline`, `sweep-stale`, `config`, `finding sync` |

Planning and lifecycle mutation are MCP tools, not CLI commands.

Exit codes are a contract, also printed by `todo-db --help`:

| Code | Meaning |
| --- | --- |
| 0 | success (`doctor`: every check passed; warnings allowed) |
| 1 | findings reported (for example a failing `verify-run` ladder) |
| 2 | generic error, or a hosted auth failure before the v2 contract is negotiated |
| 4 | hosted authentication failure under `TODO_DB_AUTH_CONTRACT=v2` |

`todo-db doctor` is a read-only preflight; run it before batch work. `--rw`
adds a hosted read-write probe, and `--json` emits structured checks.

## Configuration

Every invocation discovers `.todo-db/config.json` by walking up from the
working directory, like git. Per field, the first source that resolves wins:

1. explicit flags (`--db`, `--project-id`, `--repository`)
2. environment variables
3. the discovered `.todo-db/config.json`
4. for the database only, `./.todo-db/standalone.sqlite`

There is no default identity: `init` without one from some source is a hard
error, so a database can never silently bind to a placeholder project.

| Variable | Purpose |
| --- | --- |
| `TODO_DB_PATH` / `TODO_DB_URL` | Database target (local path / hosted URL). |
| `TODO_DB_CONFIG` | Path to a config file; overrides discovery. |
| `TODO_DB_PROJECT_ID` / `TODO_DB_REPOSITORY` | Project identity. |
| `TODO_DB_ACTOR` | Audit principal. |
| `TODO_DB_AUTH_TOKEN` | Hosted read-write credential. |
| `TODO_DB_RO_AUTH_TOKEN` | Hosted read-only credential, preferred for reads. |
| `TODO_DB_CREDENTIAL_COMMAND` | Command that supplies a credential on demand. |
| `TODO_DB_AUTH_CONTRACT` | Set to `v2` to opt into exit 4 on hosted auth failure. |
| `TODO_DB_AUDIT_OPEN_POLICY` | `full` forces O(N) chain verification on open. |
| `TODO_DB_ALLOW_HOSTED_VERIFY_RUN` | Set to `1` to permit `verify-run` against a hosted tracker. |
| `TODO_DB_VERIFY_ENV_PASSTHROUGH` | Extra variable names to pass to verification subprocesses. |
| `TODO_DB_FINDING_DRAFTS_DIR` | Override the finding-drafts directory. |

## Hosted backends

The hosted target is Turso Cloud, one database per project. Provision the
database and its credentials outside `todo-db`; the CLI never creates a
database from a first-use connection.

```sh
uv sync --extra hosted --extra audit
turso db create example-project
export TODO_DB_AUTH_TOKEN="$(turso db tokens create example-project --expiration 90d)"
todo-db --db libsql://example-project.aws-us-east-1.turso.io \
  init-project --project-id example-project \
  --repository https://example.test/example-project
```

Only `https://` and `libsql://` are accepted. Cleartext `http://` and `ws://`
are refused. `wss://` is refused too: the driver treats it as a local path, so
accepting it would open a file named after the URL. Read-only commands prefer
`TODO_DB_RO_AUTH_TOKEN` and fall back to `TODO_DB_AUTH_TOKEN` only when the
read-only variable is absent — so least privilege requires actually minting a
token with `--read-only`. `CredentialMode.READ_ONLY` selects a credential; it
does not constrain one.

Rather than exporting a token per shell, point `TODO_DB_CREDENTIAL_COMMAND` at
your secret store:

```sh
export TODO_DB_CREDENTIAL_COMMAND="security find-generic-password -w -s todo-db-rw"
```

Provisioning, rotation, the provider contract, and compromise response are in
[`docs/operations/hosted-credentials.md`](docs/operations/hosted-credentials.md).
Agent mutations against hosted Turso remain **experimental**: real
commit-outcome fault behaviour is unmeasured (ADR 0003 §2.9).

## Data safety

- Every lifecycle mutation and its audit event commit atomically.
- The audit chain is SHA-256 (`sha256-chain-v2`). Open performs an O(1)
  head check; `complete` and `export` verify the full chain.
- Migrations are packaged and checksum-verified. They are additive, but an
  older binary must not write to a database a newer one has upgraded — take a
  backup before upgrading a writable tracker.
- `export` writes a lossless JSON envelope; `restore` verifies identity, schema
  version, and audit chain before replacing state.
- Signed manifests are available via `todo_db.sign_export()` and
  `todo_db.verify_signed_export()`. Keep the signing key outside the database.

## Documentation

- [Decision records](docs/adr/README.md) — why the system is shaped this way.
- [MCP client registration](docs/operations/mcp-clients.md)
- [Hosted credentials](docs/operations/hosted-credentials.md)
- [Release gates](docs/operations/release-gates.md)
- [Skill deployment](docs/operations/skill-deployment.md)
- [Contributing](CONTRIBUTING.md) · [Security policy](SECURITY.md) ·
  [Changelog](CHANGELOG.md)

## License

MIT. See [LICENSE](LICENSE).
