# Blog Critique Reference

Use for adversarial draft review.

Apply `shared-review-protocol/references/adversarial-review.md` with `change`
scope. This file adds the blog-specific rubric and readiness score.

## Rubric

| Area | Questions |
|---|---|
| Thesis | Is the claim specific, defensible, and worth reading? |
| Audience | Is the reader clear, and are prerequisites handled? |
| Evidence | Are technical claims sourced, measured, or reproducible? |
| Structure | Does each section advance the argument without boilerplate? |
| Voice | Does the section lead with its purpose and state it once? Does each negative sentence add a fact, legal boundary, or scope condition? Do qualifications sit where they change interpretation? Do repeated caveats serve a new reader job? Identity, neutrality, and no superlatives still apply. |
| Utility | Does the reader leave with a usable insight, command, or decision? |
| Risk | What could be misleading, stale, partisan, overclaimed, or underqualified? |
| Shelf-life | For vendor-response posts, compare outline and source dates; flag a response window beyond the typical 1-2 weeks. |

A single redundant denial ("X is Y. X is not Z") is a Voice fail. A negation that is the news, a legal bound, or a necessary condition is not.

## Vendor-Response Checks

Apply only when a post responds to a vendor or source author.

- **Currency (Risk lane):** verify blocked TODOs, deferrals, and "not yet
  shipped" claims
  against `git log`, `todo list`, and `todo show <id>`. Flag work shipped after
  the outline date.
- **Partisan reader (Voice lane):** flag contrasts such as boring/novel,
  surface/hidden, and obvious/clever when the source's team could find them
  dismissive. Replace rankings with the exact API, benchmark coverage, or
  operational limitation.

## Scoring

9-10 publish-ready, 7-8 targeted edits, 5-6 significant revision, <5 structural rethink.

## Output

Lead with blockers, then targeted improvements, useful rewrites, and publish
readiness. Separate factual corrections from taste.

## Authorized follow-up fixes

After later authorization, fix only broken links, formatting, obvious factual
errors, and local wording. Leave thesis, framing, and controversial judgments
to the user.
