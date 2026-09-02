"""FastMCP stdio server foundation for the todo-db tracker.

This module builds the server skeleton: launch-arg parsing, stderr-only logging,
target resolution + a READ_ONLY startup schema/identity check that never
migrates, explicit identity, the single worker thread, and the instructions
surface. The lifecycle tools are a later migration item and are absent here;
``tools/list`` returns only ``get_instructions``.
"""

from __future__ import annotations

import argparse
import logging
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass

from ..errors import SchemaBehindError, SchemaMismatchError, TodoError
from ..models import CredentialMode
from .identity import Identity, resolve_identity
from .instructions import INSTRUCTIONS
from .target import ResolvedTarget, resolve_target
from .worker import run_in_worker, shutdown_worker

LOG = logging.getLogger("todo_db.mcp")

_INSTALL_HINT = "todo-db[mcp] is required: pip install 'todo-db[mcp]'"
_LOG_LEVELS = ("debug", "info", "warning", "error")


@dataclass(frozen=True)
class LaunchConfig:
    target: ResolvedTarget
    identity: Identity
    profile: str
    allow_hosted: bool
    log_level: str = "info"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="todo-db-mcp",
        description="MCP stdio server for the todo-db tracker (foundation; lifecycle tools land later).",
    )
    parser.add_argument("--config", help="path to a .todo-db/config.json (overrides discovery)")
    parser.add_argument("--db", help="local SQLite path or secure libsql/https URL")
    parser.add_argument("--repo-root", help="project root for config discovery and git scope (default: cwd)")
    parser.add_argument("--actor", help="explicit audit principal (else TODO_DB_ACTOR, else derived at initialize)")
    parser.add_argument("--session", help="session id override (default: per-process uuid4 hex)")
    parser.add_argument("--profile", choices=("agent", "full"), default="agent", help="tool profile (default: agent)")
    parser.add_argument("--log-level", choices=_LOG_LEVELS, default="info", help="stderr log level (default: info)")
    parser.add_argument(
        "--allow-hosted",
        action="store_true",
        help="permit a hosted (Turso/libSQL) target; the server is local-SQLite-first",
    )
    return parser


def configure_logging(level: str) -> None:
    """Structured logging to stderr only -- stdout carries JSON-RPC framing."""

    root = logging.getLogger("todo_db.mcp")
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper()))
    root.propagate = False


def resolve_launch_config(args: argparse.Namespace) -> LaunchConfig:
    target = resolve_target(config=args.config, db=args.db, repo_root=args.repo_root)
    if target.is_hosted and not args.allow_hosted:
        raise TodoError(
            f"refusing to start against a hosted target ({target.db_target!r}): the MCP server is "
            "local-SQLite-first; pass --allow-hosted to override (experimental, plan §12)."
        )
    identity = resolve_identity(args.actor, args.session)
    return LaunchConfig(
        target=target,
        identity=identity,
        profile=args.profile,
        allow_hosted=bool(args.allow_hosted),
        log_level=args.log_level,
    )


def startup_check(target: ResolvedTarget) -> None:
    """Open the database READ_ONLY to run schema + identity checks only.

    READ_ONLY open runs ``_check_schema()`` + ``_check_identity()`` and never
    ``_migrate()``. The connection is closed immediately; tools (later item)
    open their own connection per call.
    """

    from ..database import TodoDatabase

    ro_config = target.database_config(CredentialMode.READ_ONLY)
    try:
        database = TodoDatabase.open(ro_config)
    except SchemaBehindError as exc:
        raise TodoError(f"E_SCHEMA: database schema is behind this package; run `todo-db migrate` ({exc})") from exc
    except SchemaMismatchError as exc:
        raise TodoError(
            f"E_SCHEMA: database schema diverged from this package; the package may be stale ({exc})"
        ) from exc
    database.close()


@asynccontextmanager
async def _lifespan(server: "FastMCP", launch: LaunchConfig):  # noqa: F821 - forward ref, mcp optional
    ident = launch.identity
    LOG.info("session id: %s", ident.session_id)
    if ident.actor_pending:
        LOG.info("principal: pending (no --actor/TODO_DB_ACTOR; derived at initialize)")
    else:
        LOG.info("principal: %s", ident.actor)
    LOG.info(
        "target: %s (source=%s, repo_root=%s)",
        launch.target.db_target,
        launch.target.source,
        launch.target.repo_root,
    )
    await run_in_worker(startup_check, launch.target)
    LOG.info("startup schema/identity check passed (READ_ONLY, no migration)")
    try:
        yield {"launch": launch, "identity": ident, "target": launch.target}
    finally:
        shutdown_worker(wait=True)


def build_server(launch: LaunchConfig) -> "FastMCP":  # noqa: F821
    from mcp.server.fastmcp import FastMCP

    from .resources import register_instructions

    @asynccontextmanager
    async def lifespan(server: FastMCP):
        async with _lifespan(server, launch) as ctx:
            yield ctx

    server = FastMCP(
        name="todo-db",
        instructions=INSTRUCTIONS,
        log_level=launch.log_level.upper(),
        lifespan=lifespan,
    )
    register_instructions(server)
    return server


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.log_level)

    try:
        import mcp  # noqa: F401
    except ModuleNotFoundError:
        print(_INSTALL_HINT, file=sys.stderr)
        return 1

    try:
        launch = resolve_launch_config(args)
        server = build_server(launch)
    except TodoError as exc:
        print(f"todo-db-mcp: {exc}", file=sys.stderr)
        return 2

    server.run("stdio")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
