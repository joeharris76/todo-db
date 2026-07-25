# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

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
