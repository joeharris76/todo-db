# Contributing to todo-db

Thank you for contributing to `todo-db`.

## Development Setup

The project uses [uv](https://docs.astral.sh/uv/) for Python environment and dependency management.

```sh
# Clone and enter repository
git clone https://github.com/joeharris76/todo-db.git
cd todo-db

# Install dependencies with all optional extras and dev dependencies
uv sync --all-extras
```

## Running Validation Locally

Before opening a pull request, verify that all local checks pass:

```sh
# 1. Lint and style checks
uv run ruff check .

# 2. Run test suite
uv run pytest -q

# 3. Verify CLI and schema parity conformance
uv run python scripts/parity_conformance.py --check

# 4. Verify distribution build
uv build
```

If you modify MCP tools or arguments in `src/todo_db/mcp/`, update the frozen snapshots under `scripts/mcp_snapshots/` and run `tests/test_mcp_server.py` to ensure schema stability.

## Contribution Guidelines

1. **Focused Pull Requests**: Keep pull requests scoped to a single fix or feature.
2. **Deterministic Schemas & Exit Codes**: Exit codes (0, 1, 2, 4) and error code strings (`E_*` in `src/todo_db/errors.py`) are formal contracts. Do not alter existing codes without reviewing `docs/adr/` records.
3. **Redaction & Credential Safety**: Never log, print, or leak database tokens or connection strings. Use `HostedAuthError` and ensure any new output paths redact sensitive information.
4. **Tests**: Add test coverage for all new functionality or bug fixes under `tests/`.
5. **Changelog**: Document user-visible additions, changes, or deprecations in `CHANGELOG.md` following [Keep a Changelog](https://keepachangelog.com/).
