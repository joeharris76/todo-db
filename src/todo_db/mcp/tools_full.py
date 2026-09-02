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

import json
import logging
import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from ..database import TodoDatabase
from ..errors import TodoDBError
from ..findings import default_drafts_dir
from ..models import CredentialMode, DatabaseConfig, ProjectIdentity
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
        if not principal:
            return err("E_NO_PRINCIPAL", "principal not yet resolved; call get_instructions first", kind="error")

        def _work():
            with database_for_tool(_target(), "create_item", allow_hosted=allow_hosted) as db:
                tracker = TodoTracker(db, actor=principal)
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
        approach: str | None = None,
        category: str | None = None,
        add_work: list[dict[str, Any]] | None = None,
        edit_work: dict[str, str] | None = None,
        add_verify: list[dict[str, str]] | None = None,
        drop_verify: list[int] | None = None,
        add_scope: list[list[str]] | None = None,
        drop_scope: list[list[str]] | None = None,
        add_deps: list[str] | None = None,
        drop_deps: list[str] | None = None,
        add_preserves: list[str] | None = None,
        drop_preserves: list[str] | None = None,
        add_anti_patterns: list[dict[str, str]] | None = None,
        drop_anti_patterns: list[str] | None = None,
        add_prior_art: list[dict[str, str]] | None = None,
        drop_prior_art: list[list[str]] | None = None,
        add_work_needs: list[list[str]] | None = None,
        drop_work_needs: list[list[str]] | None = None,
        reason: str | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        principal = _principal(holder, ctx)
        if not principal:
            return err("E_NO_PRINCIPAL", "principal not yet resolved; call get_instructions first", kind="error")

        def _work():
            with database_for_tool(_target(), "update_item", allow_hosted=allow_hosted) as db:
                tracker = TodoTracker(db, actor=principal)
                try:
                    tracker.update_item(
                        id,
                        title=title,
                        description=description,
                        priority=priority,
                        worktree=worktree,
                        approach=approach,
                        category=category,
                        add_work=add_work or [],
                        edit_work=edit_work,
                        add_verify=add_verify or [],
                        drop_verify=drop_verify or [],
                        add_scope=[tuple(x) for x in (add_scope or [])],  # type: ignore[misc]
                        drop_scope=[tuple(x) for x in (drop_scope or [])],  # type: ignore[misc]
                        add_deps=add_deps or [],
                        drop_deps=drop_deps or [],
                        add_preserves=add_preserves or [],
                        drop_preserves=drop_preserves or [],
                        add_anti_patterns=add_anti_patterns or [],
                        drop_anti_patterns=drop_anti_patterns or [],
                        add_prior_art=add_prior_art or [],
                        drop_prior_art=[tuple(x) for x in (drop_prior_art or [])],  # type: ignore[misc]
                        add_work_needs=[tuple(x) for x in (add_work_needs or [])],  # type: ignore[misc]
                        drop_work_needs=[tuple(x) for x in (drop_work_needs or [])],  # type: ignore[misc]
                        reason=reason,
                    )
                    return ok({"id": id, "status": "updated"})
                except TodoDBError as exc:
                    return err(getattr(exc, "code", None) or "E_ERROR", str(exc))

        return await run_in_worker(_work)

    @server.tool(name="add_dependency", description="Add an item dependency (needs).")
    async def add_dependency_tool(id: str, needs: str, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        principal = _principal(holder, ctx)
        if not principal:
            return err("E_NO_PRINCIPAL", "principal not yet resolved; call get_instructions first", kind="error")

        def _work():
            with database_for_tool(_target(), "add_dependency", allow_hosted=allow_hosted) as db:
                tracker = TodoTracker(db, actor=principal)
                try:
                    tracker.update_item(id, add_deps=[needs], reason=f"add dependency {needs}")
                    return ok({"id": id, "needs": needs})
                except TodoDBError as exc:
                    return err(getattr(exc, "code", None) or "E_ERROR", str(exc))

        return await run_in_worker(_work)

    @server.tool(name="defer", description="Defer an item.")
    async def defer_tool(id: str, summary: str, reason: str | None = None, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        principal = _principal(holder, ctx)
        if not principal:
            return err("E_NO_PRINCIPAL", "principal not yet resolved; call get_instructions first", kind="error")

        def _work():
            with database_for_tool(_target(), "defer", allow_hosted=allow_hosted) as db:
                tracker = TodoTracker(db, actor=principal)
                try:
                    deferral_id = tracker.defer(id, summary=summary, reason=reason or "deferred via MCP")
                    return ok({"id": id, "deferral_id": deferral_id})
                except TodoDBError as exc:
                    return err(getattr(exc, "code", None) or "E_ERROR", str(exc))

        return await run_in_worker(_work)

    @server.tool(name="promote_deferral", description="Promote a deferral to an item.")
    async def promote_deferral_tool(deferral_id: int, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        principal = _principal(holder, ctx)
        if not principal:
            return err("E_NO_PRINCIPAL", "principal not yet resolved; call get_instructions first", kind="error")

        def _work():
            with database_for_tool(_target(), "promote_deferral", allow_hosted=allow_hosted) as db:
                tracker = TodoTracker(db, actor=principal)
                try:
                    new_id = tracker.promote_deferral(deferral_id)
                    return ok({"deferral_id": deferral_id, "new_item": new_id})
                except TodoDBError as exc:
                    return err(getattr(exc, "code", None) or "E_ERROR", str(exc))

        return await run_in_worker(_work)

    @server.tool(name="dismiss_deferral", description="Dismiss a deferral.")
    async def dismiss_deferral_tool(deferral_id: int, reason: str, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        principal = _principal(holder, ctx)
        if not principal:
            return err("E_NO_PRINCIPAL", "principal not yet resolved; call get_instructions first", kind="error")

        def _work():
            with database_for_tool(_target(), "dismiss_deferral", allow_hosted=allow_hosted) as db:
                tracker = TodoTracker(db, actor=principal)
                try:
                    tracker.dismiss_deferral(deferral_id, reason=reason)
                    return ok({"deferral_id": deferral_id, "status": "dismissed"})
                except TodoDBError as exc:
                    return err(getattr(exc, "code", None) or "E_ERROR", str(exc))

        return await run_in_worker(_work)

    @server.tool(name="block", description="Block an item.")
    async def block_tool(id: str, reason: str, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        principal = _principal(holder, ctx)
        if not principal:
            return err("E_NO_PRINCIPAL", "principal not yet resolved; call get_instructions first", kind="error")

        def _work():
            with database_for_tool(_target(), "block", allow_hosted=allow_hosted) as db:
                tracker = TodoTracker(db, actor=principal)
                try:
                    tracker.block(id, reason=reason)
                    return ok({"id": id, "blocked": True})
                except TodoDBError as exc:
                    return err(getattr(exc, "code", None) or "E_ERROR", str(exc))

        return await run_in_worker(_work)

    @server.tool(name="unblock", description="Unblock an item.")
    async def unblock_tool(id: str, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        principal = _principal(holder, ctx)
        if not principal:
            return err("E_NO_PRINCIPAL", "principal not yet resolved; call get_instructions first", kind="error")

        def _work():
            with database_for_tool(_target(), "unblock", allow_hosted=allow_hosted) as db:
                tracker = TodoTracker(db, actor=principal)
                try:
                    tracker.unblock(id)
                    return ok({"id": id, "blocked": False})
                except TodoDBError as exc:
                    return err(getattr(exc, "code", None) or "E_ERROR", str(exc))

        return await run_in_worker(_work)

    @server.tool(name="drop", description="Drop an item.")
    async def drop_tool(id: str, reason: str | None = None, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        principal = _principal(holder, ctx)
        if not principal:
            return err("E_NO_PRINCIPAL", "principal not yet resolved; call get_instructions first", kind="error")

        def _work():
            with database_for_tool(_target(), "drop", allow_hosted=allow_hosted) as db:
                tracker = TodoTracker(db, actor=principal)
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
        try:
            from ..findings import create_draft
        except ImportError as exc:
            return err("E_ERROR", str(exc))

        # Resolve drafts dir via default_drafts_dir; error if identity missing and no env override
        project_id = _target().identity.project_id if _target().identity else None
        if project_id is None and not os.environ.get("TODO_DB_FINDING_DRAFTS_DIR"):
            return err("E_NO_PROJECT", "project identity not resolved; drafts dir is project-scoped", kind="error")
        try:
            drafts_dir = default_drafts_dir(project_id or "")
        except Exception as exc:
            return err(getattr(exc, "code", None) or "E_IDENTITY", str(exc))

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

    @server.tool(name="finding_list", description="List findings.")
    async def finding_list_tool(ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        principal = _principal(holder, ctx)
        if not principal:
            return err("E_NO_PRINCIPAL", "principal not yet resolved; call get_instructions first", kind="error")

        def _work():
            with database_for_tool(_target(), "finding_list", allow_hosted=allow_hosted) as db:
                try:
                    from ..findings import FindingsTracker

                    ft = FindingsTracker(db, actor=principal)
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
        if not principal:
            return err("E_NO_PRINCIPAL", "principal not yet resolved; call get_instructions first", kind="error")

        def _work():
            with database_for_tool(_target(), "finding_show", allow_hosted=allow_hosted) as db:
                try:
                    from ..findings import FindingsTracker

                    ft = FindingsTracker(db, actor=principal)
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
        if not principal:
            return err("E_NO_PRINCIPAL", "principal not yet resolved; call get_instructions first", kind="error")

        def _work():
            with database_for_tool(_target(), "finding_triage", allow_hosted=allow_hosted) as db:
                try:
                    from ..findings import FindingsTracker

                    ft = FindingsTracker(db, actor=principal)
                    ft.triage(id, urgency=urgency, disposition=disposition, reason=reason)
                    return ok({"id": id, "status": "triaged"})
                except TodoDBError as exc:
                    return err(getattr(exc, "code", None) or "E_ERROR", str(exc))

        return await run_in_worker(_work)

    @server.tool(name="finding_link", description="Link a finding.")
    async def finding_link_tool(id: str, kind: str, target: str | None = None, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        principal = _principal(holder, ctx)
        if not principal:
            return err("E_NO_PRINCIPAL", "principal not yet resolved; call get_instructions first", kind="error")

        def _work():
            with database_for_tool(_target(), "finding_link", allow_hosted=allow_hosted) as db:
                try:
                    from ..findings import FindingsTracker

                    ft = FindingsTracker(db, actor=principal)
                    ft.link(id, kind=kind, target_item=target)
                    return ok({"id": id, "linked": kind})
                except TodoDBError as exc:
                    return err(getattr(exc, "code", None) or "E_ERROR", str(exc))

        return await run_in_worker(_work)

    @server.tool(name="finding_promote", description="Promote a finding to an item.")
    async def finding_promote_tool(id: str, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        principal = _principal(holder, ctx)
        if not principal:
            return err("E_NO_PRINCIPAL", "principal not yet resolved; call get_instructions first", kind="error")

        def _work():
            with database_for_tool(_target(), "finding_promote", allow_hosted=allow_hosted) as db:
                try:
                    from ..findings import FindingsTracker

                    ft = FindingsTracker(db, actor=principal)
                    new_id = ft.promote(id) if hasattr(ft, "promote") else None
                    return ok({"id": id, "new_item": new_id})
                except TodoDBError as exc:
                    return err(getattr(exc, "code", None) or "E_ERROR", str(exc))

        return await run_in_worker(_work)

    @server.tool(name="finding_dismiss", description="Dismiss a finding.")
    async def finding_dismiss_tool(id: str, reason: str, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        principal = _principal(holder, ctx)
        if not principal:
            return err("E_NO_PRINCIPAL", "principal not yet resolved; call get_instructions first", kind="error")

        def _work():
            with database_for_tool(_target(), "finding_dismiss", allow_hosted=allow_hosted) as db:
                try:
                    from ..findings import FindingsTracker

                    ft = FindingsTracker(db, actor=principal)
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
    async def init_project_tool(project_id: str, repository: str, force: bool = False, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        def _work():
            try:
                from ..cli import CONFIG_DIRNAME, CONFIG_FILENAME, DEFAULT_DB_RELATIVE, SCAFFOLD_GITIGNORE

                root = _target().repo_root
                config_dir = root / CONFIG_DIRNAME
                config_path = config_dir / CONFIG_FILENAME
                gitignore_path = config_dir / ".gitignore"
                collisions = [path for path in (config_path,) if path.exists()]
                if gitignore_path.exists() and gitignore_path.read_text(encoding="utf-8") != SCAFFOLD_GITIGNORE:
                    collisions.append(gitignore_path)
                if collisions and not force:
                    return err(
                        "E_EXISTS",
                        "refusing to overwrite existing scaffolding: "
                        + ", ".join(str(p) for p in sorted(set(collisions)))
                        + "; pass force=true to overwrite",
                    )
                identity = ProjectIdentity(project_id=project_id, repository=repository)
                db_value = DEFAULT_DB_RELATIVE
                if "://" in db_value or Path(db_value).is_absolute():
                    db_target = db_value
                else:
                    db_target = str(root / db_value)
                database_config = DatabaseConfig(
                    path=db_target,
                    identity=identity,
                    credential_mode=CredentialMode.READ_WRITE,
                )
                with TodoDatabase.open(database_config):
                    pass
                config_dir.mkdir(parents=True, exist_ok=True)
                payload = {"project_id": identity.project_id, "repository": identity.repository, "db": db_value}
                config_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                gitignore_path.write_text(SCAFFOLD_GITIGNORE, encoding="utf-8")
                return ok({"project_id": project_id, "repository": repository, "config": str(config_path)})
            except TodoDBError as exc:
                return err(getattr(exc, "code", None) or "E_ERROR", str(exc))
            except Exception as exc:
                return err("E_ERROR", str(exc))

        return await run_in_worker(_work)

    @server.tool(name="config_get", description="Read a tracker config key.")
    async def config_get_tool(key: str, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        principal = _principal(holder, ctx)
        if not principal:
            return err("E_NO_PRINCIPAL", "principal not yet resolved; call get_instructions first", kind="error")

        def _work():
            with database_for_tool(_target(), "config_get", allow_hosted=allow_hosted) as db:
                tracker = TodoTracker(db, actor=principal)
                try:
                    value = tracker.get_config(key)
                    return ok({"key": key, "value": value})
                except TodoDBError as exc:
                    return err(getattr(exc, "code", None) or "E_ERROR", str(exc))

        return await run_in_worker(_work)
