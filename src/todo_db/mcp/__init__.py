"""FastMCP stdio server for the todo-db tracker: the agent interface (ADR 0006).

This package builds the server and registers the tracker tools. It resolves the
project/database target, opens the database READ_ONLY at startup for a
schema/identity check (never migrates), resolves an explicit principal (never
``default_actor()``), and owns the single worker thread that all database and
git work is dispatched to.

Import this package only when the ``mcp`` optional dependency is installed.
"""

from __future__ import annotations

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    """Entry point shim so ``todo_db.mcp:main`` works; see ``server.main``."""

    from .server import main as _main

    return _main(argv)
