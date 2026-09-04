import subprocess
from pathlib import Path

import pytest

from todo_db import DatabaseConfig, ProjectIdentity, TodoDatabase, TodoTracker
from todo_db.agent import GitScopeEngine
from todo_db.errors import TodoError


def test_git_scope_engine_captures_state(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)

    engine = GitScopeEngine(tmp_path)
    state = engine.capture_state()
    assert state.is_clean is True

    # Create a file and commit
    f = tmp_path / "hello.txt"
    f.write_text("hello", encoding="utf-8")
    subprocess.run(["git", "add", "hello.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, check=True)

    state = engine.capture_state()
    assert state.head_sha is not None
    assert state.is_clean is True


def test_git_scope_engine_handles_renames_and_untracked(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)

    (tmp_path / "old_name.txt").write_text("content", encoding="utf-8")
    subprocess.run(["git", "add", "old_name.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "base commit"], cwd=tmp_path, check=True)
    base_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()

    # Rename file and add an untracked file
    subprocess.run(["git", "mv", "old_name.txt", "new_name.txt"], cwd=tmp_path, check=True)
    (tmp_path / "untracked.py").write_text("print(1)", encoding="utf-8")

    engine = GitScopeEngine(tmp_path)
    changed = engine.changed_files(base=base_sha)

    # Both old and new rename paths plus untracked file must be included
    assert "old_name.txt" in changed
    assert "new_name.txt" in changed
    assert "untracked.py" in changed


def test_git_scope_engine_detects_unreachable_and_diverged_baselines(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)

    (tmp_path / "init.txt").write_text("initial", encoding="utf-8")
    subprocess.run(["git", "add", "init.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True)

    engine = GitScopeEngine(tmp_path)

    # 1. Unreachable baseline
    with pytest.raises(TodoError, match="E_BASE_UNREACHABLE"):
        engine.changed_files(base="0000000000000000000000000000000000000000")

    # 2. Diverged baseline: create branch A, commit, branch B, commit
    branch_a_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    (tmp_path / "file_a.txt").write_text("a", encoding="utf-8")
    subprocess.run(["git", "add", "file_a.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "commit on branch a"], cwd=tmp_path, check=True)
    diverged_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()

    # Checkout previous commit, make another commit (creating divergence)
    subprocess.run(["git", "checkout", "-b", "branch-b", branch_a_sha], cwd=tmp_path, check=True)
    (tmp_path / "file_b.txt").write_text("b", encoding="utf-8")
    subprocess.run(["git", "add", "file_b.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "commit on branch b"], cwd=tmp_path, check=True)

    # Calling changed_files with diverged_sha from branch-b
    with pytest.raises(TodoError, match="E_BASE_DIVERGED"):
        engine.changed_files(base=diverged_sha)


def test_tracker_rebaseline_scope_audit_event(tmp_path: Path) -> None:
    db_path = tmp_path / "rebaseline.sqlite"
    config = DatabaseConfig(
        path=db_path,
        identity=ProjectIdentity(project_id="rebaseline-test", repository="todo-db"),
    )
    database = TodoDatabase.open(config)
    tracker = TodoTracker(database, actor="operator")
    tracker.create_item(
        item_id="item-base",
        title="Rebaseline Item",
        worktree="todo-db",
        priority="high",
        description="Testing rebaseline operation",
    )
    claim = tracker.claim("item-base", git_baseline="oldsha123")
    token = claim["claim_token"]

    tracker.rebaseline_scope(
        "item-base", new_baseline="newsha456", reason="Rebased onto main branch", claim_token=token
    )
    item = tracker.get_item("item-base")
    assert item["git_baseline"] == "newsha456"

    # Audit events must record rebaseline
    database.verify_audit()
    events = database.connection.execute("SELECT action, detail FROM events WHERE action = 'rebaseline'").fetchall()
    assert len(events) == 1
    assert "oldsha123" in events[0]["detail"]
    assert "newsha456" in events[0]["detail"]
    database.close()
