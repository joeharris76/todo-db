"""The instructions surface: a resource, a portable fallback tool, and a prompt.

All three return the same workflow text (:data:`todo_db.mcp.instructions.INSTRUCTIONS`).
Clients differ in what they support, so the text is offered three ways:

- ``todo://instructions`` resource   -- the portable path.
- ``get_instructions`` tool          -- the portable fallback for clients that
  do not surface resources or prompts (and the only tool this foundation ships).
- ``todo/workflow`` prompt           -- additive; Claude Code surfaces it as a
  slash command, other clients ignore it.

``get_instructions`` doubles as the first-tool-call seam that pins the principal
from the ``initialize`` handshake when no ``--actor`` was supplied.
"""

from __future__ import annotations

import logging

from mcp.server.fastmcp import Context, FastMCP

from .identity import PrincipalHolder
from .instructions import INSTRUCTIONS

_RESOURCE_URI = "todo://instructions"
LOG = logging.getLogger("todo_db.mcp")


def _client_info(ctx: Context | None):
    if ctx is None:
        return None
    try:
        return ctx.request_context.session.client_params.clientInfo
    except AttributeError:
        return None


def register_instructions(server: FastMCP, principal: PrincipalHolder | None = None) -> None:
    def _pin_principal(ctx: Context | None) -> None:
        if principal is None or not principal.pending:
            return
        pinned = principal.ensure(_client_info(ctx))
        if pinned is not None:
            LOG.info("principal: %s (derived from initialize clientInfo)", pinned)

    @server.resource(
        _RESOURCE_URI,
        name="todo-instructions",
        title="todo-db agent workflow",
        description="The autonomous-agent workflow protocol for the todo-db tracker.",
        mime_type="text/markdown",
    )
    def _instructions_resource() -> str:
        return INSTRUCTIONS

    @server.tool(
        name="get_instructions",
        description=(
            "Return the todo-db autonomous-agent workflow protocol as Markdown. "
            "Portable fallback for the todo://instructions resource and the "
            "todo/workflow prompt."
        ),
    )
    def get_instructions(ctx: Context = None) -> str:  # type: ignore[assignment]
        _pin_principal(ctx)
        return INSTRUCTIONS

    @server.prompt(
        name="todo/workflow",
        title="todo-db agent workflow",
        description="The autonomous-agent workflow protocol for the todo-db tracker.",
    )
    def _workflow_prompt() -> str:
        return INSTRUCTIONS
