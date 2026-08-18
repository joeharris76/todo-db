from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from test_hosted_backend import FakeLibsql

if TYPE_CHECKING:
    from conftest import SQLTrace

from todo_db import CredentialMode, DatabaseConfig, ProjectIdentity, TodoDatabase, TodoTracker


def test_sql_trace_fixture_and_phases(sql_trace: SQLTrace, seeded_db: TodoDatabase) -> None:
    sql_trace.reset()
    tracker = TodoTracker(seeded_db, actor="test-actor")

    # Verify phase separation
    with sql_trace.phase("open"):
        seeded_db.connection.execute("SELECT 1")

    with sql_trace.phase("operation"):
        items = tracker.list_items()
        assert len(items) >= 20

    with sql_trace.phase("render"):
        lines = [item["title"] for item in items]
        sql_trace.record_output(stdout="\n".join(lines))

    assert sql_trace.open_statements == 1
    assert sql_trace.op_statements >= 1
    assert sql_trace.render_statements == 0
    assert sql_trace.stdout_bytes > 0
    assert sql_trace.estimated_tokens > 0


def test_seeded_db_fixture_conformance(seeded_db: TodoDatabase) -> None:
    tracker = TodoTracker(seeded_db, actor="test-actor")
    items = tracker.list_items()
    assert len(items) >= 20

    # Verify every item has >= 3 work units
    for item in items:
        full_item = tracker.get_item(item["id"])
        assert full_item is not None
        assert len(full_item["work"]) >= 3

    # Verify dependencies exist in the seeded set
    has_deps = any(len(tracker.get_item(item["id"])["deps"]) > 0 for item in items)
    assert has_deps is True


def test_baseline_counts_per_command(sql_trace: SQLTrace, seeded_db: TodoDatabase) -> None:
    tracker = TodoTracker(seeded_db, actor="test-actor")

    # 1. Baseline ready_items count
    sql_trace.reset()
    ready = tracker.ready_items()
    assert len(ready) > 0
    ready_statements = sql_trace.total_statements
    assert ready_statements == 1

    # 2. Baseline get_item count (1 query per child table, independent of work unit count)
    sql_trace.reset()
    item = tracker.get_item("item-00")
    assert item is not None
    get_statements = sql_trace.total_statements
    assert get_statements == 10

    # 3. Baseline work_order count (derived in-memory, 0 extra queries beyond get_item)
    sql_trace.reset()
    order = tracker.work_order("item-00")
    assert order is not None
    order_statements = sql_trace.total_statements
    assert order_statements == 10

    # 4. Baseline lint
    sql_trace.reset()
    findings = tracker.lint("item-00")
    assert isinstance(findings, list)


def test_hosted_transport_counter(sql_trace: SQLTrace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    primary = tmp_path / "primary.sqlite"
    # Initialize the primary database first
    TodoDatabase.open(
        DatabaseConfig(
            path=primary,
            identity=ProjectIdentity(project_id="test-hosted", repository="https://example.test/hosted"),
        )
    )

    fake = FakeLibsql(primary)
    monkeypatch.setitem(sys.modules, "libsql", fake)

    sql_trace.reset()
    config = DatabaseConfig(
        path="libsql://project.aws-us-east-1.turso.io",
        identity=ProjectIdentity(project_id="test-hosted", repository="https://example.test/hosted"),
        auth_token="ro-token",
        credential_mode=CredentialMode.READ_ONLY,
    )
    db = TodoDatabase.open(config)
    assert db.project_identity.project_id == "test-hosted"
    assert sql_trace.total_statements >= 1


def test_output_metrics_recording(sql_trace: SQLTrace) -> None:
    sql_trace.reset()
    sample_out = "item-01 high Feature 1\nitem-02 medium Feature 2\n"
    sample_err = "warning: 1 draft finding pending\n"

    sql_trace.record_output(stdout=sample_out, stderr=sample_err)
    assert sql_trace.stdout_bytes == len(sample_out.encode("utf-8"))
    assert sql_trace.stderr_bytes == len(sample_err.encode("utf-8"))
    assert sql_trace.estimated_tokens == (len(sample_out) + len(sample_err) + 3) // 4


def test_tool_invocations_lifecycle_counter(sql_trace: SQLTrace, tmp_path: Path) -> None:
    sql_trace.reset()
    db_path = tmp_path / "lifecycle.sqlite"
    config = DatabaseConfig(
        path=db_path,
        identity=ProjectIdentity(project_id="lifecycle-test", repository="https://example.test/repo"),
    )
    db = TodoDatabase.open(config)
    tracker = TodoTracker(db, actor="tester")

    # Simulate full item lifecycle
    steps = [
        (
            "create",
            lambda: tracker.create_item(
                item_id="item-lc",
                title="Lifecycle item",
                worktree="todo-db",
                category="feature",
                priority="high",
                description="Testing full lifecycle statement budget",
                work=[{"id": "w0", "summary": "step 0"}],
                verifications=[{"description": "v1", "command": "printf PASS", "expected": "PASS"}],
            ),
        ),
        ("claim", lambda: tracker.claim("item-lc")),
        ("start", lambda: tracker.start_unit("item-lc", "w0")),
        ("done", lambda: tracker.done_unit("item-lc", "w0", "commit 123")),
        ("verify", lambda: tracker.run_verification("item-lc", 1)),
        ("complete", lambda: tracker.complete("item-lc")),
    ]

    for step_name, action in steps:
        sql_trace.record_tool_invocation(step_name)
        with sql_trace.phase("operation"):
            action()

    assert len(sql_trace.tool_invocations) == 6
    assert sql_trace.tool_invocations == ["create", "claim", "start", "done", "verify", "complete"]
    assert sql_trace.total_statements >= 6
