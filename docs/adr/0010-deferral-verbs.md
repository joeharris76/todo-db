# ADR 0010: First-Class Deferral Verbs

- **Status**: Accepted
- **Date**: 2026-09-27
- **Amends**: ADR 0007 (JSON/Git tracker), ADR 0006 G9 (tool surface)

## Context

The pre-simplification tracker (0.6.x) had `defer`, `promote`, and
`dismiss` verbs over a `deferrals` SQL table. The JSON/Git rewrite
(0.7.0) dropped them: migration archives old deferral rows into
per-item `legacy` detail, and no new deferral can be recorded or
resolved. A review sweep that left 27 open deferrals needing maintainer
promote/dismiss calls had nowhere to record its verdicts except item
prose, and `finish` had no gate on unresolved deferred work.

## Decision

Restore deferrals as first-class, detail-owned state with four MCP
tools over shared service operations:

- `defer(id, summary, reason)` records an open row on a task without
  changing its status or claim.
- `list_deferrals(from_item?, resolution?, limit?, cursor?)` reads the
  queue: open rows by default, `all` for every resolution, paged with
  stable cursors scoped to the state revision plus the query. Rows
  carry `reason` always, plus `resolved_item`/`resolved_reason` when
  set, so triage needs no second read.
- `promote_deferral(id, deferral_id, to_item?, title?, priority?)`
  resolves an open row into a linked planning item: it creates the
  successor (or links an existing `to_item`) and flips the row to
  `promoted` in one atomic snapshot change.
- `dismiss_deferral(id, deferral_id, reason)` resolves an open row as
  deliberately dropped, with a mandatory reason.

Rules:

- **Detail-owned, additive.** Rows live in the source task's detail
  file under `deferrals`, keyed by positive integer ids scoped to that
  task. The index shape is unchanged, so older readers load snapshots
  that carry deferrals. No schema version bump: like `not_before` and
  `sessions`, older servers preserve rows they do not enforce.
- **Finish gate.** `finish` refuses with `E_OPEN_DEFERRALS` while any
  row on the task stays open, naming the blocking ids. `show_item`
  reports them as `open_deferrals` so agents see the gate before
  hitting it.
- **No claim required.** Resolving a deferral needs no claim on the
  source item, by analogy with `drop` on unclaimed tasks. Promotion
  validates the successor exactly like `create_item`; summaries share
  the 200-char title bound so every recorded deferral is promotable
  with its default title.
- **Audited.** `defer`/`promote`/`dismiss` record session-history
  entries on the source task; a promotion that creates its successor
  annotates the new item under the same operation id. Linking to a
  pre-existing item annotates only the source.
- **MCP-only mutations.** Per ADR 0006 G1/G2, the verbs are MCP tools,
  not CLI commands. The CLI keeps bootstrap, validation, migration,
  recovery, and read-only list/show.

## Consequences

- The MCP surface grows from 15 to 19 tools; the measured startup
  schema-plus-instructions cost rises from ~3.7k to ~4.4k tokens,
  still far below the 0.6.x 26-tool surface.
- `finish` gains a new refusal mode. Agents that defer work and then
  finish the source task must resolve the queue first; the error names
  the ids and the recovery is explicit.
- Legacy `legacy.deferrals` rows from migration stay archival prose.
  They are not revived as live rows; new deferrals start at id 1 per
  task.
