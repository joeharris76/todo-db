# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- New-project bootstrap: a discovered repo-local `.todo-db/config.json`
  (found by walking up from the current directory like git discovery, or via
  `TODO_DB_CONFIG`) supplies the project identity and database target, with
  precedence explicit flags > `TODO_DB_*` environment variables > discovered
  config. A new `init-project` command runs `init` and scaffolds the repo:
  it writes the committed config file, a `.todo-db/.gitignore` that ignores
  the databases but keeps `config.json` tracked, and (with `--wrapper
  [PATH]`) an executable wrapper script that prefers an installed `todo-db`
  on PATH, falls back to a sibling `../todo-db` checkout, and relies on the
  config file instead of hardcoded identity flags. `--db` records a local
  path (default `.todo-db/standalone.sqlite`) or `libsql://` URL in the
  config; existing scaffolding is never overwritten without `--force`.
- Hosted `verify --run` gate: against a hosted (libsql/https) database,
  `verify --run` refuses to execute the DB-stored command (exit 2) unless
  `TODO_DB_ALLOW_HOSTED_VERIFY_RUN=1` is set, because commands in a shared
  database are written by other actors and executing them locally is a
  lateral code-execution channel. Local databases are unchanged.
- `scripts/turso_acceptance.sh`: a live hosted acceptance script that
  provisions a throwaway Turso database with the `turso` CLI, exercises
  init/create/claim/done/complete, the finding draft→sync→show flow, audit
  verify, and export against the real backend, asserts schema v4, and always
  destroys the database on exit (`--keep` to retain it). Tokens stay in the
  environment and are never echoed.

- Findings domain ported from the BenchBox tracker (schema v4, packaged
  migration `004_findings.sql`): credential-free draft capture under
  `~/.todo-db/finding-drafts/<project-id>/` (`TODO_DB_FINDING_DRAFTS_DIR`
  override), a `finding` CLI group
  (`create`/`list`/`show`/`candidates`/`dismiss`/`triage`/`link`/`promote`/
  `sync`) with `sync` as the sole credentialed landing step, disposition
  transitions with reason-required terminal states, atomic finding→item
  promotion, findings tables in the lossless export/restore envelope, audit
  hash-chain coverage of every finding mutation, and a findings banner on
  `ready` plus finding counts in `stats`. Draft parsing requires the new
  `findings` extra (`pyyaml`).
- MIT `LICENSE` file (the metadata already claimed MIT; the text now ships).
- `CHANGELOG.md` and a GitHub Actions CI workflow running lint and the full
  test suite with all extras.

### Fixed

- The migration runner now splits SQL on real statement boundaries (quotes,
  comments, and `BEGIN`/`CASE`...`END` bodies) instead of every bare `;`, so
  trigger-containing migrations and string literals with semicolons apply
  correctly on both backends.

### Changed

- **Breaking:** the implicit default project identity
  (`todo-db-standalone`/`todo-db`) is removed. `init` with no identity from
  flags, environment, or a discovered config is now a hard error, so a
  database can no longer silently bind to the placeholder identity that made
  the mismatch guard useless. Commands other than `init` may still run
  without supplying an identity: they proceed under the identity already
  bound in the database, and the binding check enforces only when the caller
  asserts one. Anyone relying on the old implicit default must now pass
  `--project-id`/`--repository`, set
  `TODO_DB_PROJECT_ID`/`TODO_DB_REPOSITORY`, or adopt `.todo-db/config.json`
  (BenchBox's compat shim always passes explicit identity flags and is
  unaffected).
- `TOOL_VERSION` is now derived from package metadata instead of a duplicated
  constant, so `pyproject.toml` is the single version source.
- The `dev` dependency group now includes `pyyaml` and `libsql`, so a fresh
  `uv sync && uv run pytest` passes without a manual `--all-extras` sync.

## [0.1.0] - 2026-07-21

### Added

- Standalone baseline extracted from the BenchBox in-repo tracker: SQLite and
  Turso/libSQL backends, packaged checksum-verified migrations (schema v3),
  SHA-256 audit chain with Ed25519-signed export manifests, project-identity
  binding, full item lifecycle CLI (`init`, `create`, `claim`, `start`,
  `done`, `defer`, `promote`, `dismiss`, `complete`, `lint`, `check-scope`,
  `verify`, `sweep-stale`, `export`, `restore`, `restore-legacy`,
  `audit verify`, and query commands), legacy YAML import bridge behind the
  `legacy` extra, and operational acceptance gates (two-process claim
  contention, packaging smoke tests, hosted outage fail-closed, credential
  redaction).
