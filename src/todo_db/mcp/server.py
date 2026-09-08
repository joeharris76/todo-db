"""FastMCP stdio server for the JSON/Git TODO tracker.

This module builds the server: launch-arg parsing, stderr-only logging,
state-target resolution plus a read-only startup reachability check that
never bootstraps or migrates, explicit identity, the instructions
surface, and the eight tool registrations.
"""

from __future__ import annotations

import argparse
import logging
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass

from ..errors import TodoDBError, TodoError
from .identity import Identity, PrincipalHolder, resolve_identity
from .instructions import INSTRUCTIONS
from .target import ResolvedTarget, resolve_target

LOG = logging.getLogger("todo_db.mcp")

_INSTALL_HINT = "the mcp extra is required: install todo-db with the [mcp] extra"
_LOG_LEVELS = ("debug", "info", "warning", "error")


@dataclass(frozen=True)
class LaunchConfig:
    target: ResolvedTarget
    identity: Identity
    log_level: str = "info"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="todo-db-mcp",
        description="MCP stdio server for the JSON/Git TODO tracker: the agent interface.",
    )
    parser.add_argument("--config", help="path to a .todo-db/config.json (overrides discovery)")
    parser.add_argument("--state-remote", help="Git URL or path of the state remote")
    parser.add_argument("--state-branch", help="state branch (default: todo-state)")
    parser.add_argument("--cache-dir", help="local snapshot cache (default: ~/.cache/todo-db-state)")
    parser.add_argument("--repo-root", help="project root for config discovery (default: cwd)")
    parser.add_argument("--actor", help="explicit worker identity (else TODO_DB_ACTOR, else derived at initialize)")
    parser.add_argument("--session", help="session id override (default: per-process uuid4 hex)")
    parser.add_argument("--log-level", choices=_LOG_LEVELS, default="info", help="stderr log level (default: info)")
    return parser


def configure_logging(level: str) -> None:
    """Structured logging to stderr only -- stdout carries JSON-RPC framing."""

    root = logging.getLogger("todo_db")
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper()))
    root.propagate = False


def resolve_launch_config(args: argparse.Namespace) -> LaunchConfig:
    target = resolve_target(
        config=args.config,
        state_remote=args.state_remote,
        state_branch=args.state_branch,
        cache_dir=args.cache_dir,
        repo_root=args.repo_root,
    )
    identity = resolve_identity(args.actor, args.session)
    return LaunchConfig(target=target, identity=identity, log_level=args.log_level)


def startup_check(target: ResolvedTarget) -> None:
    """Verify the state remote is reachable and the branch exists.

    Read-only: resolves the accepted tip and never bootstraps, migrates,
    or writes. A missing branch is a bootstrap step a human runs from the
    CLI, not something the server improvises.
    """

    from ..git_backend import ls_remote_tip

    try:
        tip = ls_remote_tip(target.state_ref)
    except TodoDBError as exc:
        raise TodoError(f"state remote unreachable: {exc}", code="E_OFFLINE") from exc
    if tip is None:
        raise TodoError(
            f"state branch {target.state_ref.branch!r} does not exist on "
            f"{target.state_ref.remote!r}; run `todo-db bootstrap` first",
            code="E_STATE",
        )


@asynccontextmanager
async def _lifespan(server: "FastMCP", launch: LaunchConfig, principal: PrincipalHolder):  # noqa: F821 - forward ref, mcp optional
    ident = launch.identity
    LOG.info("session id: %s", ident.session_id)
    if principal.pending:
        LOG.info("principal: pending (no --actor/TODO_DB_ACTOR; derived at initialize)")
    else:
        LOG.info("principal: %s", principal.principal)
    LOG.info(
        "target: %s#%s (source=%s, cache=%s)",
        launch.target.state_ref.remote,
        launch.target.state_ref.branch,
        launch.target.source,
        launch.target.cache_dir,
    )
    try:
        yield {"launch": launch, "identity": ident, "target": launch.target, "principal": principal}
    finally:
        pass


def build_server(launch: LaunchConfig) -> "FastMCP":  # noqa: F821
    from mcp.server.fastmcp import FastMCP

    from .. import TOOL_VERSION
    from .resources import register_instructions
    from .tools import register_tools

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
    # would otherwise report the MCP SDK's own version to every client instead
    # of the tracker's. A cosmetic label must not break startup if the SDK moves it.
    try:
        server._mcp_server.version = TOOL_VERSION
    except AttributeError:  # pragma: no cover - depends on the installed SDK
        LOG.debug("MCP SDK does not expose a server version field; leaving it unset")
    register_instructions(server, principal)
    register_tools(server, launch.target, principal)
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
        startup_check(launch.target)
        LOG.info("startup state check passed (read-only, no bootstrap)")
        server = build_server(launch)
    except (TodoDBError, TodoError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        server.run("stdio")
    except (TodoDBError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
