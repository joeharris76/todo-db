---
name: substack
description: "`auth`, `status`, `preview`, `draft`, `publish`, `schedule`, `update`, `suggest_tags`, `pending`, `fetch`, `sync`, `pull`"
---

# Substack Workflow

Use the matching `letterops` MCP action; do not invent higher-level aliases.

## Actions

| Action | Read |
|---|---|
| `auth` | `references/read.md` |
| `status` | `references/read.md` |
| `preview` | `references/read.md` |
| `draft` | `references/drafts.md` |
| `publish` | `references/publication.md` |
| `schedule` | `references/publication.md` |
| `update` | `references/drafts.md` |
| `suggest_tags` | `references/drafts.md` |
| `pending` | `references/read.md` |
| `fetch` | `references/read.md` |
| `sync` | `references/sync.md` |
| `pull` | `references/sync.md` |

## Global rules

- Start with read-only `auth`, `status`, `preview`, and `pending` checks. Dry-run
  mutations before execution. After publishing or scheduling, use `fetch`, not
  `pull`.
- Never pull into `_blog/published/` without explicit review of the dry-run.
- If a write action changes local/state files, commit through
  `shared-change-framework/SKILL.md`; read-only actions do not create commits.
- Config is read from `_project/substack/config.yaml` through
  `LETTEROPS_CONFIG`. Paths may be repo-relative or absolute; published
  markdown under `_blog/published/` is the local source of truth.
- Execute live mutations only after the required user confirmation.
