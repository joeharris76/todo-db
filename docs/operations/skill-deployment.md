# Skill deployment

## What ships, and why

This repository commits the agent skills it needs into three tracked trees:
`.claude/skills/`, `.codex/skills/`, and `.gemini/skills/`. They are generated
mirrors, not hand-edited files.

They are committed on purpose. Anyone who clones this repository — a new
contributor, a CI runner, a web agent with no home directory — gets the
`todo-db` skill without installing `skill-sync` or adopting any other project.
That self-containment is the requirement; the disk cost of three mirrors is the
price.

## Sources

Skills come from three sources, pinned in `skill-sync.yaml` and resolved into
the integrity manifest `skill-sync.lock`:

| Source | Type | Owns |
| --- | --- | --- |
| `project` | local (`skills/`) | `todo-db` — this product's own skill |
| `product` | git | the `skill-sync` operator skill |
| `catalog` | git | shared development-workflow skills |

The `todo-db` skill is **owned by this repository**. Its editable source is
`skills/todo-db/`; the three tracked trees are materializations of it. Edit the
source, never a mirror — `skill-sync verify` compares mirrors against the lock
and will fail on a hand-edited target.

The managed block in `.gitattributes` marks the target trees `-text` so digests
are stable across platforms. The managed block in `.gitignore` covers only the
loader-reserved `.system/` path inside each target.

## Making a change

```sh
$EDITOR skills/todo-db/SKILL.md      # edit the source
npx skill-sync sync --dry-run        # preview
npx skill-sync sync                  # apply and regenerate the lock
npx skill-sync verify                # offline integrity gate
git add skill-sync.yaml skill-sync.lock skills \
        .claude/skills .codex/skills .gemini/skills .gitattributes
```

### Same-commit invariant

After any skill change, these must land together in one commit:

1. the source under `skills/` (for repo-owned skills) or the pins in
   `skill-sync.yaml` (for git sources)
2. the regenerated `skill-sync.lock`
3. the three target trees, plus `.gitattributes` if it moved

CI enforces this with `skill-sync verify`, which needs no source access: it
proves the committed mirrors match the lock. `skill-sync sync --dry-run` is the
separate freshness check and does need source access.

The workflow clones `joeharris76/skill-sync` at a pinned commit, builds it, and
runs that binary. `npx github:…#<sha>` is not used: on the GitHub-hosted Ubuntu
npm it fails with `GitFetcher requires an Arborist constructor to pack a
tarball` (npm/cli#6723).

## Advancing a git-sourced skill

Prefer advancing `skill-sync.yaml` refs to a merged, published revision on the
source's default branch. A git source is cloned at depth 1 of that branch, so a
ref that exists only on a feature branch fails as a missing ref. Test a
feature branch through project-local targets sourced from its worktree rather
than repointing a shared store.
