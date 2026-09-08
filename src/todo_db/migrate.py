"""Non-destructive migration from the legacy SQLite/export runtime.

Preferred input is the lossless export envelope (``format_version`` 2)
produced by the pre-simplification package (``todo-db export`` under
v0.6.x). The new runtime keeps no libSQL or hosted credential machinery:
SQLite files are not read directly. Export with the old release first;
this command only reads the envelope and never modifies it.

Mapping:

- IDs, titles, priorities, dependencies, and descriptions are preserved.
- ``planning`` becomes ``open``; ``active`` becomes ``blocked`` when it
  carries a blocked reason, else ``open``; ``done``/``dropped`` carry over.
  Live claims are never carried: any unexpired claim refuses the cutover
  with an actionable error (``E_ACTIVE_CLAIMS``).
- Work units, work needs, scope rules, verifications, preserves,
  anti-patterns, prior art, and deferrals move to per-item
  ``detail["legacy"]`` so multi-step plans and evidence stay recoverable.
- Global tables (findings family, events, audit head, metadata) are not
  dropped silently: the input envelope is preserved untouched, a backup
  copy is written next to the report, and every table is accounted for in
  the report with row counts.

Historical audit data remains an immutable legacy artifact (the preserved
envelope). Git history on the state branch is ordinary change history and
does not recreate audit-chain guarantees.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import git_backend
from . import store as S
from .errors import E_ACTIVE_CLAIMS, E_CONFLICT, E_STATE, TodoError

EXPORT_FORMAT_VERSION = 2
LEASE_TTL_HOURS = 24.0

STATUS_MAP = {
    "planning": "open",
    "active": "open",
    "done": "done",
    "dropped": "dropped",
}

#: Source tables grouped by where their rows land. Every table in a v2
#: envelope must appear here; anything else fails loudly instead of being
#: silently discarded.
ITEM_TABLES = frozenset({
    "items", "work_units", "work_needs", "item_deps", "scope_rules",
    "verifications", "preserves", "anti_patterns", "prior_art", "deferrals",
})
GLOBAL_TABLES = frozenset({
    "project_identity", "metadata", "meta", "events", "audit_head",
    "schema_migrations", "findings", "finding_evidence", "finding_links",
    "finding_events", "finding_sections",
})


def _lease_live(claimed_at: str | None, now: datetime) -> bool:
    if not claimed_at:
        return False
    try:
        when = S.parse_ts(claimed_at)
    except TodoError:
        return True  # Unparseable claim timestamp: treat as live, refuse.
    return (now - when).total_seconds() < LEASE_TTL_HOURS * 3600


def _rows(export: dict[str, Any], table: str) -> list[dict[str, Any]]:
    tables = export.get("tables") or {}
    rows = tables.get(table, [])
    if not isinstance(rows, list):
        raise TodoError(f"export table {table!r} must be a list", code=E_STATE)
    return [dict(row) for row in rows]


def migrate_export(export: dict[str, Any], *, now: datetime | None = None) -> tuple[S.Snapshot, dict[str, Any]]:
    """Map a lossless export envelope to a new snapshot plus a report."""
    moment = now or datetime.now(timezone.utc)
    if export.get("format_version") != EXPORT_FORMAT_VERSION:
        raise TodoError(
            f"unsupported export format {export.get('format_version')!r}: expected {EXPORT_FORMAT_VERSION}",
            code=E_STATE,
        )
    tables = export.get("tables") or {}
    unknown = set(tables) - ITEM_TABLES - GLOBAL_TABLES
    if unknown:
        raise TodoError(
            f"export carries unknown tables that migration cannot account for: {sorted(unknown)}",
            code=E_STATE,
        )
    items = _rows(export, "items")
    item_ids = set()
    for row in items:
        if not isinstance(row, dict):
            raise TodoError("export items rows must be objects", code=E_STATE)
        item_ids.add(str(row.get("id", "")))
    deps: dict[str, list[str]] = {}
    for row in _rows(export, "item_deps"):
        source, target = str(row.get("item_id", "")), str(row.get("needs_item", ""))
        if source not in item_ids:
            raise TodoError(f"export dependency from unknown item {source!r}", code=E_STATE)
        if target not in item_ids:
            raise TodoError(f"export dependency on unknown item {target!r}", code=E_STATE)
        deps.setdefault(source, []).append(target)
    by_item: dict[str, dict[str, list[dict[str, Any]]]] = {}
    scoped = (
        ("work_units", "item_id"), ("work_needs", "item_id"), ("scope_rules", "item_id"),
        ("verifications", "item_id"), ("preserves", "item_id"), ("anti_patterns", "item_id"),
        ("prior_art", "item_id"), ("deferrals", "from_item"),
    )
    for table, key in scoped:
        for row in _rows(export, table):
            by_item.setdefault(str(row.get(key, "")), {}).setdefault(table, []).append(row)

    snapshot = S.Snapshot(index=S.empty_index(), details={})
    warnings: list[str] = []
    live_holders: list[str] = []
    for row in items:
        item_id = str(row.get("id", ""))
        S.validate_id(item_id)
        title = str(row.get("title", "") or "")
        if not title.strip():
            raise TodoError(f"export item {item_id!r} has an empty title", code=E_STATE)
        priority = str(row.get("priority", "medium") or "medium").strip().lower()
        if priority not in S.PRIORITIES:
            warnings.append(f"{item_id}: unknown priority {priority!r}; using medium")
            priority = "medium"
        old_state = str(row.get("state", "planning"))
        if old_state not in STATUS_MAP:
            raise TodoError(f"export item {item_id!r} has unknown state {old_state!r}", code=E_STATE)
        status = STATUS_MAP[old_state]
        if old_state == "active" and str(row.get("blocked_reason") or "").strip():
            status = "blocked"
        if row.get("claimed_by") and _lease_live(str(row.get("claimed_at") or ""), moment):
            live_holders.append(f"{item_id} (held by {row.get('claimed_by')})")
        needs = sorted(set(deps.get(item_id, [])))
        S.op_create(
            snapshot,
            item_id=item_id,
            title=title,
            priority=priority,
            description=str(row.get("description") or ""),
            needs=needs,
            context=str(row.get("approach") or ""),
        )
        snapshot.index["items"][item_id]["status"] = status
        legacy: dict[str, Any] = {}
        extra = by_item.get(item_id, {})
        for table in ("work_units", "work_needs", "scope_rules", "verifications", "preserves", "anti_patterns", "prior_art"):
            if extra.get(table):
                legacy[table] = extra[table]
        if extra.get("deferrals"):
            legacy["deferrals"] = extra["deferrals"]
        # Archive every source column that has no canonical home. The mapped
        # set below is exhaustive by construction: anything not mapped here
        # lands in item_meta, so no column is silently discarded.
        mapped = {"id", "title", "priority", "state", "description", "approach"}
        for key, value in row.items():
            if key not in mapped and value is not None:
                legacy.setdefault("item_meta", {})[key] = value
        # Claims are never carried into the new system.
        if legacy.get("item_meta", {}).get("claimed_by") and not _lease_live(
            str(row.get("claimed_at") or ""), moment
        ):
            warnings.append(f"{item_id}: dropped an expired claim by {row.get('claimed_by')}")
        if legacy:
            snapshot.details[item_id]["legacy"] = legacy
    if live_holders:
        raise TodoError(
            "refusing cutover with live claims: " + "; ".join(sorted(live_holders)) + ". "
            "Wait for release/finish or expiry, export again, and retry.",
            code=E_ACTIVE_CLAIMS,
        )
    S.validate_snapshot(snapshot.index, snapshot.details)
    global_counts = {table: len(_rows(export, table)) for table in sorted(GLOBAL_TABLES) if table in tables}
    item_counts = {table: len(_rows(export, table)) for table in sorted(ITEM_TABLES) if table in tables}
    report = {
        "items": len(items),
        "statuses": {s: sum(1 for e in snapshot.index["items"].values() if e["status"] == s) for s in S.STATUSES},
        "warnings": warnings,
        "item_tables": item_counts,
        "global_tables_preserved_in_backup": global_counts,
        "project": export.get("project"),
    }
    return snapshot, report


def migrate_file(
    input_path: str | Path,
    ref: git_backend.StateRef,
    *,
    worker: str,
    dry_run: bool = False,
    backup_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Migrate an export file onto a freshly bootstrapped state branch."""
    import hashlib as _hashlib

    source = Path(input_path)
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise TodoError(f"cannot read export file {source}: {exc}", code=E_STATE) from exc
    try:
        export = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TodoError(f"invalid export file {source}: {exc}", code=E_STATE) from exc
    snapshot, report = migrate_export(export)
    report = dict(report)
    report["dry_run"] = dry_run
    report["source_sha256"] = _hashlib.sha256(raw).hexdigest()
    if backup_dir is not None:
        dest_dir = Path(backup_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        backup = dest_dir / (source.name + ".backup")
        shutil.copy2(source, backup)
        report["backup"] = str(backup)
    elif not dry_run:
        # Global tables survive only as row counts unless the source envelope
        # is preserved durably. Applying without a backup would make the
        # "preserved in backup" report line a lie, so refuse instead.
        raise TodoError(
            "refusing to apply migration without --backup-dir: the source envelope "
            "is the durable archive for findings, events, and audit data",
            code=E_STATE,
        )
    if dry_run:
        report["applied"] = False
        return report

    def apply(snap: S.Snapshot) -> dict[str, Any]:
        if snap.index["items"]:
            raise TodoError(
                "refusing migration onto a non-empty state branch; "
                "migrate onto a fresh bootstrap or restore it first",
                code=E_CONFLICT,
            )
        snap.index = json.loads(json.dumps(snapshot.index))
        snap.details.clear()
        snap.details.update(json.loads(json.dumps(snapshot.details)))
        return {"id": f"{report['items']}-items"}

    outcome = git_backend.mutate(
        ref, op="migrate", summary=f"migrate {report['items']} items from legacy export",
        worker=worker, apply=apply,
    )
    if not outcome.ok:
        raise TodoError(outcome.error or "migration publication failed", code=outcome.code or E_STATE)
    report["applied"] = True
    report["rev"] = outcome.sha
    report["rollback"] = (
        f"migration landed as {outcome.sha}; before adoption, roll back with "
        f"`git push <remote> --delete {ref.branch}` and re-bootstrap; after adoption, "
        "restore a prior revision with `todo-db recover --restore-rev <sha>` (append-only)."
    )
    return report
