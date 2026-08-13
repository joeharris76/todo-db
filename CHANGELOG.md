# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `todo-db complete` now runs every configured verification rung before the
  item can become done. Any failing command, including pytest's exit 5 for an
  empty selection, leaves the item active. Commands are graded only by exit
  status; `expected` remains human acceptance text rather than an implicit
  output-substring assertion. Hosted databases retain the
  `TODO_DB_ALLOW_HOSTED_VERIFY_RUN=1` trust boundary. Maintainers can use
  `--override-verification REASON` when a rung cannot run; the actor, reason,
  and overridden sequence numbers are recorded in the hash-chained completion
  event. Items without a verification ladder keep their existing completion
  behavior.

## [0.3.1] - 2026-08-10

### Fixed

- `todo-db release` is holder-only: a non-holder cannot release another
  actor's active or stale claim, while the holder can release and an
  unclaimed item remains an idempotent no-op. Non-holder attempts retain the
  CLI's generic error / exit-2 contract.

## [0.3.0] - 2026-07-25

### Added

- `todo-db update <id>`: a safe, fully audited amendment verb for
  create-time-only fields. `--title`/`--description`/`--priority`/
  `--worktree` edit item metadata with `create`-identical validation;
  `--add-work` extends the breakdown (new units start `pending`);
  `--edit-work WID:SUMMARY` rewrites a summary only while the unit is
  pending (a done unit's evidence attaches to its summary, so it is
  immutable); `--add-verify`/`--drop-verify SEQ` amend verification steps.
  `--reason` is required for any edit to a done/dropped item and always for
  `--drop-verify`. Item id, state, created timestamps, and project identity
  stay immutable, and `update` never transitions state. Each call commits
  atomically with one hash-chained `update` event carrying exact from/to
  diffs, and verification amendments log the full command text (they are
  security-relevant history because `verify --run` executes stored
  commands). Calls with no change flags or equal-value edits exit 2 instead
  of logging empty diffs.

- Auth-failure classification for the hosted backend: connect/sync failures
  that are auth-shaped (HTTP 401/403, `unauthorized`/`forbidden`, token/JWT
  complaints in the underlying libsql error, matched conservatively against
  the already-redacted message) raise the new `HostedAuthError` whose message
  names the concrete remediation (refresh `TODO_DB_AUTH_TOKEN` /
  `TODO_DB_RO_AUTH_TOKEN` via `turso db tokens create`, or `turso auth
  login`). The CLI maps `HostedAuthError` to a new exit code 4; exit 2 stays
  the generic fix-the-cause error, and the exit-code contract is documented
  in `--help` and the README. URL/token redaction guarantees are unchanged.
- `todo-db doctor`: a read-only preflight that checks config discovery,
  identity resolution (with source tier; FAIL only when unresolvable and the
  database is unbound), the database target (local file/parent and schema
  version with a `behind -- run init to migrate` warning; hosted URL scheme
  plus a read-only `SELECT` probe against the primary with auth failures
  classified), turso CLI availability and `turso auth whoami` for hosted
  targets (WARN means automatic token re-mint is unavailable), and
  finding-drafts dir writability. Exit 0 healthy (warnings allowed), 4 on
  any auth-classified failure, 2 on other failures; `--json` emits
  structured check records with an exit hint; `--rw` opts into a hosted
  replica open+sync probe (off by default so doctor stays side-effect-free).
- Wrapper auto-remediation: the `init-project --wrapper` script now retries
  once on exit 4 against a `libsql://` target after minting a fresh token
  with the turso CLI (name resolved from `turso db list`, token exported and
  never echoed). When remediation is impossible it prints a delimited
  `TODO-DB AUTH ALERT` block to stderr — tracker writes are blocked, the two
  remediation commands, do not continue batch work — and exits 4. The
  wrapper stays shellcheck-clean and keeps the existing tool-resolution and
  `TODO_DB_CONFIG` behavior.

## [0.2.0] - 2026-07-25

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
  environment and are never echoed. Passed against a real Turso database on
  2026-07-25 — the first live end-to-end validation of the hosted path.
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
