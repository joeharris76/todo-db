"""Migration proving cases against synthetic v2 export envelopes."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from todo_db import git_backend
from todo_db.errors import TodoError
from todo_db.migrate import migrate_export, migrate_file


def _export(**overrides) -> dict:
    base = {
        "format_version": 2,
        "project": {"project_id": "p", "repository": "https://example.test/p"},
        "tables": {
            "items": [
                {
                    "id": "alpha", "title": "Alpha", "worktree": "todo-db",
                    "priority": "high", "state": "planning", "blocked_reason": None,
                    "category": "feature", "description": "Do alpha",
                    "approach": "Carefully", "claimed_by": None, "claimed_at": None,
                    "claimed_session": None, "claim_token": None,
                    "claimed_branch": None, "claimed_worktree": None,
                    "git_baseline": None, "created_at": "2026-01-01T00:00:00Z",
                    "completed_at": None, "completed_pr": None,
                },
                {
                    "id": "beta", "title": "Beta", "worktree": "todo-db",
                    "priority": "weird", "state": "active", "blocked_reason": "waiting",
                    "category": "feature", "description": "Do beta",
                    "approach": None, "claimed_by": None, "claimed_at": None,
                    "claimed_session": None, "claim_token": None,
                    "claimed_branch": None, "claimed_worktree": None,
                    "git_baseline": None, "created_at": "2026-01-02T00:00:00Z",
                    "completed_at": None, "completed_pr": None,
                },
            ],
            "item_deps": [{"item_id": "beta", "needs_item": "alpha"}],
            "work_units": [
                {"item_id": "alpha", "wid": "w0", "summary": "Step 0",
                 "status": "done", "evidence": "commit 1", "notes": None,
                 "started_at": None, "completed_at": None},
            ],
            "work_needs": [],
            "scope_rules": [
                {"item_id": "alpha", "kind": "only_modify", "path_glob": "src/**"},
            ],
            "verifications": [
                {"item_id": "alpha", "seq": 1, "description": "tests",
                 "command": "pytest -q", "expected_output": None, "last_status": "pass"},
            ],
            "preserves": [],
            "anti_patterns": [],
            "prior_art": [],
            "deferrals": [
                {"id": 1, "from_item": "alpha", "summary": "later",
                 "reason": "time", "created_at": "2026-01-03T00:00:00Z"},
            ],
            "meta": [],
            "findings": [],
            "finding_evidence": [],
            "finding_links": [],
            "finding_events": [],
            "finding_sections": [],
            "events": [{"seq": 1}],
            "metadata": [],
            "audit_head": [{"singleton": 1, "head_seq": 1, "head_hash": "abc"}],
            "schema_migrations": [{"version": 7}],
            "project_identity": [{"singleton": 1, "project_id": "p", "repository": "https://example.test/p"}],
        },
    }
    base.update(overrides)
    return base


def test_migration_preserves_mapping_and_archive() -> None:
    snapshot, report = migrate_export(_export())
    assert set(snapshot.index["items"]) == {"alpha", "beta"}
    assert snapshot.index["items"]["beta"]["needs"] == ["alpha"]
    assert snapshot.index["items"]["beta"]["status"] == "blocked"
    assert snapshot.index["items"]["beta"]["priority"] == "medium"
    assert any("weird" in w for w in report["warnings"])
    legacy = snapshot.details["alpha"]["legacy"]
    assert legacy["work_units"][0]["evidence"] == "commit 1"
    assert legacy["scope_rules"][0]["path_glob"] == "src/**"
    assert legacy["verifications"][0]["command"] == "pytest -q"
    assert legacy["deferrals"][0]["summary"] == "later"
    assert report["global_tables_preserved_in_backup"]["events"] == 1
    # No live claims carried.
    assert all(e.get("claim") is None for e in snapshot.index["items"].values())


def test_migration_wires_forward_referenced_dependencies() -> None:
    # Export rows are not topologically ordered: a dependent may appear
    # before the task it needs. Migration must still wire the edge.
    export = _export()
    export["tables"]["items"].reverse()  # beta (needs alpha) now comes first
    snapshot, _ = migrate_export(export)
    assert snapshot.index["items"]["beta"]["needs"] == ["alpha"]


def test_migration_preserves_legacy_ids_longer_than_64_characters() -> None:
    export = _export()
    long_id = "adversarial-review-cost-framework-enhancement-warnings-metadata-timing-storage"
    export["tables"]["items"] = [dict(export["tables"]["items"][0], id=long_id)]
    export["tables"]["item_deps"] = []
    for table, key in {
        "work_units": "item_id",
        "scope_rules": "item_id",
        "verifications": "item_id",
        "deferrals": "from_item",
    }.items():
        for row in export["tables"][table]:
            if row[key] == "alpha":
                row[key] = long_id

    snapshot, _ = migrate_export(export)

    assert set(snapshot.index["items"]) == {long_id}
    assert snapshot.details[long_id]["description"] == "Do alpha"


def test_migration_refuses_live_claim_cutover() -> None:
    export = _export()
    export["tables"]["items"][0]["claimed_by"] = "someone"
    export["tables"]["items"][0]["claimed_at"] = "2999-01-01T00:00:00Z"
    export["tables"]["items"][0]["claim_token"] = "tok"
    with pytest.raises(TodoError) as excinfo:
        migrate_export(export)
    assert excinfo.value.code == "E_ACTIVE_CLAIMS"


def test_migration_refuses_unknown_tables() -> None:
    export = _export()
    export["tables"]["mystery"] = [{"a": 1}]
    with pytest.raises(TodoError):
        migrate_export(export)


def test_migration_archives_every_unmapped_column() -> None:
    export = _export()
    export["tables"]["items"][0]["future_column"] = "keep me"
    export["tables"]["items"][0]["another_one"] = 42
    snapshot, _ = migrate_export(export)
    meta = snapshot.details["alpha"]["legacy"]["item_meta"]
    assert meta["future_column"] == "keep me"
    assert meta["another_one"] == 42
    assert meta["category"] == "feature"


def test_migration_rejects_dangling_dependencies() -> None:
    export = _export()
    export["tables"]["item_deps"].append({"item_id": "beta", "needs_item": "ghost"})
    with pytest.raises(TodoError):
        migrate_export(export)


def test_migration_archives_dependency_row_extras() -> None:
    export = _export()
    export["tables"]["item_deps"][0]["source_note"] = "why beta waits"
    snapshot, _ = migrate_export(export)
    assert snapshot.details["beta"]["legacy"]["item_deps"][0]["source_note"] == "why beta waits"


def test_migration_refuses_non_object_rows() -> None:
    export = _export()
    export["tables"]["items"].append(["not", "an", "object"])
    with pytest.raises(TodoError):
        migrate_export(export)


def test_migrate_file_dry_run_and_apply(tmp_path: Path) -> None:
    source = tmp_path / "legacy-export.json"
    source.write_text(json.dumps(_export()), encoding="utf-8")
    remote = tmp_path / "state.git"
    subprocess.run(["git", "init", "--quiet", "--bare", str(remote)], check=True)
    ref = git_backend.StateRef(remote=str(remote), branch="todo-state")
    git_backend.bootstrap(ref)
    dry = migrate_file(source, ref, worker="w", dry_run=True, backup_dir=tmp_path / "bak")
    assert dry["dry_run"] and not dry["applied"]
    assert dry["items"] == 2
    assert (tmp_path / "bak" / "legacy-export.json.backup").is_file()
    # Source unchanged.
    assert json.loads(source.read_text(encoding="utf-8"))["format_version"] == 2
    applied = migrate_file(source, ref, worker="w", backup_dir=tmp_path / "bak")
    assert applied["applied"] and applied["rev"]
    assert "rollback" in applied
    ro = git_backend.read(ref, tmp_path / "cache")
    assert set(ro.snapshot.index["items"]) == {"alpha", "beta"}
    # Second migration onto non-empty branch refuses.
    with pytest.raises(TodoError):
        migrate_file(source, ref, worker="w")
