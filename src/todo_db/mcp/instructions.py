"""Workflow instructions surface for the MCP server.

MCP-shaped guidance: the agent drives the tracker through typed tool calls, not
shell commands. Verification execution and rebaseline are deliberately **not**
tools (ADR 0006 G6 / plan §7) -- a human runs those from the floor CLI.
"""

from __future__ import annotations

INSTRUCTIONS = (
    "# Autonomous Agent Workflow Protocol\n\n"
    "Drive the tracker through these MCP tools. Every response carries a "
    "`next_action` naming the tool and arguments to call next.\n\n"
    "1. `next` -- inspect the ready queue or your existing claim.\n"
    "2. `take` -- atomically claim a ready item (or re-adopt your active claim). "
    "The server supplies its own session id.\n"
    "3. `context` -- fetch bounded, guardrailed context for the claimed item; "
    "also how you re-read `claim_token` and `next_action` after a restart.\n"
    "4. `progress` -- mark each work unit done with evidence; this refreshes the lease.\n"
    "5. `finish` -- the no-shell close gate. Model-assert only: it requires a "
    "current workspace-fingerprint attestation and rejects a stale pass.\n"
    "6. `release` -- hand the claim back without finishing.\n\n"
    "Notes:\n"
    "- One active claim is enforced per principal.\n"
    "- Scope rules are checked on `progress` and `finish`.\n"
    "- Verification execution and `rebaseline` are not tools. When `finish` "
    "reports a stale attestation, a human runs `todo-db verify-run` (or "
    "`todo-db rebaseline`) from the floor CLI; your `finish` call remains the closer.\n"
)
