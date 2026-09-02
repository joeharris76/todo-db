"""Six hot-path lifecycle tools (ADR 0006 G9, plan §5).

Always loaded: ``next``, ``take``, ``context``, ``progress``, ``finish``,
``release`` plus ``claims`` for E_MULTIPLE_CLAIMS recovery. Every tool is
async, dispatches all DB and git work to the single dedicated worker thread,
and returns the ``{ok, data}`` / ``{ok, code, error, recovery, kind}`` envelope.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from ..agent import AgentWorkflow, GitScopeEngine
from ..errors import E_MULTIPLE_CLAIMS, TodoDBError
from ..tracker import TodoTracker
from .dbpool import database_for_tool
from .envelope import MAX_BYTES, err, ok
from .identity import PrincipalHolder
from .target import ResolvedTarget
from .worker import run_in_worker

LOG = logging.getLogger("todo_db.mcp")

# Default context limit chosen together with the 16 KiB byte cap (plan §11).
# ``context`` at limit 20 on a large item already approaches the cap; callers
# that hit E_OUTPUT_TRUNCATED should retry with a smaller limit or a section.
DEFAULT_CONTEXT_LIMIT = 20


def _principal(holder: PrincipalHolder, ctx: Context | None) -> str | None:
    if holder.principal is not None:
        return holder.principal
    # Fallback: derive from the handshake if still pending (S2).
    try:
        ci = ctx.request_context.session.client_params.clientInfo if ctx else None  # type: ignore[union-attr]
    except AttributeError:
        ci = None
    if ci is not None:
        return holder.ensure(ci)
    return holder.principal


def _claims_for_principal(db, principal: str) -> list[dict[str, Any]]:
    rows = db.connection.execute(
        "SELECT id, claim_token FROM items WHERE claimed_by = ? AND state = 'active' ORDER BY id",
        (principal,),
    ).fetchall()
    return [{"id": r["id"], "claim_token": r["claim_token"]} for r in rows]


def _to_err(exc: BaseException, *, principal: str | None = None, db: Any | None = None) -> dict[str, Any]:
    code = getattr(exc, "code", None) or "E_ERROR"
    msg = str(exc)
    # E_MULTIPLE_CLAIMS: enrich with item ids + tokens so the model can release.
    if code == E_MULTIPLE_CLAIMS and principal and db is not None:
        try:
            claims = _claims_for_principal(db, principal)
            recovery = [f"{c['id']}:{c['claim_token']}" for c in claims] or [msg]
            return err(code, msg, recovery=recovery, kind="gate")
        except Exception:
            pass
    # Heuristic gate vs error: codes in GATE_CODES are gates, others are errors.
    return err(code, msg, recovery=[], kind=None)


def register_work_tools(
    server: FastMCP,
    target: ResolvedTarget,
    holder: PrincipalHolder,
    session_id: str,
    allow_hosted: bool = False,
) -> None:
    def _target() -> ResolvedTarget:
        return target

    @server.tool(
        name="next",
        description="Inspect the ready queue or your existing claim. Returns claim-or-ready plus next_action.",
    )
    async def next_tool(ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        principal = _principal(holder, ctx)
        if not principal:
            return err("E_NO_PRINCIPAL", "principal not yet resolved; call get_instructions first", kind="error")

        def _work():
            with database_for_tool(_target(), "next", allow_hosted=allow_hosted) as db:
                tracker = TodoTracker(db, actor=principal)
                wf = AgentWorkflow(tracker, git_engine=GitScopeEngine(_target().repo_root))
                try:
                    data = wf.next(principal=principal)
                except TodoDBError as exc:
                    return _to_err(exc, principal=principal, db=db)
                # Check cap
                import json

                if (
                    len(json.dumps({"ok": True, "data": data}, separators=(",", ":"), sort_keys=True).encode())
                    > MAX_BYTES
                ):
                    # For next, the payload is small; just truncate via capped?
                    from .envelope import capped

                    return capped(data)
                return ok(data)

        return await run_in_worker(_work)

    @server.tool(
        name="take",
        description="Atomically claim a ready item or re-adopt your active claim. Pass the server's session id internally so a restart auto-adopts.",
    )
    async def take_tool(id: str | None = None, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        principal = _principal(holder, ctx)
        if not principal:
            return err("E_NO_PRINCIPAL", "principal not yet resolved; call get_instructions first", kind="error")

        def _work():
            with database_for_tool(_target(), "take", allow_hosted=allow_hosted) as db:
                tracker = TodoTracker(db, actor=principal)
                wf = AgentWorkflow(tracker, git_engine=GitScopeEngine(_target().repo_root))
                try:
                    data = wf.take(id, session=session_id)
                except TodoDBError as exc:
                    return _to_err(exc, principal=principal, db=db)
                return ok(data)

        return await run_in_worker(_work)

    @server.tool(
        name="context",
        description="Fetch bounded, guardrailed context for the claimed item; also how you re-read claim_token and next_action after a restart. Supports section/cursor/limit and fields projection.",
    )
    async def context_tool(
        id: str,
        fields: list[str] | None = None,
        section: str | None = None,
        cursor: int = 0,
        limit: int = DEFAULT_CONTEXT_LIMIT,
        ctx: Context = None,
    ) -> dict[str, Any]:  # type: ignore[assignment]
        principal = _principal(holder, ctx)
        if not principal:
            return err("E_NO_PRINCIPAL", "principal not yet resolved; call get_instructions first", kind="error")

        def _work():
            with database_for_tool(_target(), "context", allow_hosted=allow_hosted) as db:
                tracker = TodoTracker(db, actor=principal)
                wf = AgentWorkflow(tracker, git_engine=GitScopeEngine(_target().repo_root))
                try:
                    data = wf.context(id, fields=fields, section=section, cursor=cursor, limit=limit)
                except TodoDBError as exc:
                    return _to_err(exc, principal=principal, db=db)
                # Byte cap with paging fallback is handled inside context's completeness;
                # still guard the envelope size.
                import json

                if (
                    len(json.dumps({"ok": True, "data": data}, separators=(",", ":"), sort_keys=True).encode())
                    > MAX_BYTES
                ):
                    from .envelope import capped

                    return capped(data)
                return ok(data)

        return await run_in_worker(_work)

    @server.tool(name="progress", description="Mark a work unit done with evidence; this refreshes the lease.")
    async def progress_tool(id: str, wid: str, evidence: str, claim_token: str, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        principal = _principal(holder, ctx)
        if not principal:
            return err("E_NO_PRINCIPAL", "principal not yet resolved; call get_instructions first", kind="error")

        def _work():
            with database_for_tool(_target(), "progress", allow_hosted=allow_hosted) as db:
                tracker = TodoTracker(db, actor=principal)
                wf = AgentWorkflow(tracker, git_engine=GitScopeEngine(_target().repo_root))
                try:
                    # progress signature is progress(item_id, wid, evidence, claim_token=...)
                    data = wf.progress(id, wid, evidence, claim_token=claim_token)
                except TodoDBError as exc:
                    return _to_err(exc, principal=principal, db=db)
                return ok(data)

        return await run_in_worker(_work)

    @server.tool(
        name="finish",
        description="The no-shell close gate. Model-assert only: requires a current workspace-fingerprint attestation and rejects a stale pass.",
    )
    async def finish_tool(id: str, claim_token: str, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        principal = _principal(holder, ctx)
        if not principal:
            return err("E_NO_PRINCIPAL", "principal not yet resolved; call get_instructions first", kind="error")

        def _work():
            with database_for_tool(_target(), "finish", allow_hosted=allow_hosted) as db:
                tracker = TodoTracker(db, actor=principal)
                wf = AgentWorkflow(tracker, git_engine=GitScopeEngine(_target().repo_root))
                try:
                    data = wf.finish(id, claim_token=claim_token, model_assert=True)
                except TodoDBError as exc:
                    return _to_err(exc, principal=principal, db=db)
                return ok(data)

        return await run_in_worker(_work)

    @server.tool(name="release", description="Hand the claim back without finishing.")
    async def release_tool(id: str, claim_token: str, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        principal = _principal(holder, ctx)
        if not principal:
            return err("E_NO_PRINCIPAL", "principal not yet resolved; call get_instructions first", kind="error")

        def _work():
            with database_for_tool(_target(), "release", allow_hosted=allow_hosted) as db:
                tracker = TodoTracker(db, actor=principal)
                wf = AgentWorkflow(tracker, git_engine=GitScopeEngine(_target().repo_root))
                try:
                    data = wf.release(id, claim_token)
                except TodoDBError as exc:
                    return _to_err(exc, principal=principal, db=db)
                return ok(data)

        return await run_in_worker(_work)

    @server.tool(name="claims", description="List your active claims (for E_MULTIPLE_CLAIMS recovery).")
    async def claims_tool(ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        principal = _principal(holder, ctx)
        if not principal:
            return err("E_NO_PRINCIPAL", "principal not yet resolved; call get_instructions first", kind="error")

        def _work():
            with database_for_tool(_target(), "claims", allow_hosted=allow_hosted) as db:
                claims = _claims_for_principal(db, principal)
                return ok({"claims": claims, "principal": principal})

        return await run_in_worker(_work)
