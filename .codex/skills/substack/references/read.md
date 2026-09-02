# Read-Only Substack Actions

## `auth`

Use `mcp__letterops__auth(test=true)` before a live operation or when
troubleshooting credentials/connectivity.

## `status`

Use `mcp__letterops__status`; add `remote: true` for the remote view. CLI
fallback: `make tools-run CMD='status --remote'`.

## `preview`

Read the target, call `mcp__letterops__preview` with `file_path`, and report
validation plus the rendered block summary. Suggest fixes before draft/publish
when invalid.

## `pending`

Use `mcp__letterops__pending` to list local files changed since the last sync.
CLI fallback: `make tools-run CMD='pending'`.

## `fetch`

Use `mcp__letterops__fetch` to refresh remote state and local hashes without
pulling content. Prefer it after `publish` or `schedule` and commit state
changes through the commit framework. CLI fallback:
`make tools-run CMD='status --remote'`.
