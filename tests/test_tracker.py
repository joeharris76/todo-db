from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from todo_db import DatabaseConfig, ProjectIdentity, TodoDatabase, TodoTracker
from todo_db.errors import TodoError


def open_tracker(tmp_path: Path) -> tuple[TodoDatabase, TodoTracker]:
    identity = ProjectIdentity(project_id="tracker-test", repository="https://example.test/tracker")
    database = TodoDatabase.open(DatabaseConfig(path=tmp_path / "todo.sqlite", identity=identity))
    return database, TodoTracker(database, actor="test-actor")


def test_full_item_lifecycle_is_audited_and_dependency_gated(tmp_path: Path) -> None:
    database, tracker = open_tracker(tmp_path)
    try:
        tracker.create_item(
            item_id="dependency",
            title="Dependency item",
            worktree="todo-db",
            priority="high",
            description="A dependency that must be completed first.",
            work=[{"id": "w0", "summary": "Complete dependency"}],
        )
        tracker.create_item(
            item_id="feature-item",
            title="Feature item",
            worktree="todo-db",
            priority="medium",
            description="A feature with ordered work and explicit scope.",
            work=[
                {"id": "w0", "summary": "Write the failing test"},
                {"id": "w1", "summary": "Implement the behavior", "needs": ["w0"]},
            ],
            deps=["dependency"],
            scope=[("only_modify", "src/**"), ("do_not_modify", "BenchBox/**")],
            verifications=[{"description": "Run tests", "command": "printf PASS", "expected": "PASS"}],
        )

        with pytest.raises(TodoError, match="unmet dependencies"):
            tracker.claim("feature-item")

        tracker.claim("dependency")
        tracker.done_unit("dependency", "w0", "test evidence")
        tracker.complete("dependency")
        tracker.claim("feature-item")

        with pytest.raises(TodoError, match="unfinished units"):
            tracker.start_unit("feature-item", "w1")
        tracker.start_unit("feature-item", "w0")
        tracker.done_unit("feature-item", "w0", "pytest tests/test_tracker.py")
        tracker.start_unit("feature-item", "w1")
        tracker.done_unit("feature-item", "w1", "implementation evidence")
        tracker.complete("feature-item", pr=123)

        item = tracker.get_item("feature-item")
        assert item["state"] == "done"
        assert item["completed_pr"] == 123
        assert item["scope"][0]["kind"] == "do_not_modify"
        assert tracker.stats()["items_by_state"]["done"] == 2
        events = database.export()["events"]
        assert events[-1]["action"] == "complete"
        assert events[-1]["detail"]["item_id"] == "feature-item"
    finally:
        database.close()


def test_work_and_item_cycles_are_rejected(tmp_path: Path) -> None:
    database, tracker = open_tracker(tmp_path)
    try:
        tracker.create_item(
            item_id="cycle-item",
            title="Cycle item",
            worktree="todo-db",
            priority="low",
            description="An item used to verify cycle rejection.",
            work=[
                {"id": "w0", "summary": "First unit"},
                {"id": "w1", "summary": "Second unit", "needs": ["w0"]},
            ],
        )
        with pytest.raises(TodoError, match="cycle"):
            tracker.add_work_need("cycle-item", "w0", "w1")

        tracker.create_item(
            item_id="other-item",
            title="Other item",
            worktree="todo-db",
            priority="low",
            description="Another item used to verify dependency cycles.",
        )
        tracker.add_item_dep("cycle-item", "other-item")
        with pytest.raises(TodoError, match="cycle"):
            tracker.add_item_dep("other-item", "cycle-item")
    finally:
        database.close()


def test_deferrals_lint_verification_and_export(tmp_path: Path) -> None:
    database, tracker = open_tracker(tmp_path)
    try:
        tracker.set_config("lint.require_scope_rules", "on")
        tracker.create_item(
            item_id="deferred-item",
            title="Deferred item",
            worktree="todo-db",
            priority="medium",
            description="This description has no verification evidence.",
            work=[{"id": "w0", "summary": "Do the work"}],
        )
        assert "no verification steps recorded" in tracker.lint("deferred-item")
        assert any("no scope rules" in finding for finding in tracker.lint("deferred-item"))

        tracker.claim("deferred-item")
        deferral_id = tracker.defer("deferred-item", "Follow-up behavior", "Out of scope for this item")
        tracker.done_unit("deferred-item", "w0", "evidence")
        with pytest.raises(TodoError, match="unresolved deferrals"):
            tracker.complete("deferred-item")
        tracker.dismiss_deferral(deferral_id, "Tracked separately")
        tracker.complete("deferred-item")

        export = database.export()
        assert export["schema"]["version"] == 4
        assert export["tables"]["items"][0]["id"] == "deferred-item"
        json.dumps(export)
    finally:
        database.close()


def test_existing_non_standalone_tracker_database_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "todo.sqlite"
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE items (id TEXT PRIMARY KEY)")
    raw.commit()
    raw.close()
    identity = ProjectIdentity(project_id="tracker-test", repository="https://example.test/tracker")

    with pytest.raises(Exception, match="different tracker schema"):
        TodoDatabase.open(DatabaseConfig(path=path, identity=identity))


def test_promoting_a_deferral_audits_the_new_item_and_resolution(tmp_path: Path) -> None:
    database, tracker = open_tracker(tmp_path)
    try:
        tracker.create_item(
            item_id="parent-item",
            title="Parent item",
            worktree="todo-db",
            priority="medium",
            description="The parent item for a promoted follow-up.",
        )
        tracker.claim("parent-item")
        deferral_id = tracker.defer("parent-item", "Follow-up item", "Needs a separate implementation")
        tracker.promote_deferral(deferral_id, "follow-up-item")

        assert tracker.get_item("follow-up-item")["state"] == "planning"
        actions = [event["action"] for event in database.export()["events"]]
        assert actions[-2:] == ["create", "promote"]
    finally:
        database.close()


def test_release_is_holder_only(tmp_path: Path) -> None:
    """Regression: non-holder release must fail with exit-2 semantics.

    Covers the lease-theft fix in TodoTracker.release(): Bob's failed
    release preserves Alice's claim, Alice's own release succeeds,
    unclaimed release is a no-op, and stale leases are still not
    releasable by others (takeover is via claim / sweep-stale).
    """

    identity = ProjectIdentity(project_id="tracker-test", repository="https://example.test/tracker")
    database = TodoDatabase.open(DatabaseConfig(path=tmp_path / "todo.sqlite", identity=identity))
    try:
        alice = TodoTracker(database, actor="alice")
        bob = TodoTracker(database, actor="bob")

        alice.create_item(
            item_id="lease-item",
            title="Lease item",
            worktree="todo-db",
            priority="medium",
            description="Item used to verify holder-only release.",
        )
        alice.claim("lease-item")
        assert database.connection.execute("SELECT claimed_by FROM items WHERE id='lease-item'").fetchone()[
            "claimed_by"
        ] == "alice"

        # Bob must not be able to release Alice's active claim.
        with pytest.raises(TodoError, match="only the holder can release"):
            bob.release("lease-item")
        assert database.connection.execute("SELECT claimed_by FROM items WHERE id='lease-item'").fetchone()[
            "claimed_by"
        ] == "alice"

        # Holder can release.
        alice.release("lease-item")
        assert database.connection.execute("SELECT claimed_by FROM items WHERE id='lease-item'").fetchone()[
            "claimed_by"
        ] is None

        # Unclaimed release is a no-op (idempotent).
        bob.release("lease-item")
        assert database.connection.execute("SELECT claimed_by FROM items WHERE id='lease-item'").fetchone()[
            "claimed_by"
        ] is None

        # Stale lease is still not releasable by non-holder; takeover is via claim.
        alice.claim("lease-item")
        database.connection.execute("UPDATE items SET claimed_at='2000-01-01T00:00:00Z' WHERE id='lease-item'")
        database.connection.commit()
        with pytest.raises(TodoError, match="only the holder can release"):
            bob.release("lease-item")
        # Claim takeover of expired lease succeeds.
        bob.claim("lease-item")
        assert database.connection.execute("SELECT claimed_by FROM items WHERE id='lease-item'").fetchone()[
            "claimed_by"
        ] == "bob"
    finally:
        database.close()
