"""Planning, findings, and admin tools behind --profile full (plan §5).

Exposes ``create_item``, ``update_item``, ``add_dependency``, ``defer``,
``promote_deferral``, ``dismiss_deferral``, ``block``, ``unblock``, ``drop``;
``finding_create`` (draft file only), ``finding_list``, ``finding_show``,
``finding_triage``, ``finding_link``, ``finding_promote``, ``finding_dismiss``;
``init_project`` and ``config_get``. The credentialed landing step
``finding_sync``, ``config_set``, ``sweep_stale``, ``migrate``, ``complete``,
verification execution, and ``rebaseline`` are never tools.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from ..errors import TodoDBError
from ..tracker import TodoTracker
from .dbpool import database_for_tool
from .envelope import err, ok
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


def register_full_tools(
    server: FastMCP,
    target: ResolvedTarget,
    holder: PrincipalHolder,
    session_id: str,
    allow_hosted: bool = False,
) -> None:
    def _target() -> ResolvedTarget:
        return target

    # ------------------------------------------------------------------ planning
    @server.tool(name="create_item", description="Create a new item. Supports work, scope, preserves, verifications.")
    async def create_item_tool(
        id: str,
        title: str,
        worktree: str,
        priority: str = "medium",
        description: str = "",
        work: list[dict[str, Any]] | None = None,
        scope: dict[str, list[str]] | None = None,
        preserves: list[str] | None = None,
        verifications: list[dict[str, str]] | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        principal = _principal(holder, ctx)

        def _work():
            with database_for_tool(_target(), "create_item", allow_hosted=allow_hosted) as db:
                tracker = TodoTracker(db, actor=principal or "tester")
                try:
                    tracker.create_item(
                        item_id=id,
                        title=title,
                        worktree=worktree,
                        priority=priority,
                        description=description,
                        work=work or [],
                        scope=scope or {},
                        preserves=preserves or [],
                        verifications=verifications or [],
                    )
                    return ok({"id": id, "status": "created"})
                except TodoDBError as exc:
                    return err(getattr(exc, "code", None) or "E_ERROR", str(exc))

        return await run_in_worker(_work)

    @server.tool(name="update_item", description="Amend an item without touching its lifecycle.")
    async def update_item_tool(
        id: str,
        title: str | None = None,
        description: str | None = None,
        priority: str | None = None,
        worktree: str | None = None,
        add_work: list[dict[str, Any]] | None = None,
        reason: str | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        principal = _principal(holder, ctx)

        def _work():
            with database_for_tool(_target(), "update_item", allow_hosted=allow_hosted) as db:
                tracker = TodoTracker(db, actor=principal or "tester")
                try:
                    tracker.update_item(
                        id,
                        title=title,
                        description=description,
                        priority=priority,
                        worktree=worktree,
                        add_work=add_work or [],
                        reason=reason,
                    )
                    return ok({"id": id, "status": "updated"})
                except TodoDBError as exc:
                    return err(getattr(exc, "code", None) or "E_ERROR", str(exc))

        return await run_in_worker(_work)

    @server.tool(name="add_dependency", description="Add an item dependency (needs).")
    async def add_dependency_tool(id: str, needs: str, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        principal = _principal(holder, ctx)

        def _work():
            with database_for_tool(_target(), "add_dependency", allow_hosted=allow_hosted) as db:
                tracker = TodoTracker(db, actor=principal or "tester")
                try:
                    tracker.update_item(id, add_deps=[needs], reason=f"add dependency {needs}")
                    return ok({"id": id, "needs": needs})
                except TodoDBError as exc:
                    return err(getattr(exc, "code", None) or "E_ERROR", str(exc))

        return await run_in_worker(_work)

    @server.tool(name="defer", description="Defer an item.")
    async def defer_tool(id: str, summary: str, reason: str | None = None, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        principal = _principal(holder, ctx)

        def _work():
            with database_for_tool(_target(), "defer", allow_hosted=allow_hosted) as db:
                tracker = TodoTracker(db, actor=principal or "tester")
                try:
                    deferral_id = tracker.defer(id, summary=summary, reason=reason or "deferred via MCP")
                    return ok({"id": id, "deferral_id": deferral_id})
                except TodoDBError as exc:
                    return err(getattr(exc, "code", None) or "E_ERROR", str(exc))

        return await run_in_worker(_work)

    @server.tool(name="promote_deferral", description="Promote a deferral to an item.")
    async def promote_deferral_tool(deferral_id: int, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        principal = _principal(holder, ctx)

        def _work():
            with database_for_tool(_target(), "promote_deferral", allow_hosted=allow_hosted) as db:
                tracker = TodoTracker(db, actor=principal or "tester")
                try:
                    new_id = tracker.promote_deferral(deferral_id)
                    return ok({"deferral_id": deferral_id, "new_item": new_id})
                except TodoDBError as exc:
                    return err(getattr(exc, "code", None) or "E_ERROR", str(exc))

        return await run_in_worker(_work)

    @server.tool(name="dismiss_deferral", description="Dismiss a deferral.")
    async def dismiss_deferral_tool(deferral_id: int, reason: str, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        principal = _principal(holder, ctx)

        def _work():
            with database_for_tool(_target(), "dismiss_deferral", allow_hosted=allow_hosted) as db:
                tracker = TodoTracker(db, actor=principal or "tester")
                try:
                    tracker.dismiss_deferral(deferral_id, reason=reason)
                    return ok({"deferral_id": deferral_id, "status": "dismissed"})
                except TodoDBError as exc:
                    return err(getattr(exc, "code", None) or "E_ERROR", str(exc))

        return await run_in_worker(_work)

    @server.tool(name="block", description="Block an item.")
    async def block_tool(id: str, reason: str, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        principal = _principal(holder, ctx)

        def _work():
            with database_for_tool(_target(), "block", allow_hosted=allow_hosted) as db:
                tracker = TodoTracker(db, actor=principal or "tester")
                try:
                    tracker.block(id, reason=reason)
                    return ok({"id": id, "blocked": True})
                except TodoDBError as exc:
                    return err(getattr(exc, "code", None) or "E_ERROR", str(exc))

        return await run_in_worker(_work)

    @server.tool(name="unblock", description="Unblock an item.")
    async def unblock_tool(id: str, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        principal = _principal(holder, ctx)

        def _work():
            with database_for_tool(_target(), "unblock", allow_hosted=allow_hosted) as db:
                tracker = TodoTracker(db, actor=principal or "tester")
                try:
                    tracker.unblock(id)
                    return ok({"id": id, "blocked": False})
                except TodoDBError as exc:
                    return err(getattr(exc, "code", None) or "E_ERROR", str(exc))

        return await run_in_worker(_work)

    @server.tool(name="drop", description="Drop an item.")
    async def drop_tool(id: str, reason: str | None = None, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        principal = _principal(holder, ctx)

        def _work():
            with database_for_tool(_target(), "drop", allow_hosted=allow_hosted) as db:
                tracker = TodoTracker(db, actor=principal or "tester")
                try:
                    tracker.drop(id, reason=reason or "dropped via MCP")
                    return ok({"id": id, "status": "dropped"})
                except TodoDBError as exc:
                    return err(getattr(exc, "code", None) or "E_ERROR", str(exc))

        return await run_in_worker(_work)

    # ------------------------------------------------------------------ findings
    @server.tool(name="finding_create", description="Create a finding draft file only, never the DB.")
    async def finding_create_tool(
        id: str,
        title: str,
        finding_text: str,
        why_matters: str,
        next_steps: str,
        disposition: str = "open",
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        # Drafts are file-only, so no DB needed; use the drafts dir.
        # Resolve drafts dir from target identity or env.
        drafts_dir = Path.home() / ".todo-db" / "finding-drafts"
        if _target().identity:
            drafts_dir = drafts_dir / _target().identity.project_id
        try:
            from ..findings import create_draft

            def _work():
                try:
                    path = create_draft(
                        drafts_dir=drafts_dir,
                        finding_id=id,
                        title=title,
                        finding_text=finding_text,
                        why_matters=why_matters,
                        next_steps=next_steps,
                        disposition=disposition,
                    )
                    return ok({"id": id, "draft": str(path)})
                except TodoDBError as exc:
                    return err(getattr(exc, "code", None) or "E_ERROR", str(exc))

            return await run_in_worker(_work)
        except ImportError as exc:
            return err("E_ERROR", str(exc))

    @server.tool(name="finding_list", description="List findings.")
    async def finding_list_tool(ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        principal = _principal(holder, ctx)

        def _work():
            with database_for_tool(_target(), "finding_list", allow_hosted=allow_hosted) as db:
                try:
                    from ..findings import FindingsTracker

                    ft = FindingsTracker(db, actor=principal or "tester")
                    findings = ft.list_findings() if hasattr(ft, "list_findings") else []
                    return ok({"findings": findings})
                except TodoDBError as exc:
                    return err(getattr(exc, "code", None) or "E_ERROR", str(exc))
                except Exception as exc:
                    return err("E_ERROR", str(exc))

        return await run_in_worker(_work)

    @server.tool(name="finding_show", description="Show one finding.")
    async def finding_show_tool(id: str, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        principal = _principal(holder, ctx)

        def _work():
            with database_for_tool(_target(), "finding_show", allow_hosted=allow_hosted) as db:
                try:
                    from ..findings import FindingsTracker

                    ft = FindingsTracker(db, actor=principal or "tester")
                    finding = ft.get_finding(id) if hasattr(ft, "get_finding") else None
                    if finding is None:
                        return err("E_NOT_FOUND", f"finding {id!r} not found")
                    return ok(finding)
                except TodoDBError as exc:
                    return err(getattr(exc, "code", None) or "E_ERROR", str(exc))

        return await run_in_worker(_work)

    @server.tool(name="finding_triage", description="Triage a finding.")
    async def finding_triage_tool(
        id: str,
        urgency: str | None = None,
        disposition: str | None = None,
        reason: str | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        principal = _principal(holder, ctx)

        def _work():
            with database_for_tool(_target(), "finding_triage", allow_hosted=allow_hosted) as db:
                try:
                    from ..findings import FindingsTracker

                    ft = FindingsTracker(db, actor=principal or "tester")
                    ft.triage(id, urgency=urgency, disposition=disposition, reason=reason)
                    return ok({"id": id, "status": "triaged"})
                except TodoDBError as exc:
                    return err(getattr(exc, "code", None) or "E_ERROR", str(exc))

        return await run_in_worker(_work)

    @server.tool(name="finding_link", description="Link a finding.")
    async def finding_link_tool(id: str, kind: str, target: str | None = None, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        principal = _principal(holder, ctx)

        def _work():
            with database_for_tool(_target(), "finding_link", allow_hosted=allow_hosted) as db:
                try:
                    from ..findings import FindingsTracker

                    ft = FindingsTracker(db, actor=principal or "tester")
                    ft.link(id, kind=kind, target_item=target)
                    return ok({"id": id, "linked": kind})
                except TodoDBError as exc:
                    return err(getattr(exc, "code", None) or "E_ERROR", str(exc))

        return await run_in_worker(_work)

    @server.tool(name="finding_promote", description="Promote a finding to an item.")
    async def finding_promote_tool(id: str, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        principal = _principal(holder, ctx)

        def _work():
            with database_for_tool(_target(), "finding_promote", allow_hosted=allow_hosted) as db:
                try:
                    from ..findings import FindingsTracker

                    ft = FindingsTracker(db, actor=principal or "tester")
                    new_id = ft.promote(id) if hasattr(ft, "promote") else None
                    return ok({"id": id, "new_item": new_id})
                except TodoDBError as exc:
                    return err(getattr(exc, "code", None) or "E_ERROR", str(exc))

        return await run_in_worker(_work)

    @server.tool(name="finding_dismiss", description="Dismiss a finding.")
    async def finding_dismiss_tool(id: str, reason: str, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        principal = _principal(holder, ctx)

        def _work():
            with database_for_tool(_target(), "finding_dismiss", allow_hosted=allow_hosted) as db:
                try:
                    from ..findings import FindingsTracker

                    ft = FindingsTracker(db, actor=principal or "tester")
                    if hasattr(ft, "dismiss"):
                        ft.dismiss(id, reason=reason)
                    else:
                        ft.triage(id, disposition="dismissed", reason=reason)
                    return ok({"id": id, "status": "dismissed"})
                except TodoDBError as exc:
                    return err(getattr(exc, "code", None) or "E_ERROR", str(exc))

        return await run_in_worker(_work)

    # ------------------------------------------------------------------ admin
    @server.tool(name="init_project", description="Bootstrap a .todo-db/config.json for the project.")
    async def init_project_tool(project_id: str, repository: str, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        def _work():
            try:
                # Use the repo_root as the project root
                root = _target().repo_root
                # Call the CLI's init-project via direct tracker? Simplify: create config file directly
                cfg_path = root / ".todo-db" / "config.json"
                cfg_path.parent.mkdir(parents=True, exist_ok=True)
                import json

                cfg_path.write_text(
                    json.dumps({"project_id": project_id, "repository": repository, "db": ".todo-db/standalone.sqlite"})
                )
                return ok({"project_id": project_id, "repository": repository, "config": str(cfg_path)})
            except TodoDBError as exc:
                return err(getattr(exc, "code", None) or "E_ERROR", str(exc))
            except Exception as exc:
                return err("E_ERROR", str(exc))

        return await run_in_worker(_work)

    @server.tool(name="config_get", description="Read a tracker config key.")
    async def config_get_tool(key: str, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        principal = _principal(holder, ctx)

        def _work():
            with database_for_tool(_target(), "config_get", allow_hosted=allow_hosted) as db:
                tracker = TodoTracker(db, actor=principal or "tester")
                try:
                    value = tracker.get_config(key)
                    return ok({"key": key, "value": value})
                except TodoDBError as exc:
                    return err(getattr(exc, "code", None) or "E_ERROR", str(exc))

        return await run_in_worker(_work)
