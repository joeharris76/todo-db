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
3. `create_item` -- id, title, priority (default medium),
   description, needs, acceptance, links, context.
4. `take` -- claim a task; returns the claim generation plus enough
   context to begin work. One live claim per worker.
5. `release` -- hand the claim back (needs the generation from take).
6. `finish` -- close the task (needs the generation from take).
   `drop` abandons a task; it is refused while another worker holds it.
7. `renew` -- extend a long-running claim (same generation, no
   progress milestones required).

`update_item` edits title/priority/description/needs/sections/status
(open/blocked moves only; closing goes through finish/drop).

## Responses

    {"ok": true,  "data": {...}}
    {"ok": false, "code": "E_...", "error": "...", "recovery": [...], "kind": "gate|error"}

`kind: "gate"` is an expected result to act on; `kind: "error"` is a
failure -- stop and report it. Read `recovery` before improvising.
Responses are capped at 16 KiB; lists page with cursors scoped to a
state revision (a changed revision returns E_CURSOR_STALE: restart
from the first page).

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

One server instance is one worker identity: concurrent workers run
separate servers with different --actor values (or distinct client
names when no --actor is set). Claims are cooperative, not access
control.
"""
