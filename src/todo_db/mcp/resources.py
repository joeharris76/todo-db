"""The instructions surface: a resource, a portable fallback tool, and a prompt.

All three return the same workflow text, rendered by
:func:`todo_db.mcp.instructions.render_instructions` with a section naming the bound store.
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
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from ..errors import E_STATE
from ..service import err
from .identity import PrincipalHolder
from .instructions import render_instructions
from .target import ResolvedTarget

_RESOURCE_URI = "todo://instructions"
LOG = logging.getLogger("todo_db.mcp")


def _client_info(ctx: Context | None):
    if ctx is None:
        return None
    try:
        return ctx.request_context.session.client_params.clientInfo
    except AttributeError:
        return None


def _check_expected_store(target: ResolvedTarget, expected: str) -> dict[str, Any] | None:
    """None when *expected* occurs, ignoring case, in the redacted remote.

    The branch is not searched: every store's default branch is ``todo-state``,
    so a fragment such as ``todo`` would otherwise match a tracker for any project.
    """
    needle = expected.strip().lower()
    if not needle:
        return err(E_STATE, "expect_store must not be empty")
    actual = target.state_ref.display
    if needle in target.state_ref.display_remote.lower():
        return None
    return err(
        E_STATE,
        f"bound store {actual} does not match expected {expected.strip()!r}",
        recovery=[
            "do not write to this tracker",
            "restart todo-db-mcp with the project's --config, or launch it from the project directory",
        ],
        kind="gate",
    )


def register_instructions(
    server: FastMCP, target: ResolvedTarget, principal: PrincipalHolder | None = None,
) -> None:
    def _pin_principal(ctx: Context | None) -> None:
        if principal is None or not principal.pending:
            return
        pinned = principal.ensure(_client_info(ctx))
        if pinned is not None:
            LOG.info("principal: %s (derived from initialize clientInfo)", pinned)

    def _render() -> str:
        return render_instructions(target, principal.principal if principal is not None else None)

    @server.resource(
        _RESOURCE_URI,
        name="todo-instructions",
        title="todo-db agent workflow",
        description="The autonomous-agent workflow protocol for the todo-db tracker.",
        mime_type="text/markdown",
    )
    def _instructions_resource() -> str:
        return _render()

    @server.tool(
        name="get_instructions",
        description=(
            "Return the todo-db autonomous-agent workflow protocol as Markdown, including the "
            "bound state store. Pass expect_store, a fragment of the repository name the work "
            "belongs to, to refuse a tracker for another project. Portable fallback for the "
            "todo://instructions resource and the todo/workflow prompt."
        ),
    )
    def get_instructions(
        expect_store: str | None = None, ctx: Context = None,  # type: ignore[assignment]
    ) -> str | dict[str, Any]:
        _pin_principal(ctx)
        if expect_store is not None:
            mismatch = _check_expected_store(target, expect_store)
            if mismatch is not None:
                return mismatch
        return _render()

    @server.prompt(
        name="todo/workflow",
        title="todo-db agent workflow",
        description="The autonomous-agent workflow protocol for the todo-db tracker.",
    )
    def _workflow_prompt() -> str:
        return _render()
