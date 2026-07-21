"""Parity fixtures derived from BenchBox ``todo_db.py:1816-2120``.

Expected values pin the legacy YAML import contract, not current standalone output.
"""

from __future__ import annotations

from pathlib import Path

from todo_db import DatabaseConfig, ProjectIdentity, TodoDatabase, TodoTracker


def _tracker(tmp_path: Path) -> tuple[TodoDatabase, TodoTracker]:
    database = TodoDatabase.open(
        DatabaseConfig(
            path=tmp_path / "standalone.sqlite",
            identity=ProjectIdentity(project_id="parity", repository="todo-db"),
        )
    )
    return database, TodoTracker(database, actor="parity-test")


def test_import_preserves_legacy_policy_and_timestamps(tmp_path: Path) -> None:
    """BenchBox todo_db.py:1933-1998 preserves policy fields and dates."""
    todo_dir = tmp_path / "project" / "TODO"
    todo_dir.mkdir(parents=True)
    (todo_dir / "source.yaml").write_text(
        """id: Source Item
title: Source item
worktree: feature/source
priority: URGENT
description: Source description for importer parity.
status: Blocked
metadata:
  created_date: 2025-01-02
work:
  - id: w0
    summary: Preserve importer fields
    status: pending
    notes: keep this note
verification:
  - description: focused test
    command: uv run -- python -m pytest -q
    expected_output: exit 0
  - manual verification
must_preserve: [legacy output]
anti_patterns:
  - DO NOT drop fields -- because parity fails -- preserve them instead
prior_art:
  - "README.md: extend the documented contract"
deferred:
  - summary: Follow up later
    reason: Outside this migration
""",
        encoding="utf-8",
    )

    database, tracker = _tracker(tmp_path)
    report = tracker.import_yaml_tree(todo_dir)
    item = tracker.get_item("source-item")

    assert report["warnings"] == [
        f"{todo_dir / 'source.yaml'}: id 'Source Item' sanitized to 'source-item'",
        f"{todo_dir / 'source.yaml'}: unknown priority 'urgent'; using medium",
    ]
    assert item["priority"] == "medium"
    assert item["state"] == "active"
    assert item["blocked_reason"] == "imported from YAML status: Blocked"
    assert item["created_at"] == "2025-01-02T00:00:00Z"
    assert item["work"][0]["notes"] == "keep this note"
    assert item["verifications"][0]["expected"] == "exit 0"
    assert item["verifications"][1]["description"] == "manual verification"
    assert item["anti_patterns"] == [{"dont": "drop fields", "why": "parity fails", "instead": "preserve them instead"}]
    assert item["prior_art"] == [
        {"path": "README.md", "concept": "README.md: extend the documented contract", "decision": "extend"}
    ]
    assert item["deferrals"][0]["summary"] == "Follow up later"
    actions = [row["action"] for row in database.export()["events"]]
    assert actions.index("create") < actions.index("defer")
    database.close()


def test_import_matches_legacy_dependency_and_archive_rules(tmp_path: Path) -> None:
    """BenchBox todo_db.py:1886-1930,2005-2117 pins archive and dependency semantics."""
    todo_dir = tmp_path / "project" / "TODO"
    done_dir = tmp_path / "project" / "DONE"
    todo_dir.mkdir(parents=True)
    done_dir.mkdir(parents=True)
    (todo_dir / "a-dependent.yaml").write_text(
        """id: shared-id
title: Open shared item
worktree: feature/open
priority: high
description: Open item that depends on a later file.
deps:
  needs: [z-target]
dependencies:
  blocked_by: [archive/z-target.yaml]
""",
        encoding="utf-8",
    )
    (todo_dir / "z-target.yaml").write_text(
        """id: z-target
title: Dependency target
worktree: feature/target
priority: medium
description: Target imported after its dependent.
""",
        encoding="utf-8",
    )
    (done_dir / "shared-id.yaml").write_text(
        """id: shared-id
status: In Progress
completed_date: 2024-06-03
work:
  - id: invalid
    summary: x
    needs: [missing]
deferred:
  - summary: Archived follow-up
    reason: Keep provenance
""",
        encoding="utf-8",
    )

    database, tracker = _tracker(tmp_path)
    report = tracker.import_yaml_tree(todo_dir, done_dir)

    assert report["counts"]["deps_resolved"] == 1
    assert report["counts"]["legacy_dependencies_field"] == 1
    assert tracker.get_item("shared-id")["deps"] == ["z-target"]
    archived = tracker.get_item("shared-id-archived")
    assert archived["state"] == "done"
    assert archived["completed_at"] == "2024-06-03T00:00:00Z"
    assert archived["title"] == "Archived item shared-id"
    assert archived["work"] == []
    assert archived["deferrals"][0]["summary"] == "Archived follow-up"
    assert any("tree/status drift" in warning for warning in report["warnings"])
    assert any("invalid archive work unit" in warning for warning in report["warnings"])
    database.close()


def test_open_item_with_invalid_work_is_skipped(tmp_path: Path) -> None:
    """BenchBox todo_db.py:1886-1917 is strict for open-tree work graphs."""
    todo_dir = tmp_path / "TODO"
    todo_dir.mkdir()
    source = todo_dir / "invalid.yaml"
    source.write_text(
        """id: invalid-work
title: Invalid work item
worktree: local
priority: medium
description: This open item has an invalid work id.
work:
  - id: invalid
    summary: Invalid unit identifier
""",
        encoding="utf-8",
    )
    database, tracker = _tracker(tmp_path)
    report = tracker.import_yaml_tree(todo_dir)
    assert report["imported"] == []
    assert report["skipped"] == [str(source)]
    assert database.export()["tables"]["items"] == []
    database.close()


def test_nested_yaml_uses_legacy_parent_worktree_fallback(tmp_path: Path) -> None:
    """BenchBox todo_db.py:1964 uses each file's grandparent, not the import root."""
    todo_dir = tmp_path / "project" / "TODO"
    nested = todo_dir / "category"
    nested.mkdir(parents=True)
    (nested / "item.yaml").write_text(
        """id: nested-item
title: Nested item
priority: medium
description: Nested item with legacy worktree fallback.
""",
        encoding="utf-8",
    )
    database, tracker = _tracker(tmp_path)
    tracker.import_yaml_tree(todo_dir)
    assert tracker.get_item("nested-item")["worktree"] == "TODO"
    database.close()
