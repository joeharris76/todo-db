from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from todo_db import DatabaseConfig, ProjectIdentity, TodoDatabase, TodoTracker
from todo_db.database import SCHEMA_VERSION
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
        assert events[-1]["detail"]["verification"] == {"result": "pass", "sequences": [1]}
    finally:
        database.close()


def test_complete_refuses_exit_five_and_audits_reasoned_override(tmp_path: Path) -> None:
    database, tracker = open_tracker(tmp_path)
    try:
        tracker.create_item(
            item_id="vacuous-item",
            title="Vacuous verification item",
            worktree="todo-db",
            priority="high",
            description="The completion gate must reject a vacuous verification selector.",
            verifications=[{"description": "No tests collected", "command": "exit 5"}],
        )
        tracker.claim("vacuous-item")

        with pytest.raises(TodoError, match="verification seq=1 failed"):
            tracker.complete("vacuous-item")
        assert tracker.get_item("vacuous-item")["state"] == "active"
        assert tracker.get_item("vacuous-item")["verifications"][0]["last_result"] == "fail"

        tracker.complete(
            "vacuous-item",
            verification_override_reason="Maintainer accepted external certification evidence",
        )
        complete_event = database.export()["events"][-1]
        assert complete_event["action"] == "complete"
        assert complete_event["actor"] == "test-actor"
        assert complete_event["detail"]["verification_override"] == {
            "reason": "Maintainer accepted external certification evidence",
            "sequences": [1],
        }
    finally:
        database.close()


def test_complete_grades_verification_by_exit_status_not_expected_prose(tmp_path: Path) -> None:
    database, tracker = open_tracker(tmp_path)
    try:
        tracker.create_item(
            item_id="human-expected-item",
            title="Human expected text item",
            worktree="todo-db",
            priority="medium",
            description="The expected field is human acceptance text rather than an output assertion.",
            verifications=[
                {
                    "description": "Successful command",
                    "command": "printf actual-output",
                    "expected": "exit 0 and the release remains compatible",
                }
            ],
        )
        tracker.claim("human-expected-item")
        tracker.complete("human-expected-item")
        assert tracker.get_item("human-expected-item")["state"] == "done"
    finally:
        database.close()


def test_complete_rejects_empty_or_unnecessary_verification_override(tmp_path: Path) -> None:
    database, tracker = open_tracker(tmp_path)
    try:
        tracker.create_item(
            item_id="no-ladder-item",
            title="No ladder item",
            worktree="todo-db",
            priority="low",
            description="An item with no configured verification ladder.",
        )
        tracker.claim("no-ladder-item")
        with pytest.raises(TodoError, match="reason must not be empty"):
            tracker.complete("no-ladder-item", verification_override_reason="  ")
        with pytest.raises(TodoError, match="has no verification steps"):
            tracker.complete("no-ladder-item", verification_override_reason="Not needed")
        tracker.complete("no-ladder-item")
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
        assert export["schema"]["version"] == SCHEMA_VERSION
        assert export["tables"]["items"][0]["id"] == "deferred-item"
        json.dumps(export)
    finally:
        database.close()


def _uv_project_lint_findings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    manifest: str,
) -> list[str]:
    project = tmp_path / "tools"
    project.mkdir()
    (project / "pyproject.toml").write_text(manifest, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    database, tracker = open_tracker(tmp_path)
    try:
        tracker.create_item(
            item_id="uv-project-item",
            title="UV project verification item",
            worktree="todo-db",
            priority="medium",
            description="An item used to lint selected uv project environments.",
            verifications=[{"description": "Selected project", "command": command}],
        )
        return [finding for finding in tracker.lint("uv-project-item") if "runs pytest through uv project" in finding]
    finally:
        database.close()


def test_uv_project_lint_flags_pytest_missing_from_selected_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    findings = _uv_project_lint_findings(
        tmp_path,
        monkeypatch,
        "uv run --project tools -- python -m pytest tests -q",
        '[project]\nname = "tools"\nversion = "0.1.0"\ndependencies = ["pyyaml"]\n',
    )
    assert len(findings) == 1
    assert "does not declare or inject pytest" in findings[0]
    assert "--with pytest" in findings[0]


@pytest.mark.parametrize(
    "command",
    [
        "uv run --project tools --no-dev -- python -m pytest -q",
        "uv run --project tools --only-group lint -- python -m pytest -q",
    ],
)
def test_uv_project_lint_respects_excluded_dependencies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    findings = _uv_project_lint_findings(
        tmp_path,
        monkeypatch,
        command,
        '[project]\nname = "tools"\nversion = "0.1.0"\ndependencies = []\n'
        '[dependency-groups]\ndev = ["pytest>=9"]\nlint = ["ruff"]\n',
    )
    assert len(findings) == 1


@pytest.mark.parametrize(
    ("command", "manifest"),
    [
        (
            "uv run --project tools -- python -m pytest -q",
            '[project]\nname = "tools"\nversion = "0.1.0"\ndependencies = ["pytest>=9"]\n',
        ),
        (
            "uv run --project tools -- pytest -q",
            '[project]\nname = "tools"\nversion = "0.1.0"\ndependencies = []\n'
            '[dependency-groups]\ndev = ["pytest>=9"]\n',
        ),
        (
            "uv run --project tools --extra test -- python -m pytest -q",
            '[project]\nname = "tools"\nversion = "0.1.0"\ndependencies = []\n'
            '[project.optional-dependencies]\ntest = ["pytest>=9"]\n',
        ),
        (
            "uv run --project tools -- python -m pytest -q",
            '[project]\nname = "tools"\nversion = "0.1.0"\ndependencies = []\n'
            '[dependency-groups]\ndev = [{include-group = "test"}]\ntest = ["pytest>=9"]\n',
        ),
        (
            "uv run --project tools --with pytest -- python -m pytest -q",
            '[project]\nname = "tools"\nversion = "0.1.0"\ndependencies = []\n',
        ),
        (
            "uv run --project tools --with-requirements requirements.txt -- python -m pytest -q",
            '[project]\nname = "tools"\nversion = "0.1.0"\ndependencies = []\n',
        ),
        (
            "uv run --project tools -- python scripts/check.py",
            '[project]\nname = "tools"\nversion = "0.1.0"\ndependencies = []\n',
        ),
    ],
)
def test_uv_project_lint_accepts_self_contained_or_non_pytest_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    manifest: str,
) -> None:
    assert _uv_project_lint_findings(tmp_path, monkeypatch, command, manifest) == []


@pytest.mark.parametrize(
    ("command", "manifest"),
    [
        ("uv run --project tools -- python -m 'pytest", "not valid toml = ["),
        ("uv run --project tools -- python -m pytest", "not valid toml = ["),
        ("uv run --project tools -- python -m pytest", 'project = "not a table"\n'),
        (
            "uv run --directory elsewhere --project tools -- python -m pytest",
            '[project]\nname = "tools"\nversion = "0.1.0"\ndependencies = []\n',
        ),
    ],
)
def test_uv_project_lint_skips_ambiguous_shell_or_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    manifest: str,
) -> None:
    assert _uv_project_lint_findings(tmp_path, monkeypatch, command, manifest) == []


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
