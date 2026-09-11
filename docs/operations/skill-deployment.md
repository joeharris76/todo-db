# Skill deployment

## What ships, and why

This repository commits the agent skills it needs into three tracked trees:
`.claude/skills/`, `.codex/skills/`, and `.gemini/skills/`. They are generated
mirrors, not hand-edited files.

They are committed on purpose so contributors, CI runners, and agents have
tracker and workflow skills without needing manual package installation.
The disk cost of three mirrors is the price.

## Sources

Skills come from two Git sources, pinned in `skill-sync.conf`. Each target also
carries a `skill-sync.receipt` (provenance and ownership) and a
`skill-sync.manifest` (sha256 and mode of every file written):

| Source | Type | Owns |
| --- | --- | --- |
| `product` | git | the `skill-sync` operator skill |
| `catalog` | git | shared development-workflow skills and canonical `todo` skill |

The `todo` skill is **owned by the `skill-sync-skills` catalog**. The three tracked
trees are materializations of it. Never hand-edit a mirror — `skill-sync verify`
compares mirrors against the manifest and will fail on a hand-edited target.

The managed block in `.gitattributes` marks the target trees `-text` so digests
are stable across platforms. The managed block in `.gitignore` covers only the
loader-reserved `.system/` path inside each target.

## Making a change

```sh
skill-sync preview                   # show what apply would change
skill-sync apply                     # copy the payload (needs source checkouts)
skill-sync verify                    # offline integrity gate (needs no sources)
git add skill-sync.conf \
        .claude/skills .codex/skills .gemini/skills .gitattributes
```

`skill-sync` here is the wrapper from `joeharris76/skill-sync` at the revision
pinned in CI: check out that revision and run its `bin/skill-sync`. It never
fetches: `source` lines in `skill-sync.conf` must be local checkouts of the
catalog and product repositories.

### Same-commit invariant

After any skill change, these must land together in one commit:

1. the pins in `skill-sync.conf`
2. the regenerated `skill-sync.receipt` and `skill-sync.manifest` in each target
3. the three target trees, plus `.gitattributes` if it moved

CI enforces this with `skill-sync verify`, which needs no source access: it
proves the committed mirrors are exactly what skill-sync wrote.
`skill-sync preview` is the separate freshness check and does need source
access.

## Advancing a git-sourced skill

Prefer advancing `skill-sync.conf` revs to a merged, published revision on the
source's default branch. Fetch that revision into the local checkout first:
skill-sync resolves the rev locally and refuses to label working-tree bytes
with a clean commit SHA.
