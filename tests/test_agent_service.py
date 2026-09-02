from pathlib import Path
import pytest

from todo_db import DatabaseConfig, ProjectIdentity, TodoDatabase, TodoTracker
from todo_db.agent import AgentWorkflow, GitScopeEngine
from todo_db.errors import TodoError


import subprocess

def _setup_db(tmp_path: Path) -> tuple[TodoDatabase, TodoTracker, AgentWorkflow]:
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text(".todo-db/\n*.sqlite*\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True)
    db_dir = tmp_path / ".todo-db"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "agent_svc.sqlite"
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

        # Run and attest verification against the current workspace.
        res, _ = tracker.run_verification("item-flow", 1)
        assert res == "pass"
        tracker.attest_verifications("item-flow", workflow.git_engine.workspace_fingerprint())

        # Model assert finish accepts only the matching attestation.
        fin = workflow.finish("item-flow", claim_token=token, model_assert=True)
        assert fin["status"] == "completed"
        assert tracker.get_item("item-flow")["state"] == "done"
    finally:
        db.close()


def test_agent_workflow_finish_gates_and_remediation(tmp_path: Path) -> None:
    db, tracker, workflow = _setup_db(tmp_path)
    try:
        tracker.create_item(
            item_id="item-gates",
            title="Gates Item",
            worktree="todo-db",
            priority="high",
            description="Testing finish gate remediation",
            work=[{"id": "w0", "summary": "Step 0"}],
            scope={"only_modify": ["src/**"]},
            verifications=[{"description": "failing step", "command": "false", "expected": ""}],
        )
        ctx = workflow.take("item-gates")
        token = ctx["claim_token"]
        workflow.progress("item-gates", "w0", "Done step 0", claim_token=token)

        # 1. Model assert fails when verification not run / not passing
        with pytest.raises(TodoError, match="not attested"):
            workflow.finish("item-gates", claim_token=token, model_assert=True)

        # Claim must still be held (not released on code/verification failure)
        assert tracker.get_item("item-gates")["claimed_by"] == "agent-tester"

        # 2. Human run_verifications fails with verification failure error
        with pytest.raises(TodoError, match="verification seq 1 failed"):
            workflow.finish("item-gates", claim_token=token, run_verifications=True)

        # 3. Scope violation failure retains claim
        (tmp_path / "out_of_scope.txt").write_text("evil", encoding="utf-8")
        # Update verification to pass
        tracker.update_item(
            "item-gates",
            drop_verify=[1],
            add_verify=[{"description": "passing", "command": "true", "expected": ""}],
            reason="Update verification command to pass",
        )
        with pytest.raises(TodoError, match="scope violations detected"):
            workflow.finish("item-gates", claim_token=token, run_verifications=True)

        assert tracker.get_item("item-gates")["claimed_by"] == "agent-tester"
    finally:
        db.close()


def test_non_holder_cannot_finish_or_progress(tmp_path: Path) -> None:
    db, tracker, workflow = _setup_db(tmp_path)
    try:
        tracker.create_item(
            item_id="item-auth",
            title="Auth Item",
            worktree="todo-db",
            priority="high",
            description="Testing ownership gates",
            work=[{"id": "w0", "summary": "Step 0"}],
            scope={"only_modify": ["src/**"]},
            verifications=[{"description": "smoke", "command": "true", "expected": ""}],
        )
        ctx = workflow.take("item-auth")
        token = ctx["claim_token"]

        # Other actor workflow
        other_tracker = TodoTracker(db, actor="other-agent")
        other_workflow = AgentWorkflow(other_tracker, repo_root=tmp_path)

        # 1. Non-holder cannot progress
        with pytest.raises(TodoError, match="is not claimed by actor 'other-agent'"):
            other_workflow.progress("item-auth", "w0", "evidence", claim_token=token)

        # 2. Non-holder cannot finish
        with pytest.raises(TodoError, match="is not claimed by actor 'other-agent'"):
            other_workflow.finish("item-auth", claim_token=token, model_assert=True)

        # 3. Context redacts claim token for non-holder
        other_ctx = other_workflow.context("item-auth")
        assert other_ctx["claim_token"] is None
        holder_ctx = workflow.context("item-auth")
        assert holder_ctx["claim_token"] == token
    finally:
        db.close()


def test_adopt_rotates_token_and_refreshes_lease(tmp_path: Path) -> None:
    db, tracker, workflow = _setup_db(tmp_path)
    try:
        tracker.create_item(
            item_id="item-adopt",
            title="Adopt Item",
            worktree="todo-db",
            priority="high",
            description="Testing claim adoption",
            work=[{"id": "w0", "summary": "Step 0"}],
            scope={"only_modify": ["src/**"]},
        )
        ctx1 = workflow.take("item-adopt", session="sess-1")
        token1 = ctx1["claim_token"]

        ctx2 = workflow.adopt("item-adopt", session="sess-2")
        token2 = ctx2["claim_token"]
        assert token1 != token2
        assert ctx2["claimed_session"] == "sess-2"

        # Non-holder cannot adopt
        other_tracker = TodoTracker(db, actor="intruder")
        other_workflow = AgentWorkflow(other_tracker, repo_root=tmp_path)
        with pytest.raises(TodoError, match="not an active claim held by actor 'intruder'"):
            other_workflow.adopt("item-adopt", session="sess-evil")
    finally:
        db.close()


def test_finish_runs_verification_once_and_rejects_stale_attestation(tmp_path: Path) -> None:
    db, tracker, workflow = _setup_db(tmp_path)
    try:
        (tmp_path / "src").mkdir()
        counter = tmp_path.parent / f"{tmp_path.name}-counter.txt"
        command = (
            f"python -c \"from pathlib import Path; p=Path(r'{counter}'); "
            "p.write_text(str(int(p.read_text())+1) if p.exists() else '1')\""
        )
        tracker.create_item(
            item_id="item-once",
            title="Single verification execution",
            worktree="todo-db",
            priority="medium",
            description="Prove finish executes the ladder exactly once",
            work=[{"id": "w0", "summary": "Complete implementation"}],
            scope={"only_modify": ["src/**"]},
            verifications=[{"description": "increment", "command": command}],
        )
        context = workflow.take("item-once")
        token = context["claim_token"]
        workflow.progress("item-once", "w0", "done", claim_token=token)
        workflow.finish("item-once", claim_token=token, run_verifications=True)
        assert counter.read_text() == "1"
        counter.unlink()

        tracker.create_item(
            item_id="item-mutating-check",
            title="Reject mutating verification",
            worktree="todo-db",
            priority="medium",
            description="Prevent post-check changes and second-run scope bypasses",
            work=[{"id": "w0", "summary": "Complete implementation"}],
            scope={"only_modify": ["src/**"]},
            verifications=[
                {"description": "mutates source", "command": "printf changed > src/generated.txt"}
            ],
        )
        mutating = workflow.take("item-mutating-check")
        mutating_token = mutating["claim_token"]
        workflow.progress("item-mutating-check", "w0", "done", claim_token=mutating_token)
        with pytest.raises(TodoError, match="modified the Git workspace"):
            workflow.finish("item-mutating-check", claim_token=mutating_token, run_verifications=True)
        (tmp_path / "src" / "generated.txt").unlink()
        workflow.release("item-mutating-check", mutating_token)

        tracker.create_item(
            item_id="item-stale",
            title="Reject stale verification",
            worktree="todo-db",
            priority="medium",
            description="Prove source changes invalidate a prior verification",
            work=[{"id": "w0", "summary": "Complete implementation"}],
            scope={"only_modify": ["src/**"]},
            verifications=[{"description": "pass", "command": "true"}],
        )
        stale = workflow.take("item-stale")
        stale_token = stale["claim_token"]
        workflow.progress("item-stale", "w0", "done", claim_token=stale_token)
        tracker.run_verification("item-stale", 1)
        tracker.attest_verifications("item-stale", workflow.git_engine.workspace_fingerprint())
        (tmp_path / "src" / "changed.py").write_text("changed", encoding="utf-8")
        with pytest.raises(TodoError, match="not attested"):
            workflow.finish("item-stale", claim_token=stale_token, model_assert=True)
    finally:
        db.close()


def test_claim_generation_protects_release_and_multiple_claims_fail(tmp_path: Path) -> None:
    db, tracker, workflow = _setup_db(tmp_path)
    try:
        for item_id in ("claim-one", "claim-two"):
            tracker.create_item(
                item_id=item_id,
                title=f"Claim item {item_id}",
                worktree="todo-db",
                priority="medium",
                description="Exercise claim generation and conflict handling",
            )
        first = workflow.take("claim-one", session="old")
        old_token = first["claim_token"]
        adopted = workflow.adopt("claim-one", "new")
        with pytest.raises(TodoError, match="E_CLAIM_STALE"):
            workflow.release("claim-one", old_token)
        workflow.release("claim-one", adopted["claim_token"])

        tracker.claim("claim-one")
        tracker.claim("claim-two")
        with pytest.raises(TodoError) as exc:
            workflow.current_claim()
        assert exc.value.code == "E_MULTIPLE_CLAIMS"
    finally:
        db.close()


def test_structural_finish_gate_releases_claim_with_remediation(tmp_path: Path) -> None:
    db, tracker, workflow = _setup_db(tmp_path)
    try:
        tracker.create_item(
            item_id="item-structural",
            title="Malformed structural item",
            worktree="todo-db",
            priority="high",
            description="Missing required scope rules for lint gate coverage",
            work=[{"id": "w0", "summary": "Complete implementation"}],
        )
        context = workflow.take("item-structural")
        token = context["claim_token"]
        workflow.progress("item-structural", "w0", "done", claim_token=token)
        with pytest.raises(TodoError, match="claim retained; run `todo lint item-structural`"):
            workflow.finish("item-structural", claim_token=token, model_assert=True)
        # Lint gate retains the claim (ADR 0006 G7).
        assert tracker.get_item("item-structural")["claimed_by"] == "agent-tester"
        # Structural blockers still release immediately (even with lint present, structural dominates).
        tracker.block("item-structural", "human decision needed")
        with pytest.raises(TodoError, match="claim released; run `todo lint item-structural; then todo unblock"):
            workflow.finish("item-structural", claim_token=token, model_assert=True)
        assert tracker.get_item("item-structural")["claimed_by"] is None
    finally:
        db.close()


def test_context_pagination_and_blocked_remediation(tmp_path: Path) -> None:
    db, tracker, workflow = _setup_db(tmp_path)
    try:
        tracker.create_item(
            item_id="item-pages",
            title="Paged safety context",
            worktree="todo-db",
            priority="medium",
            description="Exercise recoverable context pagination and blockers",
            work=[{"id": f"w{i}", "summary": f"Work unit number {i}"} for i in range(3)],
            preserves=["one", "two", "three"],
        )
        tracker.block("item-pages", "human decision needed")
        first = workflow.context("item-pages", section="preserves", limit=2)
        assert first["completeness"]["preserves"]["complete"] is False
        assert first["completeness"]["preserves"]["next_cursor"] == 2
        second = workflow.context("item-pages", section="preserves", cursor=2, limit=2)
        assert second["preserves"] == ["two"]
        assert second["completeness"]["preserves"]["complete"] is True
        assert first["blocked_reason"] == "human decision needed"
        assert first["next_action"]["tool"] == "unblock"
    finally:
        db.close()


def test_empty_verification_command_and_nogit_scope(tmp_path: Path) -> None:
    db, tracker, workflow = _setup_db(tmp_path)
    try:
        tracker.create_item(
            item_id="item-empty-verif",
            title="Empty Verif",
            worktree="todo-db",
            priority="high",
            description="Testing empty verif command",
            verifications=[{"description": "empty command", "command": "", "expected": ""}],
        )
        with pytest.raises(TodoError, match="has no command"):
            tracker.run_verification("item-empty-verif", 1)

        # Scope engine fails closed on non-git dir
        nogit = tmp_path / "nogit_dir"
        nogit.mkdir()
        engine = GitScopeEngine(nogit)
        assert engine.is_git_repo() is False
        with pytest.raises(TodoError, match="not a git repository"):
            engine.changed_files()
    finally:
        db.close()
