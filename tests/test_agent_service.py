from pathlib import Path
import pytest

from todo_db import DatabaseConfig, ProjectIdentity, TodoDatabase, TodoTracker
from todo_db.agent import AgentWorkflow
from todo_db.errors import TodoError


def _setup_db(tmp_path: Path) -> tuple[TodoDatabase, TodoTracker, AgentWorkflow]:
    db_path = tmp_path / "agent_svc.sqlite"
    config = DatabaseConfig(
        path=db_path,
        identity=ProjectIdentity(project_id="agent-svc-test", repository="todo-db"),
    )
    db = TodoDatabase.open(config)
    tracker = TodoTracker(db, actor="agent-tester")
    workflow = AgentWorkflow(tracker, repo_root=tmp_path)
    return db, tracker, workflow


def test_agent_workflow_next_and_idle(tmp_path: Path) -> None:
    db, tracker, workflow = _setup_db(tmp_path)
    try:
        # Initially empty -> idle
        res = workflow.next()
        assert res["status"] == "idle"
        assert res["item"] is None
        assert res["next_action"]["action"] == "wait"

        # Create ready item
        tracker.create_item(
            item_id="item-ready",
            title="Ready Item",
            worktree="todo-db",
            priority="high",
            description="Item description for agent testing",
            work=[{"id": "w0", "summary": "Initial work"}],
        )

        res = workflow.next()
        assert res["status"] == "ready"
        assert res["item"]["id"] == "item-ready"
        assert res["next_action"]["action"] == "take"
    finally:
        db.close()


def test_agent_workflow_take_and_one_claim_rule(tmp_path: Path) -> None:
    db, tracker, workflow = _setup_db(tmp_path)
    try:
        tracker.create_item(
            item_id="item-1",
            title="Item One",
            worktree="todo-db",
            priority="high",
            description="First item description",
            work=[{"id": "w0", "summary": "Step 0"}],
        )
        tracker.create_item(
            item_id="item-2",
            title="Item Two",
            worktree="todo-db",
            priority="high",
            description="Second item description",
            work=[{"id": "w0", "summary": "Step 0"}],
        )

        # 1. Take first item
        ctx1 = workflow.take("item-1", session="session-1")
        assert ctx1["id"] == "item-1"
        assert ctx1["claimed_by"] == "agent-tester"
        assert ctx1["claimed_session"] == "session-1"

        # 2. Attempt to take second item while holding first raises TodoError
        with pytest.raises(TodoError, match="already holds active claim"):
            workflow.take("item-2")

        # 3. Same item re-take returns existing context and updates session
        ctx1_adopt = workflow.take("item-1", session="session-restarted")
        assert ctx1_adopt["id"] == "item-1"
        assert ctx1_adopt["claimed_session"] == "session-restarted"

        # 4. next() reports active claim
        nxt = workflow.next()
        assert nxt["status"] == "claimed"
        assert nxt["item"]["id"] == "item-1"
        assert nxt["next_action"]["action"] == "progress"
    finally:
        db.close()


def test_agent_workflow_context_and_mandatory_guardrails(tmp_path: Path) -> None:
    db, tracker, workflow = _setup_db(tmp_path)
    try:
        tracker.create_item(
            item_id="item-ctx",
            title="Context Item",
            worktree="todo-db",
            priority="high",
            description="Context testing item",
            work=[{"id": "w0", "summary": "First step"}],
            preserves=["Preserve integrity"],
            anti_patterns=[{"dont": "Do bad", "why": "Is bad", "instead": "Do good"}],
            scope={"only_modify": ["src/**"], "do_not_modify": [".todo-db/**"]},
        )
        workflow.take("item-ctx")

        # Requesting specific field still preserves mandatory guardrails (scope, preserves, anti_patterns, etc.)
        ctx = workflow.context("item-ctx", fields=["work_units"])
        assert "work_units" in ctx
        assert "scope" in ctx
        assert "preserves" in ctx
        assert "anti_patterns" in ctx
        assert "next_action" in ctx
        assert ctx["completeness"]["work_units_total"] == 1
    finally:
        db.close()


def test_agent_workflow_progress_and_finish(tmp_path: Path) -> None:
    db, tracker, workflow = _setup_db(tmp_path)
    try:
        tracker.create_item(
            item_id="item-flow",
            title="Flow Item",
            worktree="todo-db",
            priority="high",
            description="Complete flow item",
            work=[{"id": "w0", "summary": "Step 0"}],
            scope={"only_modify": ["src/**"]},
            verifications=[{"description": "smoke", "command": "true", "expected": ""}],
        )
        ctx = workflow.take("item-flow")
        token = ctx["claim_token"]

        # Progress work unit
        p_ctx = workflow.progress("item-flow", "w0", "Commit 123 completed", claim_token=token)
        assert p_ctx["work_units"][0]["status"] == "done"
        assert p_ctx["next_action"]["action"] == "finish"

        # Run verification step
        res, _ = tracker.run_verification("item-flow", 1)
        assert res == "pass"

        # Model assert finish (verifications passed, completes)
        fin = workflow.finish("item-flow", claim_token=token, model_assert=True)
        assert fin["status"] == "completed"
        assert tracker.get_item("item-flow")["state"] == "done"
    finally:
        db.close()
