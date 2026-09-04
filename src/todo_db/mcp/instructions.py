"""Workflow instructions surface for the MCP server.

MCP-shaped guidance: the agent drives the tracker through typed tool calls, not
shell commands. Verification execution and rebaseline are deliberately **not**
tools (ADR 0006 G6) -- a human runs those from the floor CLI.

This text is the server's ``instructions``, the ``todo://instructions``
resource, the ``get_instructions`` tool, and the ``todo/workflow`` prompt. Keep
it compact: every client receives it.
"""

from __future__ import annotations

INSTRUCTIONS = """\
# todo-db agent workflow

Drive the tracker through MCP tools. Every response carries a `next_action`
naming the tool and arguments to call next -- follow it rather than guessing.

## The loop

1. `next` -- inspect the ready queue, or the claim you already hold.
2. `take` -- atomically claim a ready item, or re-adopt your active claim.
   Omit `id` to take the top of the queue.
3. `context` -- bounded context for the claimed item; also how you re-read
   `claim_token` and `next_action` after a restart.
4. `progress` -- mark each work unit done with evidence. Refreshes the lease.
5. `finish` -- the close gate.
6. `release` -- hand the claim back without finishing.

## Planning

`create_item` takes work units, scope rules, preserves, and verifications
together. An item with no scope rules or verifications fails `lint` and then
`finish`, so supply them when you create it. `update_item` amends an item
without touching its lifecycle; `add_dependency` records that one item needs
another.

## Responses

    {"ok": true,  "data": {...}}
    {"ok": false, "code": "E_...", "error": "...", "recovery": [...], "kind": "gate|error"}

`kind: "gate"` is an expected result to act on; `kind: "error"` is an
environment or protocol failure -- stop and report it. Read `recovery` before
improvising. Responses are capped at 16 KiB; list tools page rather than
truncate, so retry with a smaller `limit` plus `cursor`.

## Gates

- `E_NOTHING_READY` -- queue empty. Report it; do not invent work.
- `E_MULTIPLE_CLAIMS` -- you already hold a claim. Use `claims`, then finish or
  `release` it.
- `E_CLAIM_STALE` -- lease expired or wrong token. `context` to re-read.
- `E_SCOPE_GATE` -- a changed file is outside scope. Use `check_scope`; narrow
  the change, or amend scope deliberately with `update_item`.
- `E_LINT_GATE` -- planning quality insufficient. `lint` says why.
- `E_VERIFY_GATE` -- no current workspace attestation. Stop: a human runs the
  `todo-db verify-run` command given in `recovery`. Your `finish` still closes.
- `E_BASE_DIVERGED` / `E_BASE_UNREACHABLE` -- the scope git baseline no longer
  resolves. Stop; a human runs `todo-db rebaseline`.
- `E_NO_PRINCIPAL` -- principal not resolved. Call `get_instructions`, retry.
- `E_AUTH_MISSING` / `E_AUTH_REJECTED` -- hosted credential problem. Stop
  writing and report; credentials are provisioned outside the agent.

## Not tools, by design

Verification execution (`todo-db verify-run`) and scope rebaseline
(`todo-db rebaseline`) have no tool at any profile, because stored verification
commands are arbitrary code written by other actors. A human runs them.

One active claim is enforced per principal. Scope is re-checked on `progress`
and `finish`.
"""
