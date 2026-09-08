"""FastMCP stdio server for the todo-db tracker: the agent interface (ADR 0007).

This package builds the server and registers the tracker tools. It resolves the
state branch target, verifies it read-only at startup (never bootstraps),
resolves an explicit worker identity (never a placeholder), and registers eight
tools over shared service operations.
"""

from __future__ import annotations

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    """Entry point shim so ``todo_db.mcp:main`` works; see ``server.main``."""

    from .server import main as _main

    return _main(argv)
