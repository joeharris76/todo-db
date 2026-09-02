from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from todo_db import DatabaseConfig, ProjectIdentity, TodoDatabase, TodoTracker


def _seed_item(
    db_path: Path,
    project_id: str,
    *,
    item_id: str,
    actor: str = "seed",
    verify: list[dict[str, str]] | None = None,
    claim_actor: str | None = None,
    finish_units: bool = False,
) -> None:
    """Seed an item (and optionally claim / finish its work) via the tracker API.

    The lifecycle-mutation CLI verbs were removed in 0.6.0 (MCP is the agent
    surface); the floor CLI keeps only ``complete`` / ``export`` / ``audit`` etc.
    """

    config = DatabaseConfig(
        path=db_path,
        identity=ProjectIdentity(project_id=project_id, repository="todo-db"),
    )
    database = TodoDatabase.open(config)
    try:
        tracker = TodoTracker(database, actor=actor)
        tracker.create_item(
            item_id=item_id,
            title=f"Item {item_id}",
            worktree="todo-db",
            priority="high",
            description="A seeded item for an end-to-end floor-CLI test.",
            work=[{"id": "w0", "summary": "Run the test", "needs": []}],
            verifications=verify or [],
        )
        if claim_actor is not None:
            holder = TodoTracker(database, actor=claim_actor)
            holder.claim(item_id)
            if finish_units:
                holder.done_unit(item_id, "w0", "seeded evidence")
    finally:
        database.close()


def test_cli_complete_help_exposes_verification_override(capsys) -> None:
    from todo_db.cli import main

    with pytest.raises(SystemExit) as raised:
        main(["complete", "--help"])
    assert raised.value.code == 0
    help_text = capsys.readouterr().out
    assert "verification ladder" in help_text
    assert "--override-verification REASON" in help_text


def test_cli_complete_and_export_close_the_floor_lifecycle(tmp_path: Path, capsys) -> None:
    from todo_db.cli import main

    db_path = tmp_path / "standalone.sqlite"
    common = ["--db", str(db_path), "--project-id", "cli-test", "--repository", "todo-db", "--actor", "cli-actor"]
    assert main([*common, "init"]) == 0
    capsys.readouterr()
    _seed_item(
        db_path,
        "cli-test",
        item_id="cli-item",
        verify=[{"description": "smoke", "command": "printf PASS", "expected": "PASS"}],
        claim_actor="cli-actor",
        finish_units=True,
    )
    assert main([*common, "complete", "cli-item"]) == 0
    export_path = tmp_path / "export.json"
    assert main([*common, "export", "--output", str(export_path)]) == 0
    assert json.loads(export_path.read_text(encoding="utf-8"))["tables"]["items"][0]["state"] == "done"
    assert "cli-item done" in capsys.readouterr().out


def test_cli_complete_requires_passing_verification_or_audited_override(tmp_path: Path, capsys) -> None:
    from todo_db.cli import main

    db_path = tmp_path / "standalone.sqlite"
    common = [
        "--db",
        str(db_path),
        "--project-id",
        "cli-complete-test",
        "--repository",
        "todo-db",
        "--actor",
        "cli-actor",
    ]
    assert main([*common, "init"]) == 0
    capsys.readouterr()
    _seed_item(
        db_path,
        "cli-complete-test",
        item_id="failing-item",
        verify=[{"description": "vacuous selector", "command": "exit 5"}],
        claim_actor="cli-actor",
        finish_units=True,
    )
    capsys.readouterr()
    assert main([*common, "complete", "failing-item"]) == 2
    assert "verification seq=1 failed" in capsys.readouterr().err

    reason = "External release gate supplied equivalent evidence"
    assert main([*common, "complete", "failing-item", "--override-verification", reason]) == 0
    capsys.readouterr()
    export_path = tmp_path / "completion-export.json"
    assert main([*common, "export", "--output", str(export_path)]) == 0
    completion = [
        event
        for event in json.loads(export_path.read_text(encoding="utf-8"))["events"]
        if event["action"] == "complete"
    ][-1]
    assert completion["actor"] == "cli-actor"
    assert completion["detail"]["verification_override"]["reason"] == reason


def test_cli_yaml_import_requires_explicit_source_and_preserves_items(tmp_path: Path, capsys) -> None:
    from todo_db.cli import main

    todo_dir = tmp_path / "TODO"
    todo_dir.mkdir()
    (todo_dir / "example.yaml").write_text(
        "id: yaml-item\n"
        "title: Imported item\n"
        "worktree: todo-db\n"
        "priority: High\n"
        "description: Imported from the legacy YAML tree.\n"
        "work:\n"
        "  - id: w0\n"
        "    summary: Import and validate\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "standalone.sqlite"
    common = ["--db", str(db_path), "--project-id", "yaml-test", "--repository", "todo-db"]
    assert main([*common, "import-yaml", "--todo-dir", str(todo_dir)]) == 0
    assert "yaml-item" in capsys.readouterr().out
    config = DatabaseConfig(
        path=db_path,
        identity=ProjectIdentity(project_id="yaml-test", repository="todo-db"),
    )
    database = TodoDatabase.open(config)
    try:
        assert TodoTracker(database, actor="check").get_item("yaml-item")["id"] == "yaml-item"
    finally:
        database.close()


def _git_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text(".todo-db/\n*.sqlite*\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "init", "--quiet"], cwd=tmp_path, check=True)
    return tmp_path


def test_cli_verify_run_previews_runs_and_attests_without_completing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    from todo_db.cli import main

    _git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".todo-db").mkdir()
    db_path = tmp_path / ".todo-db" / "standalone.sqlite"
    common = ["--db", str(db_path), "--project-id", "vr-cli", "--repository", "todo-db", "--actor", "srv-principal"]
    assert main([*common, "init"]) == 0
    capsys.readouterr()

    config = DatabaseConfig(path=db_path, identity=ProjectIdentity(project_id="vr-cli", repository="todo-db"))
    database = TodoDatabase.open(config)
    try:
        TodoTracker(database, actor="seed").create_item(
            item_id="vr-item",
            title="Verify-run CLI item",
            worktree="todo-db",
            priority="high",
            description="A floor verify-run smoke item.",
            verifications=[{"description": "smoke", "command": "true"}],
        )
        TodoTracker(database, actor="srv-principal").claim("vr-item")
        token = TodoTracker(database, actor="srv-principal").get_item("vr-item")["claim_token"]
    finally:
        database.close()

    # --actor is required and must name the claim holder.
    assert main(["--db", str(db_path), "verify-run", "vr-item"]) == 2
    assert "requires --actor" in capsys.readouterr().err

    assert main([*common, "verify-run", "vr-item", "--claim-token", token]) == 0
    out, err = capsys.readouterr()
    assert "[1] true" in err  # every stored command is previewed
    assert json.loads(out)["status"] == "attested"

    database = TodoDatabase.open(config)
    try:
        assert TodoTracker(database, actor="check").get_item("vr-item")["state"] == "active"
    finally:
        database.close()


def test_cli_rebaseline_records_an_audited_baseline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    from todo_db.cli import main

    _git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".todo-db").mkdir()
    db_path = tmp_path / ".todo-db" / "standalone.sqlite"
    common = ["--db", str(db_path), "--project-id", "rb-cli", "--repository", "todo-db", "--actor", "srv-principal"]
    assert main([*common, "init"]) == 0
    capsys.readouterr()

    config = DatabaseConfig(path=db_path, identity=ProjectIdentity(project_id="rb-cli", repository="todo-db"))
    database = TodoDatabase.open(config)
    try:
        TodoTracker(database, actor="seed").create_item(
            item_id="rb-item",
            title="Rebaseline CLI item",
            worktree="todo-db",
            priority="medium",
            description="A floor rebaseline smoke item.",
        )
        TodoTracker(database, actor="srv-principal").claim("rb-item")
        token = TodoTracker(database, actor="srv-principal").get_item("rb-item")["claim_token"]
    finally:
        database.close()

    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert main([*common, "rebaseline", "rb-item", "--reason", "confirm clean head", "--claim-token", token]) == 0
    capsys.readouterr()

    database = TodoDatabase.open(config)
    try:
        assert TodoTracker(database, actor="check").get_item("rb-item")["git_baseline"] == head
    finally:
        database.close()


def test_lease_timestamp_format_compatibility(tmp_path: Path) -> None:
    import sqlite3
    from todo_db import DatabaseConfig, ProjectIdentity, TodoDatabase, TodoTracker

    db_path = tmp_path / "lease_compat.sqlite"
    config = DatabaseConfig(
        path=db_path,
        identity=ProjectIdentity(project_id="lease-test", repository="https://example.test/lease"),
    )
    db = TodoDatabase.open(config)
    tracker = TodoTracker(db, actor="user-1")
    tracker.create_item(
        item_id="item-malformed",
        title="Malformed Lease Item",
        worktree="todo-db",
        priority="high",
        description="Item with legacy timestamp",
    )

    # Insert a non-standard formatted timestamp
    raw = sqlite3.connect(db_path)
    raw.execute(
        "UPDATE items SET claimed_by = 'other-user', claimed_at = '2026/01/01 10:00:00' WHERE id = 'item-malformed'"
    )
    raw.commit()
    raw.close()

    # 1. ready_items treats the malformed/expired lease as ready
    ready = tracker.ready_items()
    assert any(i["id"] == "item-malformed" for i in ready)

    # 2. claim can claim it without error
    order = tracker.claim("item-malformed")
    assert order["id"] == "item-malformed"
    assert order["claimed_by"] == "user-1"
    db.close()
