"""The instructions surface: a resource, a portable fallback tool, and a prompt.

All three return the same workflow text (:data:`todo_db.mcp.instructions.INSTRUCTIONS`).
Clients differ in what they support, so the text is offered three ways:

- ``todo://instructions`` resource   -- the portable path.
- ``get_instructions`` tool          -- the portable fallback for clients that
  do not surface resources or prompts (and the only tool this foundation ships).
- ``todo/workflow`` prompt           -- additive; Claude Code surfaces it as a
  slash command, other clients ignore it.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .instructions import INSTRUCTIONS

_RESOURCE_URI = "todo://instructions"


def register_instructions(server: FastMCP) -> None:
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
    def get_instructions() -> str:
        return INSTRUCTIONS

    @server.prompt(
        name="todo/workflow",
        title="todo-db agent workflow",
        description="The autonomous-agent workflow protocol for the todo-db tracker.",
    )
    def _workflow_prompt() -> str:
        return INSTRUCTIONS
