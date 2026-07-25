# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- MIT `LICENSE` file (the metadata already claimed MIT; the text now ships).
- `CHANGELOG.md` and a GitHub Actions CI workflow running lint and the full
  test suite with all extras.

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
