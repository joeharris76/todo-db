"""FastMCP stdio server foundation for the todo-db tracker.

This package is the skeleton the MCP lifecycle tools plug into. It builds the
server, resolves the project/database target, opens the database READ_ONLY at
startup for a schema/identity check (never migrates), resolves an explicit
principal (never ``default_actor()``), owns the single worker thread that all
database and git work is dispatched to, and exposes the workflow instructions
surface. The lifecycle tools (``next``/``take``/``context``/...) are a later
migration item and are intentionally absent here.

Import this package only when the ``mcp`` optional dependency is installed
(``pip install 'todo-db[mcp]'``).
"""

from __future__ import annotations

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    """Entry point shim so ``todo_db.mcp:main`` works; see ``server.main``."""

    from .server import main as _main

    return _main(argv)
