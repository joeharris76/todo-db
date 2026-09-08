# todo-db

JSON/Git TODO tracking for coding agents.

`todo-db` keeps a project's work items as canonical JSON (`index.json`
plus `items/<id>.json`) published on a dedicated Git state branch, and
exposes them to coding agents through a small MCP server. Claims are
cooperative and one-winner, so several agents can work the same tracker
without racing. Git history supplies ordinary change history.

- **Agents** drive the tracker through the MCP server, `todo-db-mcp`.
- **People and CI** use the `todo-db` CLI for bootstrap, validation,
  migration, and recovery. It deliberately has no planning verbs (see
  [ADR 0007](docs/adr/0007-json-git-tracker.md)).

Version 0.7.0 replaces the SQLite/Turso runtime. The old
database-backed product is gone, not carried behind a second adapter:
see [Migration from 0.6.x](#migration-from-06x) and the
[Changelog](CHANGELOG.md).

## Requirements

- Python 3.10 or newer.
- [uv](https://docs.astral.sh/uv/) (or `pipx`/`pip`) to install.
- **git** — task state is published through Git; the `git` CLI must be
  installed and the state remote reachable.

## Install

Every release ships a wheel on
[GitHub Releases](https://github.com/joeharris76/todo-db/releases) with SHA-256
checksums:

```sh
VERSION=0.7.0
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

Optional extras:

| Extra | Adds | Needed for |
| --- | --- | --- |
| `mcp` | `mcp` SDK | the agent interface (`todo-db-mcp`) |

## Quickstart

Create the state branch and point the project at it. A human runs, from
anywhere with Git access to the remote:

```sh
todo-db bootstrap --state-remote <git-url-or-path> --write-config
todo-db validate
```

`--write-config` writes `.todo-db/config.json` (state remote + branch)
in the repo root. **Commit this**; it is the point of the scaffold.
Bootstrap refuses to overwrite an existing state branch.

That registration is what Claude Code and Cursor read via `.mcp.json`.
Codex, Zed, Windsurf, and Continue keep MCP config elsewhere; snippets
for each are in
[`docs/operations/mcp-clients.md`](docs/operations/mcp-clients.md).

Your agent now has the tracker. It creates work with `create_item`, then runs
the loop below. This repository also ships a `todo-db` skill (mirrored into
`.claude/`, `.codex/`, and `.gemini/`) that teaches the workflow.

## Concepts

| Term | Meaning |
| --- | --- |
| **Item** | One unit of tracked work: an id, title, priority, status, claim, and optional `needs` IDs in the index; description and context in its detail file. |
| **Status** | `open`, `active`, `blocked`, `done`, or `dropped`. Only `active` holds a live claim; `done`/`dropped` carry none. |
| **Claim** | A cooperative hold: worker identity, expiry, and a unique generation. Ownership plus generation checks protect renew, release, and finish from stale writers. |
| **Generation** | The token proving you hold the claim; returned by `take`, required by `renew`/`finish`/`release`. |
| **State branch** | The authoritative task store (`todo-state` by default). One commit per operation; fast-forward pushes only. |

## The agent loop

```
list_items  ──▶  take  ──▶  show_item  ──▶  renew ×N  ──▶  finish
                                            │
                                            └──▶  release   (hand the claim back)
```

`take` returns enough context to begin work, so no immediate second read
is prescribed. Every tool returns one of:

```json
{"ok": true,  "data": {...}}
{"ok": false, "code": "E_...", "error": "...", "recovery": [...], "kind": "gate|error"}
```

`kind: "gate"` is an expected result to act on (a conflict, a stale
claim, nothing ready). `kind: "error"` is an environment or protocol failure.
Responses are capped at 16 KiB at their final serialization; lists page
with cursors scoped to a state revision, and oversized fields spill to
section reads.

One server instance is one worker identity. Concurrent workers run
separate servers with different `--actor` values.

## Floor CLI

| Area | Commands |
| --- | --- |
| Bootstrap | `bootstrap [--write-config]` |
| Validation | `validate` |
| Migration | `migrate --from-export <file> [--dry-run]` |
| Recovery | `recover --op-id <id>` / `--restore-rev <sha>` / `--limit N` |
| Reads | `list`, `show` |

Planning and lifecycle mutation are MCP tools, not CLI commands.

Exit codes are a contract:

| Code | Meaning |
| --- | --- |
| 0 | success |
| 2 | generic error (unreachable remote, rejected mutation, bad input) |

## Configuration

Every invocation resolves the state branch the same way; the first source
that resolves wins:

1. explicit flags (`--state-remote`, `--state-branch`, `--cache-dir`)
2. environment variables
3. the discovered `.todo-db/config.json` (walk up from the working
   directory, like git)

| Variable | Purpose |
| --- | --- |
| `TODO_DB_STATE_REMOTE` | State remote (Git URL or path). |
| `TODO_DB_STATE_BRANCH` | State branch (default `todo-state`). |
| `TODO_DB_CACHE_DIR` | Local snapshot cache (default `~/.cache/todo-db-state`). |
| `TODO_DB_CONFIG` | Path to a config file; overrides discovery. |
| `TODO_DB_ACTOR` | Worker identity for CLI mutations and the MCP default. |

There is no default remote: resolving without one from some source is a
hard error. The state branch shares its repository's access and
visibility — never publish credentials to it and never assume a separate
branch is private.

## Data safety

- The remote state branch is authoritative. Mutations fetch the accepted
  tip, apply one logical operation, validate, commit with the tip as
  parent, and fast-forward push — reporting success only after remote
  acceptance is confirmed.
- Competing pushes re-apply the operation against the new tip. Conflicting
  edits fail instead of silently overwriting.
- Every mutation carries a unique operation ID (a `Todo-Op-Id` commit
  trailer). A lost reply reconciles by operation ID without duplicating
  the change; an undetermined outcome is reported as unknown/retryable.
- Offline reads may serve a cached revision marked stale. Offline
  mutations fail rather than succeeding locally.
- Bootstrap refuses to overwrite an existing branch. History stays
  append-only; restores land as new commits.

## Migration from 0.6.x

Deliberate breaking change: the database runtime, hosted backends, audit
chain, workflow gates (work units, scope, verification ladders, lint,
findings), and the 26-tool surface are removed. Export first with the old
release, then migrate:

```sh
# With todo-db 0.6.x still installed:
todo-db export --output legacy-export.json   # old command
# With 0.7.0, onto a freshly bootstrapped branch:
todo-db migrate --from-export legacy-export.json --dry-run
todo-db migrate --from-export legacy-export.json --backup-dir ./migration-backup
```

Migration preserves IDs, priorities, statuses, dependencies,
descriptions, and evidence; unmapped fields land in per-item `legacy`
detail plus the preserved input envelope — nothing is silently dropped.
Live claims are never carried: cut over only with no active claims, or
the command refuses with the holders listed. Rollback before adoption is
deleting the new branch and re-bootstrapping; after adoption it is an
append-only `recover --restore-rev`. Production migration is out of
scope for the tooling change itself: exercise disposable fixtures and
leave live databases untouched.

## Measurements

Token and footprint targets are recorded in
[`docs/measurements.md`](docs/measurements.md), measured with
`o200k_base` on actual serialized surfaces: about 300 tokens for five
list rows, 1,500 for one task context, 100 for a mutation
acknowledgement, with a 16 KiB hard byte ceiling on every response.

## Documentation

- [Decision records](docs/adr/README.md) — why the system is shaped this way.
- [MCP client registration](docs/operations/mcp-clients.md)
- [Release gates](docs/operations/release-gates.md)
- [Skill deployment](docs/operations/skill-deployment.md)
- [Contributing](CONTRIBUTING.md) · [Security policy](SECURITY.md) ·
  [Changelog](CHANGELOG.md)

## License

MIT. See [LICENSE](LICENSE).
