"""Workflow instructions surface for the MCP server.

Short protocol guidance loaded once. It names the loop and the error
codes; it does not inject guides, task bodies, or the index.
"""

from __future__ import annotations

INSTRUCTIONS = """\
# todo-db agent workflow

Drive the tracker through MCP tools. Mutations return a compact
acknowledgement; lists return brief rows (id/title/priority/status).

## The loop

1. `list_items` -- brief rows; filters: status, priority, text,
   ready_only; paging: limit (default 5) + cursor.
2. `show_item` -- one task with needs, readiness, and sections.
   Large fields arrive as section reads: field/offset/budget.
3. `register_batch` -- register the immutable repository/integration contract
   before enrolling prepared members. The first schema-3 registration needs
   `confirm_schema3_cutover=true` once every process with state-branch access
   is stopped or upgraded; older clients cannot coexist after it.
4. `create_item` -- a task; optional batch metadata is `batch_id`,
   `member_id`, `implementation_dependencies`.
5. `take` -- claim a task; returns the claim generation plus enough
   context to begin work. One live claim per worker.
6. `prepare` -- persist a member receipt and release its claim, not done.
   Needs the registered batch, explicit same-batch edge, actual member base,
   accepted/current heads, frozen scope, clean exact checkout, and passed
   bounded-suite evidence.
7. `bind_batch_pr` -- bind exactly one final PR identity after integration.
8. `abort_batch` -- owner-only, resumable, after all member claims are
   released. Invalidates prepared/final evidence, keeps any bound final PR
   receipt, and returns non-terminal members to ordinary claim/finish outside
   the archived batch. Refused once a member is done, so partial closeout
   resumes without reopening it.
9. `release` -- hand the claim back (needs the generation from take).
10. `finish` -- close the task (needs the generation from take). Prepared
   tasks additionally require current combined-tree evidence covering every
   prepared member and the registered final PR.
   `drop` abandons a task; a live claim needs its generation.
11. `renew` -- extend a long-running claim (same generation, no
   progress milestones required).

`update_item` edits title/priority/description/needs/sections/status
(open/blocked moves only; closing goes through finish/drop). Batch metadata
is explicit, immutable after assignment, and never inferred from an ordinary
dependency. A prepared member cannot be dropped while its batch is active;
complete or abort the batch instead.

`not_before` (future RFC 3339) holds an open task until then; `""` clears.

## Session history

Mutations record actor, session, operation, and operation ID per task.
After `take`, read `show_item`'s `sessions`; before
`release`/`finish`, leave resumption notes in `context`. Aborts,
invalidations, and dead-session takeovers record on members;
`takeover` needs the holder seen plus why it is dead.

## Responses

    {"ok": true,  "data": {...}}
    {"ok": false, "code": "E_...", "error": "...", "recovery": [...], "kind": "gate|error"}

`kind: "gate"` is an expected result to act on; `kind: "error"` is a
failure -- stop and report it. Read `recovery` before improvising.
Responses are capped at 16 KiB; list cursors are scoped to a state
revision.

## Codes

- `E_NOTHING_READY` -- nothing claimable. Report it; do not invent work.
- `E_MULTIPLE_CLAIMS` -- you already hold a claim. Finish or release it.
- `E_CLAIM_STALE` -- wrong generation or another holder. Show and retry.
- `E_CONFLICT` -- someone changed the task first. Re-read, re-evaluate.
- `E_CURSOR_STALE` -- state moved under your pages. Restart listing.
- `E_OVERSIZED` -- one field exceeds the cap. Use its section read.
- `E_OFFLINE` -- remote unreachable. Reads may be cached (marked
  stale); mutations fail rather than succeeding locally.
- `E_UNKNOWN` -- outcome undetermined; reconcile with the operation ID.
- `E_NO_PRINCIPAL` -- call `get_instructions`, then retry.

One server instance is one logical worker. Default identities are isolated per
server, even for clients with the same name. Use a stable, unique --actor for
restart continuity and do not run that actor concurrently. Claims are
cooperative, not access control.
"""
