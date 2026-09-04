"""Response envelope for the MCP surface.

Every tool returns one of:

  {"ok": True,  "data": {...}}
  {"ok": False, "code": "E_...", "error": "...", "recovery": [...], "kind": "gate" | "error"}

``kind="gate"`` is an expected in-band gate result the model should act on;
``kind="error"`` is a protocol or environment failure. This carries forward the
Pi adapter's ``isError: !gate`` distinction (§M2).

Byte budget: 16 KiB after compact JSON. ``enforce_cap`` implements the
drop-trailing-items paging fallback for list-shaped tools before falling back to
``E_OUTPUT_TRUNCATED``.
"""

from __future__ import annotations

import json
from typing import Any

MAX_BYTES = 16 * 1024

# ``gate`` codes are expected in-band results the model should act on, not
# protocol failures. Subset of ``src/todo_db/errors.py`` plus server-added
# ones. ``E_NO_PRINCIPAL`` is intentionally excluded: tools return it with
# kind="error" (fail-closed auth, model must call get_instructions first).
GATE_CODES = frozenset(
    {
        "E_CLAIM_STALE",
        "E_MULTIPLE_CLAIMS",
        "E_LINT_GATE",
        "E_SCOPE_GATE",
        "E_VERIFY_GATE",
        "E_NOTHING_READY",
        "E_BASE_DIVERGED",
        "E_BASE_UNREACHABLE",
        "E_SCHEMA",
        "E_SCHEMA_BEHIND",
        "E_SCHEMA_DIVERGED",
        "E_IDENTITY",
        "E_AUDIT",
        "E_OUTPUT_TRUNCATED",
        "E_HOSTED",
    }
)


def ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


def err(
    code: str,
    message: str,
    *,
    recovery: list[str] | None = None,
    kind: str | None = None,
) -> dict[str, Any]:
    if kind is None:
        kind = "gate" if code in GATE_CODES else "error"
    return {
        "ok": False,
        "code": code,
        "error": message,
        "recovery": recovery or [],
        "kind": kind,
    }


def _compact(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


def capped(data: Any, *, code: str = "E_OUTPUT_TRUNCATED") -> dict[str, Any]:
    """Wrap *data* in an ok envelope, or an E_OUTPUT_TRUNCATED err if it overflows.

    List-shaped tools should call ``paged`` first to drop trailing items; this is
    the last-resort guard for non-list payloads or when paging still overflows.
    """

    env = ok(data)
    if len(_compact(env).encode("utf-8")) <= MAX_BYTES:
        return env
    return err(
        code,
        f"response exceeds {MAX_BYTES} bytes; retry with a smaller limit or a section/cursor",
        recovery=[f"retry with limit smaller than {len(_compact(data).encode('utf-8'))}"],
        kind="gate",
    )


def paged(
    items: list[Any],
    total: int,
    *,
    limit: int,
    cursor: int,
    code: str = "E_OUTPUT_TRUNCATED",
) -> dict[str, Any]:
    """Drop trailing items until the ok envelope fits, or return E_OUTPUT_TRUNCATED.

    Used by list-shaped tools (list_items, ready, deferrals, etc.) as the
    paging fallback described in plan §11 / §S3.
    """

    # Try full list first.
    data: dict[str, Any] = {"items": items, "total": total, "limit": limit, "cursor": cursor}
    env = ok(data)
    if len(_compact(env).encode("utf-8")) <= MAX_BYTES:
        return env

    # Drop trailing items until it fits or we are empty.
    for keep in range(len(items) - 1, -1, -1):
        data = {"items": items[:keep], "total": total, "limit": limit, "cursor": cursor, "truncated": True}
        env = ok(data)
        if len(_compact(env).encode("utf-8")) <= MAX_BYTES:
            return env

    return err(
        code,
        f"response exceeds {MAX_BYTES} bytes even with one item; retry with a smaller limit",
        recovery=["retry with limit=1 and a cursor, or request a section"],
        kind="gate",
    )
