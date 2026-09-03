"""Read-only query tools in the default profile (plan §5).

Implements ``list_items``, ``show_item``, ``ready``, ``stats``, ``deps``,
``deferrals``, ``export``, ``check_scope``, ``verify_list``, ``lint``,
``start_unit``. All open a READ_ONLY connection, never a read-write
credential. ``list_items``/``ready``/``show_item`` carry ``fields``/``limit``
and use the drop-trailing-items paging fallback before ``E_OUTPUT_TRUNCATED``.
``verify_list`` lists stored verifications and never executes them.
``stats`` is the one that needs an error code for the unresolved-identity case
(``E_IDENTITY``).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from ..agent import GitScopeEngine
from ..errors import E_NO_PRINCIPAL, ProjectIdentityMismatchError, TodoDBError
from ..tracker import TodoTracker
from .dbpool import database_for_tool
from .envelope import MAX_BYTES, err, ok, paged
from .identity import PrincipalHolder
from .target import ResolvedTarget
from .worker import run_in_worker

LOG = logging.getLogger("todo_db.mcp")


def _principal(holder: PrincipalHolder, ctx: Context | None) -> str | None:
    if holder.principal is not None:
        return holder.principal
    try:
        ci = ctx.request_context.session.client_params.clientInfo if ctx else None  # type: ignore[union-attr]
    except AttributeError:
        ci = None
    if ci is not None:
        return holder.ensure(ci)
    return holder.principal


def register_query_tools(
    server: FastMCP,
    target: ResolvedTarget,
    holder: PrincipalHolder,
    session_id: str,
    allow_hosted: bool = False,
) -> None:
    def _target() -> ResolvedTarget:
        return target

    @server.tool(
        name="list_items",
        description="List items with optional filters. Supports fields projection and limit with paging fallback.",
    )
    async def list_items_tool(
        fields: list[str] | None = None,
        limit: int | None = None,
        cursor: int = 0,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        principal = _principal(holder, ctx)
        if not principal:
            return err(E_NO_PRINCIPAL, "principal not yet resolved; call get_instructions first", kind="error")

        def _work():
            with database_for_tool(_target(), "list_items", allow_hosted=allow_hosted) as db:
                tracker = TodoTracker(db, actor=principal)
                try:
                    items = tracker.list_items()
                except TodoDBError as exc:
                    return err(getattr(exc, "code", None) or "E_ERROR", str(exc))
                total = len(items)
                # Apply cursor/limit and fields if provided
                page = items[cursor : cursor + limit] if limit is not None else items[cursor:]
                if fields:
                    keep = {f.strip() for f in fields if f.strip()}
                    page = [{k: v for k, v in it.items() if k in keep} for it in page]
                # Paging fallback for oversized list
                if limit is not None:
                    return paged(page, total, limit=limit, cursor=cursor)
                # Check cap for non-paged
                env = ok({"items": page, "total": total})
                if len(json.dumps(env, separators=(",", ":"), sort_keys=True).encode()) > MAX_BYTES:
                    return paged(page, total, limit=len(page) or 1, cursor=cursor)
                return env

        return await run_in_worker(_work)

    @server.tool(name="show_item", description="Show one item. Supports fields projection.")
    async def show_item_tool(
        id: str,
        fields: list[str] | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        principal = _principal(holder, ctx)
        if not principal:
            return err(E_NO_PRINCIPAL, "principal not yet resolved; call get_instructions first", kind="error")

        def _work():
            with database_for_tool(_target(), "show_item", allow_hosted=allow_hosted) as db:
                tracker = TodoTracker(db, actor=principal)
                try:
                    item = tracker.get_item(id)
                except TodoDBError as exc:
                    return err(getattr(exc, "code", None) or "E_ERROR", str(exc))
                if fields:
                    keep = {f.strip() for f in fields if f.strip()}
                    item = {k: v for k, v in item.items() if k in keep}
                return ok(item)

        return await run_in_worker(_work)

    @server.tool(
        name="ready", description="List ready items (no unmet dependencies, not blocked). Supports fields and limit."
    )
    async def ready_tool(
        fields: list[str] | None = None,
        limit: int | None = None,
        cursor: int = 0,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        principal = _principal(holder, ctx)
        if not principal:
            return err(E_NO_PRINCIPAL, "principal not yet resolved; call get_instructions first", kind="error")

        def _work():
            with database_for_tool(_target(), "ready", allow_hosted=allow_hosted) as db:
                tracker = TodoTracker(db, actor=principal)
                try:
                    items = tracker.ready_items()
                except TodoDBError as exc:
                    return err(getattr(exc, "code", None) or "E_ERROR", str(exc))
                total = len(items)
                page = items[cursor : cursor + limit] if limit is not None else items[cursor:]
                if fields:
                    keep = {f.strip() for f in fields if f.strip()}
                    page = [{k: v for k, v in it.items() if k in keep} for it in page]
                if limit is not None:
                    return paged(page, total, limit=limit, cursor=cursor)
                env = ok({"items": page, "total": total})
                if len(json.dumps(env, separators=(",", ":"), sort_keys=True).encode()) > MAX_BYTES:
                    return paged(page, total, limit=len(page) or 1, cursor=cursor)
                return env

        return await run_in_worker(_work)

    @server.tool(
        name="stats",
        description="Tracker stats. Returns a coded error when project identity is unresolved for the drafts-dir path.",
    )
    async def stats_tool(ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        principal = _principal(holder, ctx)
        if not principal:
            return err(E_NO_PRINCIPAL, "principal not yet resolved; call get_instructions first", kind="error")

        def _work():
            with database_for_tool(_target(), "stats", allow_hosted=allow_hosted) as db:
                tracker = TodoTracker(db, actor=principal)
                try:
                    # stats is the tool that surfaces the unresolved-identity
                    # case: findings are project-scoped via the drafts-dir
                    # path, so without a resolved identity raise
                    # ProjectIdentityMismatchError (E_IDENTITY), never silently
                    # return data as if identity did not matter.
                    if _target().identity is None:
                        raise ProjectIdentityMismatchError(
                            "project identity is unresolved; stats needs it for the "
                            "project-scoped findings drafts-dir path"
                        )
                    data = tracker.stats()
                    # Also include findings stats against the real
                    # FindingsTracker API. Identity errors must propagate to
                    # the E_IDENTITY mapping below, never collapse into a
                    # silent data no-op; only non-identity findings failures
                    # (e.g. a store without findings tables) fall back to
                    # tracker-only stats.
                    try:
                        from ..findings import FindingsTracker

                        data.update(FindingsTracker(db, actor=principal).stats())
                    except TodoDBError:
                        # Includes ProjectIdentityMismatchError (E_IDENTITY):
                        # identity failures surface as coded errors below.
                        raise
                    except Exception:
                        pass
                    return ok(data)
                except TodoDBError as exc:
                    code = getattr(exc, "code", None) or "E_IDENTITY"
                    return err(code, str(exc))

        return await run_in_worker(_work)

    @server.tool(name="deps", description="Show item dependencies.")
    async def deps_tool(id: str, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        principal = _principal(holder, ctx)
        if not principal:
            return err(E_NO_PRINCIPAL, "principal not yet resolved; call get_instructions first", kind="error")

        def _work():
            with database_for_tool(_target(), "deps", allow_hosted=allow_hosted) as db:
                tracker = TodoTracker(db, actor=principal)
                try:
                    item = tracker.get_item(id)
                    deps = item.get("deps", [])
                    return ok({"id": id, "deps": deps})
                except TodoDBError as exc:
                    return err(getattr(exc, "code", None) or "E_ERROR", str(exc))

        return await run_in_worker(_work)

    @server.tool(name="deferrals", description="List open deferrals.")
    async def deferrals_tool(ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        principal = _principal(holder, ctx)
        if not principal:
            return err(E_NO_PRINCIPAL, "principal not yet resolved; call get_instructions first", kind="error")

        def _work():
            with database_for_tool(_target(), "deferrals", allow_hosted=allow_hosted) as db:
                try:
                    rows = db.connection.execute(
                        "SELECT * FROM deferrals WHERE resolution = 'open' ORDER BY id"
                    ).fetchall()
                    deferrals = [dict(r) for r in rows]
                    return ok({"deferrals": deferrals})
                except TodoDBError as exc:
                    return err(getattr(exc, "code", None) or "E_ERROR", str(exc))

        return await run_in_worker(_work)

    @server.tool(name="export", description="Export all items and verifications for backup/audit.")
    async def export_tool(ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        principal = _principal(holder, ctx)
        if not principal:
            return err(E_NO_PRINCIPAL, "principal not yet resolved; call get_instructions first", kind="error")

        def _work():
            with database_for_tool(_target(), "export", allow_hosted=allow_hosted) as db:
                tracker = TodoTracker(db, actor=principal)
                try:
                    data = tracker.export_all()
                    return ok({"items": data})
                except TodoDBError as exc:
                    return err(getattr(exc, "code", None) or "E_ERROR", str(exc))

        return await run_in_worker(_work)

    @server.tool(
        name="check_scope", description="Inspect changed files vs scope — read-only. Provide a list of files to check."
    )
    async def check_scope_tool(id: str, files: list[str] | None = None, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        principal = _principal(holder, ctx)
        if not principal:
            return err(E_NO_PRINCIPAL, "principal not yet resolved; call get_instructions first", kind="error")

        def _work():
            with database_for_tool(_target(), "check_scope", allow_hosted=allow_hosted) as db:
                tracker = TodoTracker(db, actor=principal)
                try:
                    if files is None:
                        # Use the pinned git engine to compute changed files
                        eng = GitScopeEngine(_target().repo_root)
                        changed = eng.changed_files(base=tracker.get_item(id).get("git_baseline"))
                    else:
                        changed = files
                    violations = tracker.check_scope(id, changed)
                    return ok({"id": id, "violations": violations, "changed_files": changed})
                except TodoDBError as exc:
                    return err(getattr(exc, "code", None) or "E_ERROR", str(exc))

        return await run_in_worker(_work)

    @server.tool(name="verify_list", description="List stored verifications, never run them.")
    async def verify_list_tool(id: str, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        principal = _principal(holder, ctx)
        if not principal:
            return err(E_NO_PRINCIPAL, "principal not yet resolved; call get_instructions first", kind="error")

        def _work():
            with database_for_tool(_target(), "verify_list", allow_hosted=allow_hosted) as db:
                tracker = TodoTracker(db, actor=principal)
                try:
                    item = tracker.get_item(id)
                    verifications = item.get("verifications", [])
                    # Return only the stored definition, not last_run/result
                    lst = [
                        {
                            "seq": v["seq"],
                            "description": v["description"],
                            "command": v.get("command"),
                            "expected": v.get("expected"),
                        }
                        for v in verifications
                    ]
                    return ok({"id": id, "verifications": lst})
                except TodoDBError as exc:
                    return err(getattr(exc, "code", None) or "E_ERROR", str(exc))

        return await run_in_worker(_work)

    @server.tool(name="lint", description="Planning-quality check, read-only.")
    async def lint_tool(id: str, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        principal = _principal(holder, ctx)
        if not principal:
            return err(E_NO_PRINCIPAL, "principal not yet resolved; call get_instructions first", kind="error")

        def _work():
            with database_for_tool(_target(), "lint", allow_hosted=allow_hosted) as db:
                tracker = TodoTracker(db, actor=principal)
                try:
                    issues = tracker.lint(id)
                    return ok({"id": id, "issues": issues})
                except TodoDBError as exc:
                    return err(getattr(exc, "code", None) or "E_ERROR", str(exc))

        return await run_in_worker(_work)

    @server.tool(name="start_unit", description="No-shell state change — marks a unit in progress.")
    async def start_unit_tool(id: str, wid: str, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        principal = _principal(holder, ctx)
        if not principal:
            return err(E_NO_PRINCIPAL, "principal not yet resolved; call get_instructions first", kind="error")

        def _work():
            with database_for_tool(_target(), "start_unit", allow_hosted=allow_hosted) as db:
                tracker = TodoTracker(db, actor=principal)
                try:
                    tracker.start_unit(id, wid)
                    return ok({"id": id, "wid": wid, "status": "started"})
                except TodoDBError as exc:
                    return err(getattr(exc, "code", None) or "E_ERROR", str(exc))

        return await run_in_worker(_work)
