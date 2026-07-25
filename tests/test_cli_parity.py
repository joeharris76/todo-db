"""CLI compatibility assertions derived from the BenchBox tracker boundary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_default_db_prefers_local_path_over_hosted_url(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """BenchBox todo_db.py:303-320 pins TODO_DB_PATH above TODO_DB_URL."""
    from todo_db.cli import _default_db

    monkeypatch.setenv("TODO_DB_PATH", str(tmp_path / "local.sqlite"))
    monkeypatch.setenv("TODO_DB_URL", "libsql://production.example")
    assert _default_db() == str(tmp_path / "local.sqlite")


def test_default_actor_matches_legacy_harness_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """BenchBox todo_db.py:205-216 gives Claude precedence over Codex."""
    from todo_db.tracker import default_actor

    monkeypatch.setenv("CLAUDE_SESSION_ID", "claude-session")
    monkeypatch.setenv("CODEX_SESSION_ID", "codex-session")
    assert default_actor() == "claude-session"


def test_version_handshake(capsys: pytest.CaptureFixture[str]) -> None:
    from importlib import metadata

    from todo_db.cli import main

    with pytest.raises(SystemExit) as raised:
        main(["--version"])
    assert raised.value.code == 0
    assert capsys.readouterr().out == f"todo-db {metadata.version('todo-db')}\n"


def test_policy_findings_return_one_with_legacy_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from todo_db import DatabaseConfig, ProjectIdentity, TodoDatabase, TodoTracker
    from todo_db.cli import main

    path = tmp_path / "todo.sqlite"
    identity = ProjectIdentity(project_id="cli-parity", repository="todo-db")
    database = TodoDatabase.open(DatabaseConfig(path=path, identity=identity))
    TodoTracker(database, actor="test").create_item(
        item_id="lint-item",
        title="Lint item",
        worktree="local",
        priority="medium",
        description="Item intentionally missing verification rows.",
        work=[{"id": "w0", "summary": "Exercise policy output"}],
        scope=[("only_modify", "src/**")],
    )
    database.close()
    common = ["--db", str(path), "--project-id", identity.project_id, "--repository", identity.repository]

    assert main([*common, "lint", "lint-item"]) == 1
    output = capsys.readouterr()
    assert output.err == ""
    assert "lint-item: no verification steps recorded" in output.out
    assert "1 finding(s) across 1 item(s)" in output.out
    assert main([*common, "check-scope", "lint-item", "docs/outside.md"]) == 1
    output = capsys.readouterr()
    assert output.err == ""
    assert output.out == "docs/outside.md: outside only_modify allowlist\n"


def test_create_from_ignores_benign_unknown_payload_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from todo_db.cli import main

    path = tmp_path / "todo.sqlite"
    payload = {
        "id": "payload-item",
        "title": "Payload item",
        "worktree": "local",
        "priority": "medium",
        "description": "Payload containing a compatibility-only key.",
        "compatibility_note": "ignored",
    }
    monkeypatch.setattr("sys.stdin.read", lambda: json.dumps(payload))
    common = ["--db", str(path), "--project-id", "cli-parity", "--repository", "todo-db"]
    assert main([*common, "create", "--from", "-"]) == 0
    assert "created payload-item" in capsys.readouterr().out


def test_local_import_replace_is_refused_without_deleting_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from todo_db import DatabaseConfig, ProjectIdentity, TodoDatabase, TodoTracker
    from todo_db.cli import main

    path = tmp_path / "todo.sqlite"
    identity = ProjectIdentity(project_id="cli-parity", repository="todo-db")
    database = TodoDatabase.open(DatabaseConfig(path=path, identity=identity))
    tracker = TodoTracker(database, actor="test")
    tracker.create_item(
        item_id="keep-item",
        title="Keep item",
        worktree="local",
        priority="medium",
        description="This item must survive a refused replacement.",
    )
    database.close()
    source = tmp_path / "TODO"
    source.mkdir()
    common = ["--db", str(path), "--project-id", identity.project_id, "--repository", identity.repository]
    assert main([*common, "import-yaml", "--todo-dir", str(source), "--replace"]) == 2
    assert "--replace only applies to a live import into the hosted backend" in capsys.readouterr().err
    database = TodoDatabase.open(DatabaseConfig(path=path, identity=identity))
    assert TodoTracker(database).get_item("keep-item")["id"] == "keep-item"
    database.close()


def test_broken_pipe_returns_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from todo_db.cli import main

    monkeypatch.setattr("builtins.print", lambda *args, **kwargs: (_ for _ in ()).throw(BrokenPipeError()))
    assert main(["--db", str(tmp_path / "todo.sqlite"), "init"]) == 0
