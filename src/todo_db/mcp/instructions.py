"""Workflow instructions surface for the MCP server.

The text below is copied verbatim from ``todo_db.cli`` ``_agent_instructions``
(the ``todo agent instructions`` output). It is duplicated here on purpose: the
MCP package must not import from ``cli.py`` (scope + layering). Keep the two in
sync until the CLI copy is removed in the 0.6.0 destructive phase.
"""

from __future__ import annotations

INSTRUCTIONS = (
    "# Autonomous Agent Workflow Protocol\n\n"
    "1. Inspect queue or existing claim:\n"
    "   `todo agent next`\n\n"
    "2. Claim ready work or re-adopt active claim:\n"
    "   `todo agent take [id] [--session <session-id>]`\n\n"
    "3. Retrieve bounded context with guardrails:\n"
    "   `todo agent context <id>`\n\n"
    "4. Execute work units sequentially and record progress:\n"
    "   `todo agent progress <id> <wid> --evidence '<description of completed unit>'`\n\n"
    "5. Ask the no-shell finish gate to require a current verification attestation:\n"
    "   `todo agent finish <id> --claim-token <token> --model-assert`\n\n"
    "6. If verification is stale, a human reviews the printed commands and runs:\n"
    "   `todo agent finish <id> --claim-token <token> --run-verifications`\n\n"
    "Notes:\n"
    "- A single active claim is enforced per principal.\n"
    "- Scope rules are checked before and after the single verification run.\n"
    "- Diverged baselines require `todo agent rebaseline` from a clean worktree.\n"
)
