"""The small agent surface: eight tools over shared service operations.

``list_items`` / ``show_item`` / ``create_item`` / ``update_item`` /
``take`` / ``release`` / ``finish`` / ``renew`` are backed by
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
from .identity import PrincipalHolder
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


def _service(target: ResolvedTarget, worker: str) -> TrackerService:
    return TrackerService(ref=target.state_ref, cache_dir=target.cache_dir, worker=worker)


def _need_principal(holder: PrincipalHolder, ctx: Context | None) -> str | dict[str, Any] | None:
    worker = _principal(holder, ctx)
    if not worker:
        return err(E_NO_PRINCIPAL, "principal not yet resolved; call get_instructions first", kind="error")
    return worker


def register_tools(server: FastMCP, target: ResolvedTarget, holder: PrincipalHolder) -> None:
    @server.tool(
        name="list_items",
        description="List tasks as brief rows (id/title/priority/status). Filters: status, priority, text, ready_only. Paging: limit (default 5) + cursor.",
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
        svc = _service(target, worker)
        return await asyncio.to_thread(
            svc.list_items, status=status, priority=priority, text=text,
            ready_only=ready_only, limit=limit, cursor=cursor,
        )

    @server.tool(
        name="show_item",
        description="Show one task with needs, readiness, and sections. Large fields spill to section reads: field/offset/budget/rev.",
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
        svc = _service(target, worker)
        return await asyncio.to_thread(svc.show_item, id, field=field, offset=offset, budget=budget, rev=rev)

    @server.tool(
        name="create_item",
        description="Create a task: id, title, priority (default medium), description, needs, acceptance, links, context.",
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
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        worker = _need_principal(holder, ctx)
        if not isinstance(worker, str):
            return worker
        svc = _service(target, worker)
        return await asyncio.to_thread(
            svc.create_item, id, title, priority=priority, description=description,
            needs=needs or [], acceptance=acceptance or [], links=links or [], context=context,
        )

    @server.tool(
        name="update_item",
        description="Edit a task: title/priority/description/needs/sections, or open/blocked moves. Closing goes through finish.",
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
        status: str | None = None,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        worker = _need_principal(holder, ctx)
        if not isinstance(worker, str):
            return worker
        svc = _service(target, worker)
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
        if status is not None:
            kwargs["status"] = status
        return await asyncio.to_thread(svc.update_item, id, **kwargs)

    @server.tool(
        name="take",
        description="Claim a task. Returns the claim generation plus enough context to begin work. One live claim per worker.",
    )
    async def take_tool(
        id: str,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        worker = _need_principal(holder, ctx)
        if not isinstance(worker, str):
            return worker
        svc = _service(target, worker)
        return await asyncio.to_thread(svc.take, id)

    @server.tool(
        name="release",
        description="Hand a claim back without finishing. Needs the generation returned by take.",
    )
    async def release_tool(
        id: str,
        generation: str,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        worker = _need_principal(holder, ctx)
        if not isinstance(worker, str):
            return worker
        svc = _service(target, worker)
        return await asyncio.to_thread(svc.release, id, generation)

    @server.tool(
        name="finish",
        description="Close a task. Needs the generation returned by take. No work breakdown or attestation required.",
    )
    async def finish_tool(
        id: str,
        generation: str,
        ctx: Context = None,  # type: ignore[assignment]
    ) -> dict[str, Any]:
        worker = _need_principal(holder, ctx)
        if not isinstance(worker, str):
            return worker
        svc = _service(target, worker)
        return await asyncio.to_thread(svc.finish, id, generation)

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
        svc = _service(target, worker)
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
        svc = _service(target, worker)
        return await asyncio.to_thread(svc.drop, id, generation)
