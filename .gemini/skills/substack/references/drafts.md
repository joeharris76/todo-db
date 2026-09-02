# Draft Actions

## `draft`

Preview the Markdown file, then call `mcp__letterops__draft`. Dry-run when
supported and ask before `execute: true`. Set optional audience and tags,
return the draft URL or post ID, and commit local state changes.

## `update`

Use `mcp__letterops__update` for an existing draft without publishing. Preview
if the file was not recently validated, then commit local tracking changes.

## `suggest_tags`

Use `mcp__letterops__suggest_tags` with `file_path` before drafting or
publishing.

Use free audience for open-source deep dives, methodology, history, and feature
series; use paid for cloud-platform or SF1000+ benchmarks. Confirm missing
audience metadata before `draft`, `publish`, or `schedule`.
