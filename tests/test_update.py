from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from todo_db import DatabaseConfig, ProjectIdentity, TodoDatabase, TodoTracker
from todo_db.errors import TodoError


IDENTITY = ProjectIdentity(project_id="update-test", repository="https://example.test/update")


def open_tracker(tmp_path: Path) -> tuple[TodoDatabase, TodoTracker]:
    database = TodoDatabase.open(DatabaseConfig(path=tmp_path / "todo.sqlite", identity=IDENTITY))
    return database, TodoTracker(database, actor="test-actor")


def create_basic_item(tracker: TodoTracker, **overrides: Any) -> str:
    payload: dict[str, Any] = {
        "item_id": "update-item",
        "title": "Original title",
        "worktree": "todo-db",
        "priority": "medium",
        "description": "The original description before any update.",
        "work": [{"id": "w0", "summary": "Original pending unit"}],
        "verifications": [{"description": "Tests", "command": "printf PASS", "expected": "PASS"}],
    }
    payload.update(overrides)
    tracker.create_item(**payload)
    return str(payload["item_id"])


def test_metadata_updates_carry_exact_from_to_diffs(tmp_path: Path) -> None:
    database, tracker = open_tracker(tmp_path)
    try:
        create_basic_item(tracker)
        detail = tracker.update_item(
            "update-item",
            title="Corrected title",
            description="The corrected description after review.",
            priority="high",
            worktree="other-tree",
        )
        item = tracker.get_item("update-item")
        assert item["title"] == "Corrected title"
        assert item["description"] == "The corrected description after review."
        assert item["priority"] == "high"
        assert item["worktree"] == "other-tree"
        assert item["state"] == "planning"
        event = database.export()["events"][-1]
        assert event["action"] == "update"
        assert event["detail"]["item_id"] == "update-item"
        assert (
            event["detail"]["changes"]
            == detail["changes"]
            == {
                "title": {"from": "Original title", "to": "Corrected title"},
                "description": {
                    "from": "The original description before any update.",
                    "to": "The corrected description after review.",
                },
                "priority": {"from": "medium", "to": "high"},
                "worktree": {"from": "todo-db", "to": "other-tree"},
            }
        )
        assert "reason" not in event["detail"]
    finally:
        database.close()


def test_update_validates_fields_exactly_like_create(tmp_path: Path) -> None:
    database, tracker = open_tracker(tmp_path)
    try:
        create_basic_item(tracker)
        with pytest.raises(TodoError, match="title must be between"):
            tracker.update_item("update-item", title="tiny")
        with pytest.raises(TodoError, match="invalid priority"):
            tracker.update_item("update-item", priority="urgent")
        with pytest.raises(TodoError, match="worktree must not be empty"):
            tracker.update_item("update-item", worktree="   ")
        with pytest.raises(TodoError, match="description must be at least"):
            tracker.update_item("update-item", description="short")
        assert tracker.get_item("update-item")["title"] == "Original title"
    finally:
        database.close()


def test_terminal_item_edits_require_a_reason_recorded_in_the_event(tmp_path: Path) -> None:
    database, tracker = open_tracker(tmp_path)
    try:
        create_basic_item(tracker)
        tracker.claim("update-item")
        tracker.done_unit("update-item", "w0", "completion evidence")
        tracker.complete("update-item")
        with pytest.raises(TodoError, match="requires --reason"):
            tracker.update_item("update-item", title="Post-completion title")
        tracker.update_item("update-item", title="Post-completion title", reason="typo found in review")
        event = database.export()["events"][-1]
        assert event["action"] == "update"
        assert event["detail"]["reason"] == "typo found in review"
        item = tracker.get_item("update-item")
        assert item["title"] == "Post-completion title"
        assert item["state"] == "done"
    finally:
        database.close()


def test_edit_work_applies_only_to_pending_units_and_logs_from_to(tmp_path: Path) -> None:
    database, tracker = open_tracker(tmp_path)
    try:
        create_basic_item(
            tracker,
            work=[{"id": "w0", "summary": "Original pending unit"}, {"id": "w1", "summary": "Unit to finish"}],
        )
        detail = tracker.update_item("update-item", edit_work={"w0": "Corrected pending unit"})
        assert detail["work_edited"] == {"w0": {"from": "Original pending unit", "to": "Corrected pending unit"}}
        assert database.export()["events"][-1]["detail"]["work_edited"] == detail["work_edited"]
        assert tracker.get_item("update-item")["work"][0]["summary"] == "Corrected pending unit"
        tracker.claim("update-item")
        tracker.done_unit("update-item", "w1", "unit evidence")
        with pytest.raises(TodoError, match="only pending units"):
            tracker.update_item("update-item", edit_work={"w1": "Rewriting a done unit"})
        assert tracker.get_item("update-item")["work"][1]["summary"] == "Unit to finish"
    finally:
        database.close()


def test_add_work_extends_the_breakdown_and_rejects_duplicates(tmp_path: Path) -> None:
    database, tracker = open_tracker(tmp_path)
    try:
        create_basic_item(tracker)
        detail = tracker.update_item(
            "update-item", add_work=[{"id": "w1", "summary": "Mid-batch discovered unit", "needs": ["w0"]}]
        )
        assert detail["work_added"] == [{"id": "w1", "summary": "Mid-batch discovered unit", "needs": ["w0"]}]
        unit = tracker.get_item("update-item")["work"][1]
        assert (unit["wid"], unit["status"], unit["needs"]) == ("w1", "pending", ["w0"])
        with pytest.raises(TodoError, match="duplicate work-unit id"):
            tracker.update_item("update-item", add_work=[{"id": "w0", "summary": "Colliding unit"}])
    finally:
        database.close()


def test_verification_amendments_log_full_command_text(tmp_path: Path) -> None:
    database, tracker = open_tracker(tmp_path)
    try:
        create_basic_item(tracker)
        detail = tracker.update_item(
            "update-item", add_verify=[{"description": "Lint", "command": "uv run ruff check ."}]
        )
        assert detail["verify_added"] == [{"seq": 2, "name": "Lint", "command": "uv run ruff check ."}]
        with pytest.raises(TodoError, match="--reason is required"):
            tracker.update_item("update-item", drop_verify=[1])
        detail = tracker.update_item("update-item", drop_verify=[1], reason="superseded by the lint step")
        assert detail["verify_dropped"] == [{"seq": 1, "name": "Tests", "command": "printf PASS"}]
        events = database.export()["events"]
        assert events[-2]["detail"]["verify_added"][0]["command"] == "uv run ruff check ."
        assert events[-1]["detail"]["verify_dropped"][0]["command"] == "printf PASS"
        assert events[-1]["detail"]["reason"] == "superseded by the lint step"
        assert [row["seq"] for row in tracker.get_item("update-item")["verifications"]] == [2]
        with pytest.raises(TodoError, match="no verification seq=9"):
            tracker.update_item("update-item", drop_verify=[9], reason="no such row")
    finally:
        database.close()


def test_scope_amendments_require_reason_and_log_exact_rules(tmp_path: Path) -> None:
    database, tracker = open_tracker(tmp_path)
    try:
        create_basic_item(
            tracker,
            scope=[("only_modify", "src/**"), ("do_not_modify", "src/generated/**")],
        )
        with pytest.raises(TodoError, match="scope changes require --reason"):
            tracker.update_item("update-item", add_scope=[("only_modify", "tests/**")])

        detail = tracker.update_item(
            "update-item",
            add_scope=[("only_modify", "tests/**")],
            drop_scope=[("do_not_modify", "src/generated/**")],
            reason="the verification suite and generated output are now part of the reviewed boundary",
        )

        assert detail["scope_added"] == [{"kind": "only_modify", "path_glob": "tests/**"}]
        assert detail["scope_dropped"] == [{"kind": "do_not_modify", "path_glob": "src/generated/**"}]
        assert tracker.get_item("update-item")["scope"] == [
            {"kind": "only_modify", "path_glob": "src/**"},
            {"kind": "only_modify", "path_glob": "tests/**"},
        ]
        event = database.export()["events"][-1]
        assert event["action"] == "update"
        assert event["detail"]["scope_added"] == detail["scope_added"]
        assert event["detail"]["scope_dropped"] == detail["scope_dropped"]
        assert event["detail"]["reason"].startswith("the verification suite")
    finally:
        database.close()


def test_scope_amendments_validate_atomically(tmp_path: Path) -> None:
    database, tracker = open_tracker(tmp_path)
    try:
        create_basic_item(tracker, scope=[("only_modify", "src/**")])
        events_before = len(database.export()["events"])

        with pytest.raises(TodoError, match="no scope rule"):
            tracker.update_item(
                "update-item",
                add_scope=[("only_modify", "tests/**")],
                drop_scope=[("do_not_modify", "missing/**")],
                reason="exercise rollback",
            )

        assert tracker.get_item("update-item")["scope"] == [{"kind": "only_modify", "path_glob": "src/**"}]
        assert len(database.export()["events"]) == events_before
    finally:
        database.close()


def test_scope_amendments_reject_duplicate_conflicting_empty_and_existing_rules(tmp_path: Path) -> None:
    database, tracker = open_tracker(tmp_path)
    try:
        create_basic_item(tracker, scope=[("only_modify", "src/**")])
        events_before = len(database.export()["events"])

        invalid_updates = [
            ({"add_scope": [("only_modify", "tests/**"), ("only_modify", "tests/**")]}, "more than once"),
            (
                {
                    "add_scope": [("only_modify", "tests/**")],
                    "drop_scope": [("only_modify", "tests/**")],
                },
                "add and drop the same scope rule",
            ),
            ({"add_scope": [("only_modify", "   ")]}, "must not be empty"),
            ({"add_scope": [("only_modify", "src/**")]}, "already exists"),
        ]
        for kwargs, message in invalid_updates:
            with pytest.raises(TodoError, match=message):
                tracker.update_item("update-item", reason="exercise validation", **kwargs)

        assert tracker.get_item("update-item")["scope"] == [{"kind": "only_modify", "path_glob": "src/**"}]
        assert len(database.export()["events"]) == events_before
    finally:
        database.close()


def test_optional_metadata_updates_and_clears_log_from_to(tmp_path: Path) -> None:
    database, tracker = open_tracker(tmp_path)
    try:
        create_basic_item(tracker, approach="Ship the first slice", category="code")
        detail = tracker.update_item("update-item", approach="Ship the remaining fields", category="docs")
        item = tracker.get_item("update-item")
        assert item["approach"] == "Ship the remaining fields"
        assert item["category"] == "docs"
        assert detail["changes"] == {
            "approach": {"from": "Ship the first slice", "to": "Ship the remaining fields"},
            "category": {"from": "code", "to": "docs"},
        }
        cleared = tracker.update_item("update-item", approach="", category="   ")
        assert cleared["changes"] == {
            "approach": {"from": "Ship the remaining fields", "to": None},
            "category": {"from": "docs", "to": None},
        }
        cleared_item = tracker.get_item("update-item")
        assert cleared_item["approach"] is None
        assert cleared_item["category"] is None
        with pytest.raises(TodoError, match="nothing to update"):
            tracker.update_item("update-item", approach="")
    finally:
        database.close()


def test_item_dependency_amendments_enforce_claim_and_reject_cycles(tmp_path: Path) -> None:
    database, tracker = open_tracker(tmp_path)
    try:
        create_basic_item(tracker)
        create_basic_item(
            tracker,
            item_id="other-item",
            title="Other item",
            description="A second item used as a dependency target.",
        )
        detail = tracker.update_item("update-item", add_deps=["other-item"])
        assert detail["deps_added"] == ["other-item"]
        assert tracker.get_item("update-item")["deps"] == ["other-item"]
        with pytest.raises(TodoError, match="unmet dependencies"):
            tracker.claim("update-item")
        with pytest.raises(TodoError, match="--reason is required"):
            tracker.update_item("update-item", drop_deps=["other-item"])
        tracker.update_item("update-item", drop_deps=["other-item"], reason="the other item is no longer a gate")
        assert tracker.get_item("update-item")["deps"] == []
        tracker.update_item("update-item", add_deps=["other-item"])
        with pytest.raises(TodoError, match="item dependency cycle"):
            tracker.update_item("other-item", add_deps=["update-item"])
        with pytest.raises(TodoError, match="already exists"):
            tracker.update_item("update-item", add_deps=["other-item"])
        with pytest.raises(TodoError, match="missing item"):
            tracker.update_item("update-item", add_deps=["missing-item"])
    finally:
        database.close()


def test_guardrail_and_work_need_amendments_are_atomic_and_audited(tmp_path: Path) -> None:
    database, tracker = open_tracker(tmp_path)
    try:
        create_basic_item(
            tracker,
            work=[
                {"id": "w0", "summary": "Original pending unit"},
                {"id": "w1", "summary": "Second pending unit"},
            ],
            preserves=["keep the export envelope"],
            anti_patterns=[{"dont": "rewrite history", "why": "audit breaks", "instead": "amend in place"}],
            prior_art=[{"path": "README.md", "concept": "documented update verb", "decision": "extend"}],
        )
        events_before = len(database.export()["events"])
        with pytest.raises(TodoError, match="--reason is required"):
            tracker.update_item("update-item", drop_preserves=["keep the export envelope"])
        with pytest.raises(TodoError, match="no preserve"):
            tracker.update_item(
                "update-item",
                add_preserves=["keep claim blocking"],
                drop_preserves=["missing preserve"],
                reason="exercise rollback",
            )
        assert tracker.get_item("update-item")["preserves"] == ["keep the export envelope"]
        assert len(database.export()["events"]) == events_before

        detail = tracker.update_item(
            "update-item",
            add_preserves=["keep claim blocking"],
            drop_preserves=["keep the export envelope"],
            add_anti_patterns=[{"dont": "drop and recreate", "why": "ids die", "instead": "use update"}],
            drop_anti_patterns=["rewrite history"],
            add_prior_art=[{"path": "src/todo_db/tracker.py", "concept": "update_item", "decision": "extend"}],
            drop_prior_art=[("README.md", "documented update verb")],
            add_work_needs=[("w1", "w0")],
            reason="bind the remaining create-time fields to the audited update verb",
        )
        item = tracker.get_item("update-item")
        assert item["preserves"] == ["keep claim blocking"]
        assert item["anti_patterns"] == [{"dont": "drop and recreate", "why": "ids die", "instead": "use update"}]
        assert item["prior_art"] == [{"path": "src/todo_db/tracker.py", "concept": "update_item", "decision": "extend"}]
        assert item["work"][1]["needs"] == ["w0"]
        event = database.export()["events"][-1]
        assert event["detail"]["preserves_added"] == detail["preserves_added"] == ["keep claim blocking"]
        assert event["detail"]["preserves_dropped"] == ["keep the export envelope"]
        assert event["detail"]["anti_patterns_added"] == [
            {"dont": "drop and recreate", "why": "ids die", "instead": "use update"}
        ]
        assert event["detail"]["anti_patterns_dropped"] == [
            {"dont": "rewrite history", "why": "audit breaks", "instead": "amend in place"}
        ]
        assert event["detail"]["prior_art_added"] == [
            {"path": "src/todo_db/tracker.py", "concept": "update_item", "decision": "extend"}
        ]
        assert event["detail"]["work_needs_added"] == [{"wid": "w1", "needs_wid": "w0"}]
        with pytest.raises(TodoError, match="work-unit dependency cycle"):
            tracker.update_item("update-item", add_work_needs=[("w0", "w1")])
        with pytest.raises(TodoError, match="--reason is required"):
            tracker.update_item("update-item", drop_work_needs=[("w1", "w0")])
    finally:
        database.close()


def test_no_change_flags_and_no_op_edits_are_rejected_without_events(tmp_path: Path) -> None:
    database, tracker = open_tracker(tmp_path)
    try:
        create_basic_item(tracker)
        events_before = len(database.export()["events"])
        with pytest.raises(TodoError, match="at least one change flag"):
            tracker.update_item("update-item")
        with pytest.raises(TodoError, match="at least one change flag"):
            tracker.update_item("update-item", reason="a reason is not a change")
        with pytest.raises(TodoError, match="nothing to update"):
            tracker.update_item("update-item", title="Original title")
        with pytest.raises(TodoError, match="nothing to update"):
            tracker.update_item("update-item", edit_work={"w0": "Original pending unit"})
        assert len(database.export()["events"]) == events_before
    finally:
        database.close()


def test_audit_chain_and_export_round_trip_survive_updates(tmp_path: Path) -> None:
    database, tracker = open_tracker(tmp_path)
    try:
        create_basic_item(tracker)
        tracker.update_item("update-item", title="First corrected title")
        tracker.update_item("update-item", add_work=[{"id": "w1", "summary": "Added mid-batch unit"}])
        tracker.update_item("update-item", add_verify=[{"description": "Lint", "command": "uv run ruff check ."}])
        assert database.verify_audit()["event_count"] == 4
        exported = database.export()
        restored = TodoDatabase.open(DatabaseConfig(path=tmp_path / "restored.sqlite", identity=IDENTITY))
        restored.restore(exported)
        assert restored.export() == exported
        restored.close()
    finally:
        database.close()


def test_cli_update_exit_codes_show_and_audit_verify(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from todo_db.cli import main

    db_path = tmp_path / "standalone.sqlite"
    common = ["--db", str(db_path), "--project-id", "update-cli", "--repository", "todo-db"]
    assert main([*common, "init"]) == 0
    assert (
        main(
            [
                *common,
                "create",
                "cli-item",
                "--title",
                "CLI item",
                "--worktree",
                "todo-db",
                "--priority",
                "medium",
                "--description",
                "A CLI item exercising the update verb.",
                "--work",
                "w0:Do the work",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert main([*common, "update", "cli-item"]) == 2
    assert "at least one change flag" in capsys.readouterr().err
    assert main([*common, "update", "cli-item", "--title", "CLI item"]) == 2
    assert "nothing to update" in capsys.readouterr().err
    assert (
        main([*common, "update", "cli-item", "--title", "CLI item corrected", "--add-verify", "Smoke::printf PASS"])
        == 0
    )
    assert "updated cli-item (changes, verify_added)" in capsys.readouterr().out
    assert main([*common, "update", "cli-item", "--add-only-modify", "tests/**"]) == 2
    assert "scope changes require --reason" in capsys.readouterr().err
    assert (
        main(
            [
                *common,
                "update",
                "cli-item",
                "--add-only-modify",
                "tests/**",
                "--reason",
                "tests are required by the recorded verification",
            ]
        )
        == 0
    )
    assert "updated cli-item (scope_added)" in capsys.readouterr().out
    assert main([*common, "update", "cli-item", "--drop-verify", "1"]) == 2
    assert "--reason is required" in capsys.readouterr().err
    assert main([*common, "show", "cli-item", "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["title"] == "CLI item corrected"
    assert shown["verifications"][0]["command"] == "printf PASS"
    assert main([*common, "claim", "cli-item"]) == 0
    assert main([*common, "done", "cli-item", "w0", "--evidence", "done evidence"]) == 0
    capsys.readouterr()
    assert main([*common, "update", "cli-item", "--edit-work", "w0:Rewritten done unit"]) == 2
    assert "only pending units" in capsys.readouterr().err
    assert main([*common, "audit", "verify"]) == 0


def test_cli_update_remaining_fields(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from todo_db.cli import main

    db_path = tmp_path / "standalone.sqlite"
    common = ["--db", str(db_path), "--project-id", "update-cli", "--repository", "todo-db"]
    assert main([*common, "init"]) == 0
    assert (
        main(
            [
                *common,
                "create",
                "dep-item",
                "--title",
                "Dependency item",
                "--worktree",
                "todo-db",
                "--priority",
                "medium",
                "--description",
                "An item used as a dependency target.",
                "--work",
                "w0:Do the work",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                *common,
                "create",
                "cli-item",
                "--title",
                "CLI item",
                "--worktree",
                "todo-db",
                "--priority",
                "medium",
                "--description",
                "A CLI item exercising remaining update fields.",
                "--work",
                "w0:Do the work",
                "--work",
                "w1:Follow-up work",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert main([*common, "update", "cli-item", "--drop-needs", "dep-item"]) == 2
    assert "--reason is required" in capsys.readouterr().err
    assert (
        main(
            [
                *common,
                "update",
                "cli-item",
                "--approach",
                "Amend through update",
                "--category",
                "code",
                "--add-needs",
                "dep-item",
                "--add-preserve",
                "keep the item id",
                "--add-anti-pattern",
                "DO NOT drop and recreate -- because ids die -- use update",
                "--add-prior-art",
                "src/todo_db/tracker.py::update_item::extend",
                "--add-work-need",
                "w1:w0",
            ]
        )
        == 0
    )
    assert "updated cli-item" in capsys.readouterr().out
    assert main([*common, "show", "cli-item", "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["approach"] == "Amend through update"
    assert shown["category"] == "code"
    assert shown["deps"] == ["dep-item"]
    assert shown["preserves"] == ["keep the item id"]
    assert shown["anti_patterns"] == [{"dont": "drop and recreate", "why": "ids die", "instead": "use update"}]
    assert shown["prior_art"] == [{"path": "src/todo_db/tracker.py", "concept": "update_item", "decision": "extend"}]
    assert shown["work"][1]["needs"] == ["w0"]
    assert main([*common, "update", "cli-item", "--approach", ""]) == 0
    capsys.readouterr()
    assert main([*common, "show", "cli-item", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["approach"] is None
