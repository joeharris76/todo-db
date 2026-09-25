"""The small agent surface over shared service operations.

``list_items`` / ``show_item`` / ``create_item`` / ``register_batch`` /
``update_item`` / ``take`` / ``prepare`` / ``bind_batch_pr`` /
``abort_batch`` / ``release`` / ``finish`` / ``renew`` are backed by
:mod:`todo_db.service`, the same operations the human/CI CLI uses.
Blocking Git work runs via ``asyncio.to_thread``; there is no shared
mutable connection to guard.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from ..errors import E_NO_PRINCIPAL
from ..service import TrackerService, err
from .identity import PrincipalHolder, sanitize_client_name
from .target import ResolvedTarget

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


def _client_name(ctx: Context | None) -> str | None:
    """Best-effort harness label for session-history attribution."""
    try:
        ci = ctx.request_context.session.client_params.clientInfo if ctx else None  # type: ignore[union-attr]
    except AttributeError:
        ci = None
    return sanitize_client_name(getattr(ci, "name", None))


def _service(
    target: ResolvedTarget, worker: str, holder: PrincipalHolder, ctx: Context | None,
) -> TrackerService:
    return TrackerService(
        ref=target.state_ref,
        cache_dir=target.cache_dir,
        worker=worker,
        session_id=holder.session_id,
        client_name=_client_name(ctx),
    )


def _need_principal(holder: PrincipalHolder, ctx: Context | None) -> str | dict[str, Any] | None:
    worker = _principal(holder, ctx)
    if not worker:
        return err(E_NO_PRINCIPAL, "principal not yet resolved; call get_instructions first", kind="error")
    return worker


def register_tools(server: FastMCP, target: ResolvedTarget, holder: PrincipalHolder) -> None:
    @server.tool(
        name="list_items",
        description="List task summaries with filters and stable pagination.",
    )
    async def list_items_tool(
        status: str | None = None,
        priority: str | None = None,
        text: str | None = None,
        ready_only: bool = False,
        limit: int = 5,
        cursor: str | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        worker = _need_principal(holder, ctx)
        if not isinstance(worker, str):
            return worker
        svc = _service(target, worker, holder, ctx)
        return await asyncio.to_thread(
            svc.list_items, status=status, priority=priority, text=text,
            ready_only=ready_only, limit=limit, cursor=cursor,
        )

    @server.tool(
        name="show_item",
        description="Show a task or one paged large field.",
    )
    async def show_item_tool(
        id: str,
        field: str | None = None,
        offset: int = 0,
        budget: int = 6000,
        rev: str | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        worker = _need_principal(holder, ctx)
        if not isinstance(worker, str):
            return worker
        svc = _service(target, worker, holder, ctx)
        return await asyncio.to_thread(svc.show_item, id, field=field, offset=offset, budget=budget, rev=rev)

    @server.tool(
        name="create_item",
        description="Create a task, optionally as a registered batch member.",
    )
    async def create_item_tool(
        id: str,
        title: str,
        priority: str = "medium",
        description: str = "",
        needs: list[str] | None = None,
        acceptance: list[str] | None = None,
        links: list[str] | None = None,
        context: str = "",
        batch: dict[str, Any] | None = None,
        not_before: str = "",
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        worker = _need_principal(holder, ctx)
        if not isinstance(worker, str):
            return worker
        svc = _service(target, worker, holder, ctx)
        return await asyncio.to_thread(
            svc.create_item, id, title, priority=priority, description=description,
            needs=needs or [], acceptance=acceptance or [], links=links or [], context=context, batch=batch,
            not_before=not_before,
        )

    @server.tool(
        name="register_batch",
        description="Register an immutable prepared-work batch after confirmed schema-3 cutover.",
    )
    async def register_batch_tool(
        batch_id: str,
        project_id: str,
        repository: str,
        owner_generation: str,
        integration_branch: str,
        integration_worktree: str,
        start_head: str,
        members: list[str],
        scope: dict[str, list[str]],
        scope_hash: str,
        delivery_boundary: str,
        terminal_outcome: str,
        confirm_schema3_cutover: bool = False,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        worker = _need_principal(holder, ctx)
        if not isinstance(worker, str):
            return worker
        svc = _service(target, worker, holder, ctx)
        return await asyncio.to_thread(
            svc.register_batch,
            batch_id=batch_id, project_id=project_id, repository=repository,
            owner_generation=owner_generation, integration_branch=integration_branch,
            integration_worktree=integration_worktree, start_head=start_head,
            members=members, scope=scope, scope_hash=scope_hash,
            delivery_boundary=delivery_boundary, terminal_outcome=terminal_outcome,
            confirm_schema3_cutover=confirm_schema3_cutover,
        )

    @server.tool(
        name="update_item",
        description="Edit task fields or move between open and blocked.",
    )
    async def update_item_tool(
        id: str,
        title: str | None = None,
        priority: str | None = None,
        description: str | None = None,
        needs: list[str] | None = None,
        acceptance: list[str] | None = None,
        links: list[str] | None = None,
        context: str | None = None,
        batch: dict[str, Any] | None = None,
        status: str | None = None,
        not_before: str | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        worker = _need_principal(holder, ctx)
        if not isinstance(worker, str):
            return worker
        svc = _service(target, worker, holder, ctx)
        kwargs: dict[str, Any] = {}
        if title is not None:
            kwargs["title"] = title
        if priority is not None:
            kwargs["priority"] = priority
        if description is not None:
            kwargs["description"] = description
        if needs is not None:
            kwargs["needs"] = needs
        if acceptance is not None:
            kwargs["acceptance"] = acceptance
        if links is not None:
            kwargs["links"] = links
        if context is not None:
            kwargs["context"] = context
        if batch is not None:
            kwargs["batch"] = batch
        if status is not None:
            kwargs["status"] = status
        if not_before is not None:
            kwargs["not_before"] = not_before
        return await asyncio.to_thread(svc.update_item, id, **kwargs)

    @server.tool(
        name="take",
        description="Claim one ready task and return its generation and context.",
    )
    async def take_tool(
        id: str,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        worker = _need_principal(holder, ctx)
        if not isinstance(worker, str):
            return worker
        svc = _service(target, worker, holder, ctx)
        return await asyncio.to_thread(svc.take, id)

    @server.tool(
        name="takeover",
        description=(
            "Take over a live claim held by a session that cannot resume. "
            "Name the exact holder you inspected and why it is dead; the "
            "transfer is audited under your session."
        ),
    )
    async def takeover_tool(
        id: str,
        expected_holder: str,
        reason: str,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        worker = _need_principal(holder, ctx)
        if not isinstance(worker, str):
            return worker
        svc = _service(target, worker, holder, ctx)
        return await asyncio.to_thread(svc.takeover, id, expected_holder, reason)

    @server.tool(
        name="release",
        description="Release a claim by generation without finishing.",
    )
    async def release_tool(
        id: str,
        generation: str,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        worker = _need_principal(holder, ctx)
        if not isinstance(worker, str):
            return worker
        svc = _service(target, worker, holder, ctx)
        return await asyncio.to_thread(svc.release, id, generation)

    @server.tool(
        name="prepare",
        description="Record verified batch-member work without completing the task.",
    )
    async def prepare_tool(
        id: str,
        generation: str,
        batch_id: str,
        member_id: str,
        source_worktree: str,
        source_revision: str,
        source_base: str,
        accepted_head: str,
        integration_head: str,
        scope_hash: str,
        verification: dict[str, Any],
        implementation_dependencies: list[str] | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        worker = _need_principal(holder, ctx)
        if not isinstance(worker, str):
            return worker
        svc = _service(target, worker, holder, ctx)
        return await asyncio.to_thread(
            svc.prepare,
            id,
            generation,
            batch_id=batch_id,
            member_id=member_id,
            source_worktree=source_worktree,
            source_revision=source_revision,
            source_base=source_base,
            accepted_head=accepted_head,
            integration_head=integration_head,
            scope_hash=scope_hash,
            verification=verification,
            implementation_dependencies=implementation_dependencies or [],
        )

    @server.tool(
        name="bind_batch_pr",
        description="Bind the batch's final PR before closeout.",
    )
    async def bind_batch_pr_tool(
        batch_id: str,
        owner_generation: str,
        number: int,
        node_id: str,
        head: str,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        worker = _need_principal(holder, ctx)
        if not isinstance(worker, str):
            return worker
        svc = _service(target, worker, holder, ctx)
        return await asyncio.to_thread(
            svc.bind_batch_pr, batch_id, owner_generation, number=number, node_id=node_id, head=head,
        )

    @server.tool(
        name="abort_batch",
        description="Abort a claim-free active batch and recover its members.",
    )
    async def abort_batch_tool(
        batch_id: str,
        owner_generation: str,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        worker = _need_principal(holder, ctx)
        if not isinstance(worker, str):
            return worker
        svc = _service(target, worker, holder, ctx)
        return await asyncio.to_thread(svc.abort_batch, batch_id, owner_generation)

    @server.tool(
        name="finish",
        description="Close a task. Prepared tasks additionally require current final-tree evidence.",
    )
    async def finish_tool(
        id: str,
        generation: str,
        final_evidence: dict[str, Any] | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        worker = _need_principal(holder, ctx)
        if not isinstance(worker, str):
            return worker
        svc = _service(target, worker, holder, ctx)
        return await asyncio.to_thread(svc.finish, id, generation, final_evidence)

    @server.tool(
        name="renew",
        description="Extend a long-running claim. Same generation, no progress milestones required.",
    )
    async def renew_tool(
        id: str,
        generation: str,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        worker = _need_principal(holder, ctx)
        if not isinstance(worker, str):
            return worker
        svc = _service(target, worker, holder, ctx)
        return await asyncio.to_thread(svc.renew, id, generation)

    @server.tool(
        name="drop",
        description="Abandon a task as dropped. Unclaimed tasks drop freely; a live claim needs its generation.",
    )
    async def drop_tool(
        id: str,
        generation: str | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        worker = _need_principal(holder, ctx)
        if not isinstance(worker, str):
            return worker
        svc = _service(target, worker, holder, ctx)
        return await asyncio.to_thread(svc.drop, id, generation)
