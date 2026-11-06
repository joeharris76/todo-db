# ADR 0013: Rename the Product to tractable and Release It as 1.0.0

- **Status**: Accepted
- **Date**: 2026-11-06

## Context

The product has outgrown its name. `todo-db` describes a storage engine, but
the product coordinates work between agents: claims, dependencies, deferrals,
prepared work and batch delivery, published through a Git state branch. Since
0.7.0 there is no database at all.

The package has never been published to PyPI, so it has no index users yet.
Renaming before the first index release avoids a second rename later. Its
known consumers are a small, owned set: BenchBox (which vendors the wheel),
benchbox-site, Oxbow, bossmode, skill-sync, this repository, and the global
tool and MCP registrations on the owner's machine.

Nothing that reads tracker state checks the product name. Readers validate
the shape and schema of `index.json` and `items/*.json`; reconciliation
finds an operation by its exact `Todo-Op-Id` trailer; no code checks a commit
author or the `todo(op):` subject prefix. A rename therefore needs no state
migration.

## Decision

Rename the product to `tractable` and release the rename alone as 1.0.0.

### Name mapping

| Surface | Before | After |
| --- | --- | --- |
| Distribution and import package | `todo-db`, `todo_db` | `tractable`, `tractable` |
| Commands | `todo-db`, `todo-db-mcp` | `tractable`, `tractable-mcp` |
| MCP server name | `todo-db` | `tractable` |
| Batch capability key | `todo_db_batch` | `tractable_batch` |
| Instructions resource and prompt | `todo://instructions`, `todo/workflow` | `tractable://instructions`, `tractable/workflow` |
| Project config | `.todo-db/config.json` | `.tractable/config.json` |
| Environment variables | `TODO_DB_*` | `TRACTABLE_*` |
| Local cache | `~/.cache/todo-db-state`, `refs/todo-db/*`, `todo-db-id` | `~/.cache/tractable-state`, `refs/tractable/*`, `tractable-id` |
| Author of new state commits | `todo-db <todo-db@localhost>` | `tractable <tractable@localhost>` |
| Review finding drafts | `~/.todo-db/finding-drafts/` | `~/.tractable/finding-drafts/` |
| Repository and PyPI project | `joeharris76/todo-db`, none | `joeharris76/tractable`, `tractable` |

### Kept on purpose

- **State format.** Schema 4, `index.json` and `items/*.json` are unchanged.
  Any 1.0.0 client reads state written by todo-db 0.14.0 or 0.15.0, and the
  two can write the same branch.
- **Commit trailers** `Todo-Op-Id`, `Todo-Actor` and `Todo-Session`.
  Reconciliation matches `Todo-Op-Id` exactly, so renaming it would orphan
  the recovery of operations written before the rename.
- **The `todo(op): summary` commit subject.** It is descriptive, not a
  product name, and history keeps it either way.
- **The default state branch `todo-state`.** It names the data. Every
  existing project config names its branch explicitly.
- **The skill name `todo`** and every tool name (`take`, `finish`, and so
  on). The skill names a workflow, not the product.
- **History.** Changelog entries, earlier ADRs and consumers' recorded
  evidence keep the old name. They describe what was true then.

### No compatibility layer

The renamed code reads only the new names: no fallback to `.todo-db/`,
`TODO_DB_*` or the old cache root, and no `todo-db` command aliases. The
transition lives outside the code instead:

1. **Old and new tools coexist.** `todo-db` and `tractable` are different
   distributions, so installing `tractable` leaves the `todo-db` tool in
   place. Projects not yet switched keep using it.
2. **Each project switches in one change.** A project's runtime, config
   directory, MCP registration, skill mirrors and scripts move together, so a
   project is never half old and half new, and an agent never reads skill
   text naming one product while connected to the other.
3. **Mismatches fail closed.** A skill naming `tractable_batch` against a
   `todo-db` server sees no capability and falls back to serial mode; a
   project whose registration names a missing server stops at the skill's
   preflight. Neither path writes wrong state.
4. **The old tool is removed last**, once no registration names `todo-db`
   and no `todo-db-mcp` process is running.

### Version 1.0.0 and compatibility policy

The rename ships as 1.0.0 with no other behaviour change, so any regression
in it is attributable to the rename. 1.0.0 contains every capability the
`todo` skill previously gated on a version (0.8.1 takeover, 0.11.0 bound
store, the 0.14.0 schema), so version floors collapse to "tractable 1.0.0 or
later".

From 1.0.0, the public interface under Semantic Versioning is: command names
and options, MCP tool names, inputs and capability keys, the project config
file and its location, environment variables, and the set of state schemas a
client reads. A change that removes or renames any of them is a major
version. In particular, **a state-schema cutover that older clients cannot
read is a major version**, so that every 1.x client works with any tracker a
1.x client wrote. Additive tools, fields and capability flags are minor
versions.

## Consequences

- Every consumer needs one switching change, made in dependency order after
  the 1.0.0 release and the `todo` skill update. BenchBox's change touches
  its vendored wheel, lockfile, contract check, scripts, tests and CI.
- After the rename, the first offline start in a project has an empty cache
  under the new root until one online read fills it.
- Running MCP servers keep the version they started with. Every open session
  restarts after the global switch.
- GitHub redirects the old repository URLs, including release downloads, as
  long as no new repository takes the name `joeharris76/todo-db`. None may.
- The trusted publisher on PyPI is registered against the renamed
  repository, after the rename.

## Alternatives considered

- **Keep the name.** Rejected: the name misdescribes the product, and the
  first PyPI release is the cheapest point to change it.
- **Read the old config directory and variables for one release.** Rejected:
  every consumer is known and owned, so per-project switching plus a
  coexisting old tool gives the same safety without compatibility code to
  remove later.
- **Continue the 0.x numbering (0.16.0) or restart at 0.1.0.** 0.16.0 would
  keep version floors continuous; 0.1.0 would make them ambiguous. 1.0.0 was
  chosen to mark the stable public start and to anchor the compatibility
  policy above.
- **Rename the state branch default and commit trailers.** Rejected: they
  name data, renaming the trailers would break recovery of old operations,
  and neither is user-facing product branding.
