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
import sqlite3
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass

from ..database import TOOL_VERSION
from ..errors import HostedAuthError, SchemaBehindError, SchemaMismatchError, TodoDBError, TodoError
from ..models import CredentialMode
from .identity import Identity, PrincipalHolder, resolve_identity
from .instructions import INSTRUCTIONS
from .target import ResolvedTarget, resolve_target
from .worker import run_in_worker_sync, shutdown_worker

LOG = logging.getLogger("todo_db.mcp")

_INSTALL_HINT = "the mcp extra is required: install todo-db with the [mcp] extra"
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
        description="MCP stdio server for the todo-db tracker: the agent interface for planning and workflow.",
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
            "local-SQLite-first; pass --allow-hosted to override (experimental, plan §12).",
            code="E_HOSTED",
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
        raise TodoError(
            f"E_SCHEMA: database schema is behind this package; run `todo-db migrate` ({exc})",
            code="E_SCHEMA",
        ) from exc
    except SchemaMismatchError as exc:
        raise TodoError(
            f"E_SCHEMA: database schema diverged from this package; the package may be stale ({exc})",
            code="E_SCHEMA",
        ) from exc
    database.close()


@asynccontextmanager
async def _lifespan(server: "FastMCP", launch: LaunchConfig, principal: PrincipalHolder):  # noqa: F821 - forward ref, mcp optional
    ident = launch.identity
    LOG.info("session id: %s", ident.session_id)
    if principal.pending:
        LOG.info("principal: pending (no --actor/TODO_DB_ACTOR; derived at initialize)")
    else:
        LOG.info("principal: %s", principal.principal)
    LOG.info(
        "target: %s (source=%s, repo_root=%s)",
        launch.target.db_target,
        launch.target.source,
        launch.target.repo_root,
    )
    LOG.info("startup schema/identity check passed (READ_ONLY, no migration)")
    try:
        yield {"launch": launch, "identity": ident, "target": launch.target, "principal": principal}
    finally:
        shutdown_worker(wait=True)


def build_server(launch: LaunchConfig) -> "FastMCP":  # noqa: F821
    from mcp.server.fastmcp import FastMCP

    from .resources import register_instructions
    from .tools_work import register_work_tools

    principal = PrincipalHolder(launch.identity)

    @asynccontextmanager
    async def lifespan(server: FastMCP):
        async with _lifespan(server, launch, principal) as ctx:
            yield ctx

    server = FastMCP(
        name="todo-db",
        instructions=INSTRUCTIONS,
        lifespan=lifespan,
    )
    # FastMCP does not forward a version to the lowlevel server, so `serverInfo`
    # would otherwise report the MCP SDK's own version to every client. The
    # attribute is reachable only through FastMCP's private handle, so a future
    # SDK could move it; a cosmetic label must not break startup if it does.
    try:
        server._mcp_server.version = TOOL_VERSION
    except AttributeError:  # pragma: no cover - depends on the installed SDK
        LOG.debug("MCP SDK does not expose a server version field; leaving it unset")
    register_instructions(server, principal)
    register_work_tools(server, launch.target, principal, launch.identity.session_id, allow_hosted=launch.allow_hosted)

    # Query and planning tools load in every profile: reads are cheap and
    # constantly wanted, and an agent that cannot create an item cannot use
    # the tracker at all. `full` adds findings and admin on top.
    from .tools_full import register_full_tools, register_planning_tools
    from .tools_query import register_query_tools

    register_query_tools(server, launch.target, principal, launch.identity.session_id, allow_hosted=launch.allow_hosted)
    if launch.profile == "full":
        register_full_tools(server, launch.target, principal, launch.identity.session_id, allow_hosted=launch.allow_hosted)
    else:
        register_planning_tools(
            server, launch.target, principal, launch.identity.session_id, allow_hosted=launch.allow_hosted
        )
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
        # Run the startup gate BEFORE entering the MCP run loop, inside the
        # same try/except that handles all startup failures (B2/B3). This gives
        # a clean one-line message + exit 2 instead of an uncaught traceback.
        run_in_worker_sync(startup_check, launch.target)
        LOG.info("startup schema/identity check passed (READ_ONLY, no migration)")
        server = build_server(launch)
    except HostedAuthError as exc:
        label = f"error [{exc.code}]" if exc.code else "error"
        print(f"{label}: {exc}", file=sys.stderr)
        return 2
    except (TodoDBError, TodoError, OSError, ValueError, sqlite3.Error) as exc:
        msg = str(exc)
        # Helpful hint for the common first-run failure: no database file yet.
        if (
            isinstance(exc, (OSError, sqlite3.OperationalError))
            or "unable to open" in msg.lower()
            or "no such file" in msg.lower()
        ):
            print(f"error: {exc} (hint: run `todo-db init-project` or `todo-db migrate` first)", file=sys.stderr)
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        server.run("stdio")
    except (TodoDBError, HostedAuthError, OSError, ValueError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
