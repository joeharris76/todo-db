"""Shared application operations over the JSON/Git state branch.

This module backs both the MCP agent interface and the human/CI CLI, so
transport logic is never duplicated. Every response is capped at its final
model-visible serialization (:data:`MAX_BYTES`); lists page with stable
cursors scoped to a state revision; oversized fields report a usable
section read instead of looping or truncating silently.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import git_backend
from . import store as S
from .errors import (
    E_CONFLICT,
    E_CURSOR_STALE,
    E_MULTIPLE_CLAIMS,
    E_OFFLINE,
    E_OUTPUT_TRUNCATED,
    E_OVERSIZED,
    E_STATE,
    E_UNKNOWN,
    TodoError,
)

MAX_BYTES = 16 * 1024
LIST_DEFAULT_LIMIT = 5
LIST_MAX_LIMIT = 50
SECTION_BUDGET = 6000
TAKE_EXCERPT = 2000
TOKENIZER_NAME = "o200k_base"


def count_tokens(text: str) -> tuple[int, str]:
    """Return (tokens, tokenizer). o200k_base when available, else chars/4."""
    try:
        import tiktoken

        return len(tiktoken.get_encoding(TOKENIZER_NAME).encode(text)), TOKENIZER_NAME
    except Exception:
        return max(1, len(text) // 4), "chars/4-fallback"


def _compact(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


def ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


def err(code: str, message: str, recovery: list[str] | None = None) -> dict[str, Any]:
    kind = "error" if code in (E_OFFLINE, E_UNKNOWN, E_STATE) else "gate"
    return {"ok": False, "code": code, "error": message, "recovery": recovery or [], "kind": kind}


def _fits(envelope: dict[str, Any]) -> bool:
    return len(_compact(envelope).encode("utf-8")) <= MAX_BYTES


def encode_cursor(rev: str, offset: int) -> str:
    raw = _compact({"rev": rev, "offset": offset})
    return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_cursor(cursor: str) -> tuple[str, int]:
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        return str(payload["rev"]), int(payload["offset"])
    except Exception as exc:
        raise TodoError(f"invalid cursor: {exc}", code=E_CURSOR_STALE) from exc


@dataclass
class TrackerService:
    """One worker's view of a state branch. Cache lives outside worktrees."""

    ref: git_backend.StateRef
    cache_dir: str | Path
    worker: str
    ttl_hours: float = S.DEFAULT_TTL_HOURS

    def __post_init__(self) -> None:
        self.worker = S.validate_worker(self.worker)

    # -- reads --

    def _read(self) -> git_backend.ReadOutcome:
        try:
            return git_backend.read(self.ref, self.cache_dir)
        except TodoError as exc:
            raise exc

    def list_items(
        self,
        *,
        status: str | None = None,
        priority: str | None = None,
        text: str | None = None,
        ready_only: bool = False,
        limit: int = LIST_DEFAULT_LIMIT,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        try:
            now = datetime.now(timezone.utc)
            outcome = self._read()
            rows = S.query_items(
                outcome.snapshot, status=status, priority=priority,
                text=text, ready_only=ready_only, now=now,
            )
            total = len(rows)
            offset = 0
            if cursor:
                rev, offset = decode_cursor(cursor)
                if rev != outcome.rev:
                    return err(
                        E_CURSOR_STALE,
                        "cursor revision changed while listing; retry from the first page",
                        recovery=["call list_items without a cursor"],
                    )
            limit = max(1, min(int(limit), LIST_MAX_LIMIT))
            page = rows[offset : offset + limit]
            items = []
            for item_id, entry in page:
                items.append({
                    "id": item_id,
                    "title": entry["title"],
                    "priority": entry["priority"],
                    "status": entry["status"],
                    "ready": S.is_ready(item_id, outcome.snapshot, now),
                })
            remaining = total - offset - len(page)
            data: dict[str, Any] = {"items": items, "total": total, "limit": limit, "cursor_offset": offset}
            if outcome.stale:
                data["cached_rev"] = outcome.rev
                data["stale"] = True
            else:
                data["rev"] = outcome.rev
            if remaining > 0:
                data["next_cursor"] = encode_cursor(outcome.rev, offset + len(page))
            env: dict[str, Any] = ok(data)
            while items and not _fits(env):
                items.pop()
                data["items"] = items
                data["truncated"] = True
                data.pop("next_cursor", None)
                if offset + len(items) < total:
                    data["next_cursor"] = encode_cursor(outcome.rev, offset + len(items))
                env = ok(data)
            if not _fits(env):
                return err(
                    E_OUTPUT_TRUNCATED,
                    "list response exceeds the byte cap; narrow with status/priority/text filters",
                    recovery=["retry with a text filter or ready_only=true"],
                )
            return env
        except TodoError as exc:
            return err(exc.code or E_STATE, str(exc))

    def show_item(
        self,
        item_id: str,
        *,
        field: str | None = None,
        offset: int = 0,
        budget: int = SECTION_BUDGET,
    ) -> dict[str, Any]:
        try:
            now = datetime.now(timezone.utc)
            outcome = self._read()
            snap = outcome.snapshot
            if item_id not in snap.index["items"]:
                return err(E_STATE, f"unknown task {item_id!r}")
            entry = snap.index["items"][item_id]
            detail = snap.details[item_id]
            if field is not None:
                return self._show_section(item_id, detail, field, offset, budget)
            needs = list(entry.get("needs", []))
            data: dict[str, Any] = {
                "id": item_id,
                "title": entry["title"],
                "priority": entry["priority"],
                "status": entry["status"],
                "ready": S.is_ready(item_id, snap, now),
                "needs": needs,
                "unmet_needs": [dep for dep in needs if snap.index["items"][dep]["status"] != "done"],
                "unlocks": S.downstream_unlocks(item_id, snap),
                "rev": outcome.rev,
            }
            if entry.get("claim"):
                claim = entry["claim"]
                data["claim"] = {
                    "worker": claim["worker"],
                    "expires_at": claim["expires_at"],
                    "live": S.claim_is_live(claim, now),
                }
            sections: dict[str, Any] = {}
            for key in ("description", "acceptance", "links", "context", "legacy"):
                if key in detail:
                    sections[key] = self._section_summary(detail[key])
            data["sections"] = sections
            # Inline small fields; spill large ones to section reads.
            for key in ("description", "context"):
                value = detail.get(key, "")
                if isinstance(value, str) and len(value.encode("utf-8")) <= SECTION_BUDGET:
                    data[key] = value
                elif isinstance(value, str) and value:
                    data[f"{key}_continuation"] = {"field": key, "offset": 0, "budget": SECTION_BUDGET}
            for key in ("acceptance", "links"):
                value = detail.get(key, [])
                blob = _compact(value)
                if len(blob.encode("utf-8")) <= SECTION_BUDGET:
                    data[key] = value
                elif value:
                    data[f"{key}_continuation"] = {"field": key, "offset": 0, "budget": SECTION_BUDGET}
            env = ok(data)
            if _fits(env):
                return env
            slim: dict[str, Any] = {
                "id": item_id,
                "title": entry["title"],
                "priority": entry["priority"],
                "status": entry["status"],
                "rev": outcome.rev,
                "sections": sections,
                "continuation": "response exceeded the byte cap; read sections with show_item field/offset",
            }
            if _fits(ok(slim)):
                return ok(slim)
            return err(
                E_OVERSIZED,
                f"task {item_id!r} metadata exceeds the byte cap; read one section at a time",
                recovery=[f"show_item id={item_id!r} field='description'"],
            )
        except TodoError as exc:
            return err(exc.code or E_STATE, str(exc))

    def _section_summary(self, value: Any) -> dict[str, Any]:
        blob = value if isinstance(value, str) else _compact(value)
        size = len(blob.encode("utf-8"))
        tokens, tokenizer = count_tokens(blob)
        return {"bytes": size, "tokens": tokens, "tokenizer": tokenizer}

    def _show_section(self, item_id: str, detail: dict[str, Any], field: str, offset: int, budget: int) -> dict[str, Any]:
        if field not in detail:
            return err(E_STATE, f"task {item_id!r} has no section {field!r}")
        budget = max(1, min(int(budget), MAX_BYTES))
        offset = max(0, int(offset))
        value = detail[field]
        blob = value if isinstance(value, str) else _compact(value)
        total = len(blob)
        if offset >= total:
            return err(E_STATE, f"section offset {offset} is past the end ({total})")
        window = blob[offset : offset + budget]
        data: dict[str, Any] = {"id": item_id, "field": field, "offset": offset, "total": total, "window": window}
        if offset + budget < total:
            data["continuation"] = {"field": field, "offset": offset + budget, "budget": budget}
        env = ok(data)
        if not _fits(env):
            return err(
                E_OVERSIZED,
                "section window exceeds the byte cap; retry with a smaller budget",
                recovery=["retry with a smaller budget"],
            )
        return env

    # -- mutations --

    def _mutate(self, op: str, summary: str, apply) -> dict[str, Any]:
        op_id = git_backend.new_op_id()
        try:
            result = git_backend.mutate(
                self.ref, op=op, summary=summary, worker=self.worker, op_id=op_id, apply=apply,
            )
        except TodoError as exc:
            return err(exc.code or E_STATE, str(exc))
        if result.ok:
            ack = {"id": result.ack.get("id"), "rev": result.sha, **{k: v for k, v in result.ack.items() if k != "id"}}
            env = ok(ack)
            if not _fits(env):
                return err(E_OUTPUT_TRUNCATED, "acknowledgement exceeded the byte cap")
            return env
        if result.outcome == "unknown":
            return err(
                E_UNKNOWN,
                f"{result.error} (operation ID {result.op_id})",
                recovery=[f"reconcile with operation ID {result.op_id}"],
            )
        if result.outcome == "offline":
            return err(E_OFFLINE, result.error or "remote unreachable", recovery=["retry when the remote is reachable"])
        return err(result.code or E_STATE, result.error or "mutation failed")

    def _held_claim(self, snap: S.Snapshot, now: datetime) -> str | None:
        for item_id, entry in snap.index["items"].items():
            claim = entry.get("claim")
            if claim and claim.get("worker") == self.worker and S.claim_is_live(claim, now):
                return item_id
        return None

    def create_item(self, item_id: str, title: str, **kwargs: Any) -> dict[str, Any]:
        def apply(snap: S.Snapshot) -> dict[str, Any]:
            S.op_create(snap, item_id=item_id, title=title, **kwargs)
            return {"id": item_id, "status": "open"}

        return self._mutate("create", f"add {item_id}", apply)

    def update_item(self, item_id: str, **kwargs: Any) -> dict[str, Any]:
        def apply(snap: S.Snapshot) -> dict[str, Any]:
            return S.op_update(snap, item_id, **kwargs)

        return self._mutate("update", f"edit {item_id}", apply)

    def take(self, item_id: str) -> dict[str, Any]:
        worker = self.worker

        def apply(snap: S.Snapshot) -> dict[str, Any]:
            now = datetime.now(timezone.utc)
            held = self._held_claim(snap, now)
            if held is not None and held != item_id:
                raise TodoError(
                    f"worker {worker!r} already holds a live claim on {held!r}; release or finish it first",
                    code=E_MULTIPLE_CLAIMS,
                )
            took = S.op_take(snap, item_id, worker, ttl_hours=self.ttl_hours)
            entry = snap.index["items"][item_id]
            detail = snap.details[item_id]
            needs = list(entry.get("needs", []))
            description = detail.get("description", "")
            excerpt = description[:TAKE_EXCERPT]
            ack: dict[str, Any] = {
                "id": item_id,
                "status": "active",
                "title": entry["title"],
                "priority": entry["priority"],
                "needs": needs,
                "unmet_needs": [d for d in needs if snap.index["items"][d]["status"] != "done"],
                "claim": {
                    "generation": took["claim"]["generation"],
                    "expires_at": took["claim"]["expires_at"],
                },
                "description_excerpt": excerpt,
            }
            if len(description) > len(excerpt):
                ack["description_continuation"] = {
                    "field": "description",
                    "offset": len(excerpt),
                    "budget": SECTION_BUDGET,
                }
            return ack

        return self._mutate("take", f"{worker} takes {item_id}", apply)

    def release(self, item_id: str, generation: str) -> dict[str, Any]:
        worker = self.worker

        def apply(snap: S.Snapshot) -> dict[str, Any]:
            return S.op_release(snap, item_id, worker, generation)

        return self._mutate("release", f"{worker} releases {item_id}", apply)

    def finish(self, item_id: str, generation: str) -> dict[str, Any]:
        worker = self.worker

        def apply(snap: S.Snapshot) -> dict[str, Any]:
            return S.op_finish(snap, item_id, worker, generation)

        return self._mutate("finish", f"{worker} finishes {item_id}", apply)

    def renew(self, item_id: str, generation: str) -> dict[str, Any]:
        worker = self.worker

        def apply(snap: S.Snapshot) -> dict[str, Any]:
            renewed = S.op_renew(snap, item_id, worker, generation, ttl_hours=self.ttl_hours)
            return {"id": item_id, "expires_at": renewed["claim"]["expires_at"]}

        return self._mutate("renew", f"{worker} renews {item_id}", apply)

    def drop(self, item_id: str) -> dict[str, Any]:
        worker = self.worker

        def apply(snap: S.Snapshot) -> dict[str, Any]:
            return S.op_drop(snap, item_id, worker)

        return self._mutate("drop", f"{worker} drops {item_id}", apply)
