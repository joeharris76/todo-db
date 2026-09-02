# Live Publication Actions

## `publish`

Preview first. Require explicit live-publication confirmation, dry-run when
available, then call `mcp__letterops__publish` with `execute: true` and
`confirm_live: true`. Run `fetch` afterward and commit state changes.
CLI fallback: `make tools-run CMD='publish <file_path> --execute --confirm-live'`.

## `schedule`

Confirm the ISO timestamp, then call `mcp__letterops__schedule` with
`execute: true` and `confirm_live: true`. Run `fetch` afterward and commit
state changes. CLI fallback:
`make tools-run CMD='schedule <file_path> "<ISO_DATETIME>" --execute --confirm-live'`.
