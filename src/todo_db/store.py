"""Canonical JSON task state: index ownership, detail files, and pure operations.

State layout inside a checked-out state revision::

    index.json
    items/<id>.json

``index.json`` is ``{"schema_version": 1, "items": {id: entry}}`` where each
entry owns exactly ``title``, ``priority``, ``status``, ``claim``, and optional
``needs`` IDs. Detail files carry descriptions, acceptance criteria, links,
and retained task context. Detail files must not contain independently
editable copies of index-owned fields.

Lifecycle: ``open``, ``active``, ``blocked``, ``done``, ``dropped``.
Priorities preserve the historical bands: ``critical``, ``high``,
``medium-high``, ``medium``, ``low``.

A claim is ``{"worker": str, "expires_at": UTC ISO Z, "generation": uuid4hex}``.
Ownership plus generation checks protect renew, release, and finish from
stale writers. Claims are cooperative concurrency controls, not
authentication against a malicious repository writer: every writer must run
the publication protocol in :mod:`todo_db.git_backend`, and branch
rewrites/deletion invalidate its assumptions. Clock assumption: writers'
clocks are roughly synchronized (minutes); the default TTL is 24h so small
skew is harmless.

This module is pure (no subprocess, no network): it operates on in-memory
``(index, details)`` snapshots plus atomic local-directory IO. Git
publication lives in :mod:`todo_db.git_backend`.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .errors import (
    E_ACTIVE_CLAIMS,
    E_CLAIM_STALE,
    E_CONFLICT,
    E_NOTHING_READY,
    E_SCHEMA,
    E_STATE,
    TodoError,
)

SCHEMA_VERSION = 1
STATUSES = ("open", "active", "blocked", "done", "dropped")
PRIORITIES = ("critical", "high", "medium-high", "medium", "low")
PRIORITY_RANK = {name: rank for rank, name in enumerate(PRIORITIES)}
TERMINAL_STATUSES = ("done", "dropped")

ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
MAX_ID_LEN = 64
MAX_TITLE_LEN = 200
MAX_WORKER_LEN = 128
DEFAULT_TTL_HOURS = 24.0
MAX_TTL_HOURS = 72.0

INDEX_FILENAME = "index.json"
ITEMS_DIRNAME = "items"

#: Fields owned by the index. Detail files containing any of these are
#: rejected so there is never an ambiguous second copy to merge.
INDEX_OWNED_FIELDS = frozenset({"title", "priority", "status", "claim", "needs"})

#: Status transitions. Close paths (finish/drop) are separate ops with claim
#: checks; plain ``update`` may not enter ``done``/``dropped`` directly.
TRANSITIONS: dict[str, frozenset[str]] = {
    "open": frozenset({"active", "blocked", "dropped"}),
    "active": frozenset({"open", "blocked", "done", "dropped"}),
    "blocked": frozenset({"open", "active", "done", "dropped"}),
    "done": frozenset(),
    "dropped": frozenset(),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(raw: str) -> datetime:
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, AttributeError) as exc:
        raise TodoError(f"invalid timestamp {raw!r}: expected UTC ISO-8601 Z", code=E_STATE) from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def new_generation() -> str:
    return uuid4().hex


def claim_is_live(claim: dict[str, Any] | None, now: datetime | None = None) -> bool:
    if not claim:
        return False
    try:
        expires = parse_ts(str(claim.get("expires_at", "")))
    except TodoError:
        return False
    moment = now or datetime.now(timezone.utc)
    return expires > moment


def validate_id(item_id: str) -> None:
    if not isinstance(item_id, str) or not item_id:
        raise TodoError(f"invalid task ID {item_id!r}: must be a non-empty string", code=E_STATE)
    if len(item_id) > MAX_ID_LEN or not ID_RE.fullmatch(item_id):
        raise TodoError(
            f"invalid task ID {item_id!r}: use 1-{MAX_ID_LEN} chars of "
            "[a-z0-9-], starting and ending alphanumeric",
            code=E_STATE,
        )


def validate_worker(worker: str) -> str:
    if not isinstance(worker, str) or not worker.strip():
        raise TodoError("worker identity must be a non-empty string", code=E_STATE)
    cleaned = worker.strip()
    if len(cleaned) > MAX_WORKER_LEN:
        raise TodoError(f"worker identity exceeds {MAX_WORKER_LEN} chars", code=E_STATE)
    return cleaned


def empty_index() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "items": {}}


def _check_entry_shape(item_id: str, entry: Any) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise TodoError(f"index entry {item_id!r} must be an object", code=E_STATE)
    for key in ("title", "priority", "status"):
        if key not in entry:
            raise TodoError(f"index entry {item_id!r} is missing {key!r}", code=E_STATE)
    title = entry["title"]
    if not isinstance(title, str) or not title.strip() or len(title) > MAX_TITLE_LEN:
        raise TodoError(
            f"index entry {item_id!r} has an invalid title: 1-{MAX_TITLE_LEN} chars required",
            code=E_STATE,
        )
    if entry["priority"] not in PRIORITIES:
        raise TodoError(f"index entry {item_id!r} has unknown priority {entry['priority']!r}", code=E_STATE)
    if entry["status"] not in STATUSES:
        raise TodoError(f"index entry {item_id!r} has unknown status {entry['status']!r}", code=E_STATE)
    needs = entry.get("needs", [])
    if not isinstance(needs, list) or any(not isinstance(dep, str) for dep in needs):
        raise TodoError(f"index entry {item_id!r} has invalid needs: expected a list of IDs", code=E_STATE)
    claim = entry.get("claim")
    if claim is not None:
        if not isinstance(claim, dict):
            raise TodoError(f"index entry {item_id!r} has an invalid claim", code=E_STATE)
        for key in ("worker", "expires_at", "generation"):
            if key not in claim:
                raise TodoError(f"index entry {item_id!r} claim is missing {key!r}", code=E_STATE)
        validate_worker(str(claim["worker"]))
        parse_ts(str(claim["expires_at"]))
        generation = str(claim["generation"])
        if not re.fullmatch(r"[0-9a-f]{32}", generation):
            raise TodoError(f"index entry {item_id!r} claim has an invalid generation", code=E_STATE)
    return entry


def _check_cycles(items: dict[str, Any]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str, trail: list[str]) -> None:
        if node in visited:
            return
        if node in visiting:
            cycle = " -> ".join([*trail, node])
            raise TodoError(f"dependency cycle detected: {cycle}", code=E_STATE)
        visiting.add(node)
        for dep in items[node].get("needs", []):
            visit(dep, [*trail, node])
        visiting.discard(node)
        visited.add(node)

    for item_id in items:
        visit(item_id, [])


def validate_snapshot(index: Any, details: dict[str, Any]) -> dict[str, Any]:
    """Validate index/detail correspondence. Returns the index on success."""
    if not isinstance(index, dict):
        raise TodoError("index.json must contain a JSON object", code=E_STATE)
    version = index.get("schema_version")
    if version != SCHEMA_VERSION:
        raise TodoError(
            f"unsupported index schema version {version!r}: this package reads {SCHEMA_VERSION}",
            code=E_SCHEMA,
        )
    items = index.get("items")
    if not isinstance(items, dict):
        raise TodoError("index.json items must be an object keyed by ID", code=E_STATE)
    for item_id, entry in items.items():
        validate_id(item_id)
        _check_entry_shape(item_id, entry)
    for item_id, entry in items.items():
        for dep in entry.get("needs", []):
            validate_id(dep)
            if dep == item_id:
                raise TodoError(f"task {item_id!r} cannot depend on itself", code=E_STATE)
            if dep not in items:
                raise TodoError(f"task {item_id!r} needs unknown task {dep!r}", code=E_STATE)
        if entry["status"] in TERMINAL_STATUSES and entry.get("claim") is not None:
            raise TodoError(f"task {item_id!r} is {entry['status']} but still carries a claim", code=E_STATE)
    _check_cycles(items)
    if not isinstance(details, dict):
        raise TodoError("details must be a mapping of ID to detail object", code=E_STATE)
    missing = [item_id for item_id in items if item_id not in details]
    if missing:
        raise TodoError(f"missing detail files for: {', '.join(sorted(missing))}", code=E_STATE)
    orphaned = [item_id for item_id in details if item_id not in items]
    if orphaned:
        raise TodoError(f"orphaned detail files without index entries: {', '.join(sorted(orphaned))}", code=E_STATE)
    for item_id, detail in details.items():
        if not isinstance(detail, dict):
            raise TodoError(f"detail for {item_id!r} must be a JSON object", code=E_STATE)
        copies = INDEX_OWNED_FIELDS.intersection(detail)
        if copies:
            raise TodoError(
                f"detail for {item_id!r} must not copy index-owned fields: {', '.join(sorted(copies))}",
                code=E_STATE,
            )
        for key in ("acceptance", "links"):
            if key in detail and (
                not isinstance(detail[key], list) or any(not isinstance(v, str) for v in detail[key])
            ):
                raise TodoError(f"detail for {item_id!r}: {key!r} must be a list of strings", code=E_STATE)
        for key in ("description", "context"):
            if key in detail and not isinstance(detail[key], str):
                raise TodoError(f"detail for {item_id!r}: {key!r} must be a string", code=E_STATE)
    return index


@dataclass
class Snapshot:
    index: dict[str, Any]
    details: dict[str, dict[str, Any]] = field(default_factory=dict)


def load_snapshot(state_dir: str | Path) -> Snapshot:
    """Load and validate one local state directory."""
    root = Path(state_dir)
    index_path = root / INDEX_FILENAME
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TodoError(f"no {INDEX_FILENAME} in {root}: is this a state checkout?", code=E_STATE) from exc
    except json.JSONDecodeError as exc:
        raise TodoError(f"invalid {INDEX_FILENAME}: {exc}", code=E_STATE) from exc
    items_dir = root / ITEMS_DIRNAME
    details: dict[str, dict[str, Any]] = {}
    if isinstance(index, dict) and isinstance(index.get("items"), dict):
        for item_id in index["items"]:
            try:
                validate_id(item_id)
            except TodoError:
                continue
            path = items_dir / f"{item_id}.json"
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError as exc:
                raise TodoError(f"missing detail file for {item_id!r}", code=E_STATE) from exc
            except json.JSONDecodeError as exc:
                raise TodoError(f"invalid detail file for {item_id!r}: {exc}", code=E_STATE) from exc
            details[item_id] = payload
        if items_dir.is_dir():
            for path in sorted(items_dir.glob("*.json")):
                if path.name[:-5] not in index["items"]:
                    try:
                        details[path.name[:-5]] = json.loads(path.read_text(encoding="utf-8"))
                    except json.JSONDecodeError as exc:
                        raise TodoError(f"invalid detail file {path.name}: {exc}", code=E_STATE) from exc
    validate_snapshot(index, details)
    return Snapshot(index=index, details=details)


def save_snapshot(state_dir: str | Path, snapshot: Snapshot) -> None:
    """Atomically persist a snapshot after validation.

    Each file is written to a temporary sibling and renamed, so a partial
    local write never leaves a half-updated file behind. Published state
    still comes only from accepted Git commits; this guards the scratch
    copy readers and writers share on one machine.
    """
    validate_snapshot(snapshot.index, snapshot.details)
    root = Path(state_dir)
    items_dir = root / ITEMS_DIRNAME
    items_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write(root / INDEX_FILENAME, snapshot.index)
    for item_id, detail in snapshot.details.items():
        _atomic_write(items_dir / f"{item_id}.json", detail)
    wanted = {f"{item_id}.json" for item_id in snapshot.details}
    for path in items_dir.glob("*.json"):
        if path.name not in wanted:
            path.unlink()


def _atomic_write(path: Path, payload: Any) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def entry_brief(item_id: str, entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item_id,
        "title": entry["title"],
        "priority": entry["priority"],
        "status": entry["status"],
    }


def is_ready(item_id: str, snapshot: Snapshot, now: datetime | None = None) -> bool:
    entry = snapshot.index["items"].get(item_id)
    if entry is None or entry["status"] != "open":
        return False
    if claim_is_live(entry.get("claim"), now):
        return False
    return all(snapshot.index["items"][dep]["status"] == "done" for dep in entry.get("needs", []))


def downstream_unlocks(item_id: str, snapshot: Snapshot) -> int:
    return sum(
        1
        for other_id, entry in snapshot.index["items"].items()
        if other_id != item_id
        and item_id in entry.get("needs", [])
        and entry["status"] not in TERMINAL_STATUSES
    )


def order_key(item: tuple[str, dict[str, Any]]) -> tuple[int, str]:
    item_id, entry = item
    return (PRIORITY_RANK[entry["priority"]], item_id)


def query_items(
    snapshot: Snapshot,
    *,
    status: str | None = None,
    priority: str | None = None,
    text: str | None = None,
    ready_only: bool = False,
    now: datetime | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    moment = now or datetime.now(timezone.utc)
    if status is not None and status not in STATUSES:
        raise TodoError(f"unknown status {status!r}", code=E_STATE)
    if priority is not None and priority not in PRIORITIES:
        raise TodoError(f"unknown priority {priority!r}", code=E_STATE)
    needle = text.lower() if text else None
    rows: list[tuple[str, dict[str, Any]]] = []
    for item_id, entry in snapshot.index["items"].items():
        if status is not None and entry["status"] != status:
            continue
        if priority is not None and entry["priority"] != priority:
            continue
        if needle and needle not in item_id.lower() and needle not in str(entry["title"]).lower():
            continue
        if ready_only and not is_ready(item_id, snapshot, moment):
            continue
        rows.append((item_id, entry))
    rows.sort(key=order_key)
    return rows


def op_create(
    snapshot: Snapshot,
    *,
    item_id: str,
    title: str,
    priority: str = "medium",
    description: str = "",
    needs: list[str] | tuple[str, ...] = (),
    acceptance: list[str] | tuple[str, ...] = (),
    links: list[str] | tuple[str, ...] = (),
    context: str = "",
) -> str:
    validate_id(item_id)
    items = snapshot.index["items"]
    if item_id in items:
        raise TodoError(f"duplicate task ID {item_id!r}", code=E_STATE)
    entry = _check_entry_shape(
        item_id,
        {
            "title": title,
            "priority": priority,
            "status": "open",
            "claim": None,
            "needs": list(needs),
        },
    )
    for dep in entry.get("needs", []):
        validate_id(dep)
        if dep not in items:
            raise TodoError(f"task {item_id!r} needs unknown task {dep!r}", code=E_STATE)
    items[item_id] = entry
    detail: dict[str, Any] = {}
    if description:
        detail["description"] = description
    if acceptance:
        detail["acceptance"] = list(acceptance)
    if links:
        detail["links"] = list(links)
    if context:
        detail["context"] = context
    snapshot.details[item_id] = detail
    try:
        validate_snapshot(snapshot.index, snapshot.details)
    except TodoError:
        del items[item_id]
        del snapshot.details[item_id]
        raise
    return item_id


def op_update(
    snapshot: Snapshot,
    item_id: str,
    *,
    title: str | None = None,
    priority: str | None = None,
    description: str | None = None,
    needs: list[str] | tuple[str, ...] | None = None,
    acceptance: list[str] | tuple[str, ...] | None = None,
    links: list[str] | tuple[str, ...] | None = None,
    context: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    items = snapshot.index["items"]
    if item_id not in items:
        raise TodoError(f"unknown task {item_id!r}", code=E_STATE)
    entry = items[item_id]
    previous = json.loads(json.dumps(entry))
    previous_detail = json.loads(json.dumps(snapshot.details[item_id]))
    changed: list[str] = []
    if title is not None:
        entry["title"] = title
        changed.append("title")
    if priority is not None:
        entry["priority"] = priority
        changed.append("priority")
    if needs is not None:
        entry["needs"] = list(needs)
        changed.append("needs")
    if status is not None:
        if status in TERMINAL_STATUSES:
            raise TodoError(
                f"cannot move {item_id!r} to {status!r} via update; use finish/drop",
                code=E_STATE,
            )
        if status not in STATUSES:
            raise TodoError(f"unknown status {status!r}", code=E_STATE)
        if status != entry["status"] and status not in TRANSITIONS[entry["status"]]:
            raise TodoError(
                f"illegal status transition {entry['status']!r} -> {status!r} for {item_id!r}",
                code=E_STATE,
            )
        if status != entry["status"]:
            entry["status"] = status
            changed.append("status")
    detail = snapshot.details[item_id]
    if description is not None:
        if description:
            detail["description"] = description
        else:
            detail.pop("description", None)
        changed.append("description")
    if acceptance is not None:
        if list(acceptance):
            detail["acceptance"] = list(acceptance)
        else:
            detail.pop("acceptance", None)
        changed.append("acceptance")
    if links is not None:
        if list(links):
            detail["links"] = list(links)
        else:
            detail.pop("links", None)
        changed.append("links")
    if context is not None:
        if context:
            detail["context"] = context
        else:
            detail.pop("context", None)
        changed.append("context")
    try:
        validate_snapshot(snapshot.index, snapshot.details)
    except TodoError:
        items[item_id] = previous
        snapshot.details[item_id] = previous_detail
        raise
    return {"id": item_id, "changed": changed}


def _require_entry(snapshot: Snapshot, item_id: str) -> dict[str, Any]:
    try:
        return snapshot.index["items"][item_id]
    except KeyError:
        raise TodoError(f"unknown task {item_id!r}", code=E_STATE) from None


def op_take(
    snapshot: Snapshot,
    item_id: str,
    worker: str,
    *,
    ttl_hours: float = DEFAULT_TTL_HOURS,
    now: datetime | None = None,
) -> dict[str, Any]:
    worker = validate_worker(worker)
    entry = _require_entry(snapshot, item_id)
    moment = now or datetime.now(timezone.utc)
    claim = entry.get("claim")
    if claim_is_live(claim, moment):
        if claim["worker"] == worker:
            return {"id": item_id, "claim": claim, "adopted": True}
        raise TodoError(f"task {item_id!r} is claimed by {claim['worker']!r}", code=E_CONFLICT)
    if entry["status"] not in ("open", "blocked", "active"):
        raise TodoError(f"task {item_id!r} is {entry['status']} and cannot be taken", code=E_STATE)
    if not 0 < ttl_hours <= MAX_TTL_HOURS:
        raise TodoError(f"ttl must be within (0, {MAX_TTL_HOURS}] hours", code=E_STATE)
    fresh = {
        "worker": worker,
        "expires_at": (moment + timedelta(hours=ttl_hours)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generation": new_generation(),
    }
    entry["claim"] = fresh
    entry["status"] = "active"
    validate_snapshot(snapshot.index, snapshot.details)
    return {"id": item_id, "claim": fresh, "adopted": False}


def op_renew(
    snapshot: Snapshot,
    item_id: str,
    worker: str,
    generation: str,
    *,
    ttl_hours: float = DEFAULT_TTL_HOURS,
    now: datetime | None = None,
) -> dict[str, Any]:
    worker = validate_worker(worker)
    entry = _require_entry(snapshot, item_id)
    moment = now or datetime.now(timezone.utc)
    claim = entry.get("claim")
    if (
        not claim
        or claim.get("worker") != worker
        or claim.get("generation") != generation
    ):
        raise TodoError(f"stale claim for {item_id!r}: re-read with show and retry", code=E_CLAIM_STALE)
    if not 0 < ttl_hours <= MAX_TTL_HOURS:
        raise TodoError(f"ttl must be within (0, {MAX_TTL_HOURS}] hours", code=E_STATE)
    claim["expires_at"] = (moment + timedelta(hours=ttl_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    validate_snapshot(snapshot.index, snapshot.details)
    return {"id": item_id, "claim": dict(claim)}


def op_release(
    snapshot: Snapshot, item_id: str, worker: str, generation: str
) -> dict[str, Any]:
    worker = validate_worker(worker)
    entry = _require_entry(snapshot, item_id)
    claim = entry.get("claim")
    if (
        not claim
        or claim.get("worker") != worker
        or claim.get("generation") != generation
    ):
        raise TodoError(f"stale claim for {item_id!r}: cannot release another holder's task", code=E_CLAIM_STALE)
    entry["claim"] = None
    if entry["status"] == "active":
        entry["status"] = "open"
    validate_snapshot(snapshot.index, snapshot.details)
    return {"id": item_id, "status": entry["status"]}


def op_finish(
    snapshot: Snapshot, item_id: str, worker: str, generation: str
) -> dict[str, Any]:
    """Close a task. No work breakdown, scope gate, or attestation required."""
    worker = validate_worker(worker)
    entry = _require_entry(snapshot, item_id)
    claim = entry.get("claim")
    if (
        not claim
        or claim.get("worker") != worker
        or claim.get("generation") != generation
    ):
        raise TodoError(f"stale claim for {item_id!r}: only the holder can finish", code=E_CLAIM_STALE)
    entry["claim"] = None
    entry["status"] = "done"
    validate_snapshot(snapshot.index, snapshot.details)
    return {"id": item_id, "status": "done"}


def op_drop(snapshot: Snapshot, item_id: str, worker: str | None = None) -> dict[str, Any]:
    entry = _require_entry(snapshot, item_id)
    if entry["status"] in TERMINAL_STATUSES:
        raise TodoError(f"task {item_id!r} is already {entry['status']}", code=E_STATE)
    claim = entry.get("claim")
    if claim_is_live(claim) and (worker is None or claim.get("worker") != validate_worker(worker)):
        holder = claim["worker"] if claim else "?"
        raise TodoError(f"task {item_id!r} is claimed by {holder!r}", code=E_CONFLICT)
    entry["claim"] = None
    entry["status"] = "dropped"
    validate_snapshot(snapshot.index, snapshot.details)
    return {"id": item_id, "status": "dropped"}


def assert_quiescent(snapshot: Snapshot, now: datetime | None = None) -> None:
    """Refuse cutover/migration while live claims exist."""
    moment = now or datetime.now(timezone.utc)
    holders = sorted(
        {entry["claim"]["worker"] for entry in snapshot.index["items"].values() if claim_is_live(entry.get("claim"), moment)}
    )
    if holders:
        raise TodoError(
            f"refusing: live claims held by {', '.join(holders)}; "
            "wait for release/finish or expiry before cutover",
            code=E_ACTIVE_CLAIMS,
        )


def ready_rows(snapshot: Snapshot, now: datetime | None = None) -> list[tuple[str, dict[str, Any]]]:
    return query_items(snapshot, ready_only=True, now=now)


def require_ready(snapshot: Snapshot, now: datetime | None = None) -> list[tuple[str, dict[str, Any]]]:
    rows = ready_rows(snapshot, now)
    if not rows:
        raise TodoError("no ready tasks", code=E_NOTHING_READY)
    return rows
