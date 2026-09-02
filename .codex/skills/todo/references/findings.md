# Finding Pipeline — Capture to Landing

`todo finding --help` is the contract. The pipeline below is the end-to-end flow from review capture to tracker landing.

## Verb table (nine verbs)

| Verb | Credential | What it does |
|---|---|---|
| `finding create --title ... --finding-kind ... --review-context ... --gate class-not-instance` | none (local drafts dir only) | Write a draft under `~/.todo-db/finding-drafts/<project-id>/` (or `--drafts-dir`). With `--finding-kind bug-class` requires `--fixed-by <ref>`. |
| `finding candidates` | none | List unsynced `*.md` drafts (local glob, zero-credential). `todo ready` / `todo stats` advertise this when drafts exist. |
| `finding list [--disposition ...] [--rank ...] [--json]` | read-only | List landed findings. |
| `finding show <id> [--json]` | read-only | Show one landed finding. |
| `finding sync` | **read-write — the credentialed landing step** | Validate every draft and land it into the tracker; rename landed drafts to `*.synced`. The only spell that writes findings to the DB. |
| `finding triage <id> [--urgency ...] [--breadth ...] [--confidence ...] [--disposition actionable|actioned] [--reason ...]` | read-write | Set judgement fields and/or move `open -> actionable` (or directly to `actioned`). |
| `finding dismiss <id> --reason ...` | read-write | Dismiss with reason (`open -> dismissed`, `actionable -> dismissed`). |
| `finding link <id> --kind informs|resolved-by|related-finding|duplicate-of --to-item <id> | --to-finding <id>` | read-write | Link a finding to a TODO or another finding. |
| `finding promote <id> --to-item <slug> [--title ...] [--priority ...] [--worktree ...]` | read-write | Atomically promote a landed finding to a planning item. |

`create` + `candidates` are credential-free (drafts-dir only); `sync` is the sole credentialed landing step that requires DB credentials.
`finding import` is not a verb; drafts arrive via `create` (or hand-written files that satisfy validation) and land via `sync`.

## When `ready` warns

`todo ready` (and `todo stats`) may emit a stderr banner about open findings or unsynced drafts. It does not change stdout.

1. Run `todo finding candidates` to see unsynced drafts. If any, run `todo finding sync` to land them.
2. Run `todo finding list` and `todo finding show <id>` to inspect landed findings, then `triage`, `link`, `dismiss`, or
   `promote` as appropriate.
3. Findings are not claimable work items and never appear in the ready queue.

## Draft format (what validation demands)

`finding sync` rejects drafts that do not match the file contract validated by `src/todo_db/findings.py`:

- Filename `YYYY-MM-DD-HHMMSS-<kebab-slug>.md` and `id` frontmatter equal to the stem.
- Frontmatter (`---` block): `id`, `date` (ISO), `status` (`open`), `finding_kind` (`framework-gap|bug-class|missed-axis|scope-creep|assumption|other`), `review_context` (non-empty), optional `observed_sha`, `related_paths`, `suggested_sweep`, `todo_id`, `evidence`, `urgency`, `breadth`, `confidence`.
- Body:
  - `# <title>` heading.
  - Required sections: `## Finding`, `## Why this matters`, `## Suggested next steps` (exact headings; hand-written drafts missing any of them fail validation).
  - When hand-writing, prefer `finding create --gate class-not-instance ...` so the tool generates a valid skeleton.

See `shared-review-protocol/SKILL.md` section 5 for the default binding (capture path, frontmatter, headings) and section 6 for the sync/lifecycle reference.

## Landing and lifecycle (after sync)

Landed findings move through `open -> actionable -> actioned|promoted|dismissed` (and `open -> actioned|dismissed|promoted`).
Terminal states are `actioned`, `dismissed`, `promoted`. `triage --reason` is required for `actionable`/`dismissed`; `dismiss --reason` likewise. `link` and `promote` commit atomically with the hash-chained audit log.
