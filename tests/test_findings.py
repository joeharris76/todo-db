from __future__ import annotations

import hashlib
import sqlite3
from importlib import resources
from pathlib import Path

import pytest

from todo_db import DatabaseConfig, FindingsTracker, ProjectIdentity, TodoDatabase, TodoTracker
from todo_db.database import SCHEMA_VERSION
from todo_db.errors import TodoError


DRAFT_STEM = "2026-07-25-101010-example-finding"
SECOND_STEM = "2026-07-25-111111-second-finding"
IDENTITY = ProjectIdentity(project_id="findings-test", repository="todo-db")


def open_db(tmp_path: Path) -> TodoDatabase:
    return TodoDatabase.open(DatabaseConfig(path=tmp_path / "todo.sqlite", identity=IDENTITY))


def common(db_path: Path) -> list[str]:
    return ["--db", str(db_path), "--project-id", IDENTITY.project_id, "--repository", IDENTITY.repository]


def write_draft(
    directory: Path,
    stem: str = DRAFT_STEM,
    *,
    status: str = "open",
    finding_text: str = "Reviews miss a class of gaps.",
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        f"id: {stem}",
        "date: 2026-07-25",
        f"status: {status}",
        "finding_kind: framework-gap",
        'review_context: "unit test"',
        "evidence:",
        "  - path: src/todo_db/findings.py",
        '    pattern: "sync_drafts"',
        "---",
        "",
        "# Example finding",
        "",
        "## Finding",
        finding_text,
        "",
        "## Why this matters",
        "The class recurs across reviews.",
        "",
        "## Suggested next steps",
        "- [ ] add a check",
        "",
    ]
    path = directory / f"{stem}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def test_default_drafts_dir_is_project_scoped_and_env_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    from todo_db.findings import default_drafts_dir

    monkeypatch.delenv("TODO_DB_FINDING_DRAFTS_DIR", raising=False)
    assert default_drafts_dir("proj-a") == Path.home() / ".todo-db" / "finding-drafts" / "proj-a"
    monkeypatch.setenv("TODO_DB_FINDING_DRAFTS_DIR", "/tmp/custom-drafts")
    assert default_drafts_dir("proj-a") == Path("/tmp/custom-drafts")


def test_cli_finding_sync_lands_drafts_and_is_idempotent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from todo_db.cli import main

    drafts = tmp_path / "drafts"
    write_draft(drafts)
    db_path = tmp_path / "todo.sqlite"
    assert main([*common(db_path), "init"]) == 0
    assert main([*common(db_path), "finding", "sync", "--drafts-dir", str(drafts)]) == 0
    assert "synced 1" in capsys.readouterr().out
    assert (drafts / f"{DRAFT_STEM}.md.synced").exists()

    write_draft(drafts)
    assert main([*common(db_path), "finding", "sync", "--drafts-dir", str(drafts)]) == 0
    assert "skipped 1" in capsys.readouterr().out

    write_draft(drafts, finding_text="Different content this time.")
    assert main([*common(db_path), "finding", "sync", "--drafts-dir", str(drafts)]) == 2
    assert "sync conflict" in capsys.readouterr().err

    database = open_db(tmp_path)
    landed = FindingsTracker(database, actor="test").list_findings()
    database.close()
    assert [row["id"] for row in landed] == [DRAFT_STEM]
    assert landed[0]["disposition"] == "open"


def test_cli_finding_sync_rejects_invalid_drafts(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from todo_db.cli import main

    drafts = tmp_path / "drafts"
    drafts.mkdir(parents=True)
    (drafts / f"{DRAFT_STEM}.md").write_text(
        "---\n"
        f"id: {DRAFT_STEM}\n"
        "date: 2026-07-25\n"
        "status: open\n"
        "finding_kind: framework-gap\n"
        'review_context: "unit test"\n'
        "---\n"
        "\n"
        "# Missing sections\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "todo.sqlite"
    assert main([*common(db_path), "init"]) == 0
    assert main([*common(db_path), "finding", "sync", "--drafts-dir", str(drafts)]) == 2
    assert "failed validation" in capsys.readouterr().err
    capsys.readouterr()
    database = open_db(tmp_path)
    assert FindingsTracker(database, actor="test").list_findings() == []
    database.close()


def test_disposition_machine_requires_reasons_and_blocks_terminal_moves(tmp_path: Path) -> None:
    database = open_db(tmp_path)
    try:
        service = FindingsTracker(database, actor="test-actor")
        drafts = tmp_path / "drafts"
        write_draft(drafts)
        assert service.sync_drafts(drafts)["synced"] == [DRAFT_STEM]

        with pytest.raises(TodoError, match="triage needs"):
            service.triage(DRAFT_STEM)
        with pytest.raises(TodoError, match="requires a --reason"):
            service.dismiss(DRAFT_STEM, "  ")

        service.triage(DRAFT_STEM, urgency="high", disposition="actionable", reason="still load-bearing")
        finding = service.get_finding(DRAFT_STEM)
        assert finding["disposition"] == "actionable"
        assert finding["urgency"] == "high"
        assert finding["disposition_reason"] == "still load-bearing"

        service.dismiss(DRAFT_STEM, "not worth pursuing")
        with pytest.raises(TodoError, match="terminal"):
            service.triage(DRAFT_STEM, disposition="actioned")
        events = [event["action"] for event in service.get_finding(DRAFT_STEM)["events"]]
        assert events == ["sync", "triage", "disposition", "disposition"]
    finally:
        database.close()


def test_link_targets_are_validated(tmp_path: Path) -> None:
    database = open_db(tmp_path)
    try:
        service = FindingsTracker(database, actor="test-actor")
        tracker = TodoTracker(database, actor="test-actor")
        drafts = tmp_path / "drafts"
        write_draft(drafts)
        write_draft(drafts, stem=SECOND_STEM)
        service.sync_drafts(drafts)
        tracker.create_item(
            item_id="linked-item",
            title="Linked item",
            worktree="todo-db",
            priority="medium",
            description="An item that a finding informs.",
        )

        service.link(DRAFT_STEM, kind="informs", target_item="linked-item", note="context")
        service.link(DRAFT_STEM, kind="related-finding", target_finding=SECOND_STEM)
        with pytest.raises(TodoError, match="promoted-to"):
            service.link(DRAFT_STEM, kind="promoted-to", target_item="linked-item")
        with pytest.raises(TodoError, match="exactly one"):
            service.link(DRAFT_STEM, kind="informs")
        with pytest.raises(TodoError, match="does not exist"):
            service.link(DRAFT_STEM, kind="informs", target_item="missing-item")
        links = service.get_finding(DRAFT_STEM)["links"]
        assert [link["kind"] for link in links] == ["informs", "related-finding"]
    finally:
        database.close()


def test_promote_is_atomic_and_terminal(tmp_path: Path) -> None:
    database = open_db(tmp_path)
    try:
        service = FindingsTracker(database, actor="test-actor")
        tracker = TodoTracker(database, actor="test-actor")
        drafts = tmp_path / "drafts"
        write_draft(drafts)
        service.sync_drafts(drafts)
        tracker.create_item(
            item_id="existing-item",
            title="Existing item",
            worktree="todo-db",
            priority="medium",
            description="Occupies the id a failed promote targets.",
        )

        with pytest.raises(TodoError, match="cannot (promote|create item)"):
            service.promote(DRAFT_STEM, "existing-item")
        finding = service.get_finding(DRAFT_STEM)
        assert finding["disposition"] == "open"
        assert finding["links"] == []

        service.promote(DRAFT_STEM, "promoted-item")
        finding = service.get_finding(DRAFT_STEM)
        assert finding["disposition"] == "promoted"
        assert finding["links"] == [
            {
                "kind": "promoted-to",
                "target_item": "promoted-item",
                "target_finding": None,
                "note": "promoted by test-actor",
            }
        ]
        assert tracker.get_item("promoted-item")["state"] == "planning"
        with pytest.raises(TodoError, match="terminal"):
            service.promote(DRAFT_STEM, "another-item")
        actions = [event["action"] for event in database.export()["events"]]
        assert actions[-2:] == ["create", "finding-promote"]
    finally:
        database.close()


def test_finding_mutations_keep_the_audit_chain_verifiable(tmp_path: Path) -> None:
    database = open_db(tmp_path)
    service = FindingsTracker(database, actor="test-actor")
    drafts = tmp_path / "drafts"
    write_draft(drafts)
    service.sync_drafts(drafts)
    service.dismiss(DRAFT_STEM, "recorded for the audit test")
    actions = [event["action"] for event in database.export()["events"]]
    assert "finding-sync" in actions
    assert "finding-disposition" in actions
    assert database.verify_audit()["event_count"] == len(actions)
    database.close()

    reopened = open_db(tmp_path)
    reopened.verify_audit()
    reopened.close()


def test_export_restore_round_trip_includes_findings(tmp_path: Path) -> None:
    database = open_db(tmp_path)
    service = FindingsTracker(database, actor="round-trip")
    drafts = tmp_path / "drafts"
    write_draft(drafts)
    service.sync_drafts(drafts)
    service.promote(DRAFT_STEM, "promoted-item")
    exported = database.export()
    database.close()

    assert exported["tables"]["findings"][0]["id"] == DRAFT_STEM
    assert exported["tables"]["finding_evidence"][0]["path"] == "src/todo_db/findings.py"
    assert exported["tables"]["finding_links"][0]["kind"] == "promoted-to"
    assert [row["action"] for row in exported["tables"]["finding_events"]] == ["sync", "promote"]

    restored = TodoDatabase.open(DatabaseConfig(path=tmp_path / "restored.sqlite", identity=IDENTITY))
    restored.restore(exported)
    assert restored.export() == exported
    restored.close()


def test_pre_findings_database_upgrades_cleanly(tmp_path: Path) -> None:
    path = tmp_path / "todo.sqlite"
    raw = sqlite3.connect(path)
    for version, name in ((1, "initial"), (2, "audit_integrity"), (3, "tracker")):
        sql = resources.files("todo_db.migrations").joinpath(f"00{version}_{name}.sql").read_text(encoding="utf-8")
        raw.executescript(sql)
        raw.execute(
            "INSERT INTO schema_migrations(version, name, checksum, applied_at, tool_version) "
            "VALUES (?, ?, ?, '2026-01-01T00:00:00Z', '0.1.0')",
            (version, name, hashlib.sha256(sql.encode("utf-8")).hexdigest()),
        )
    raw.execute(
        "INSERT INTO project_identity(singleton, project_id, repository) VALUES (1, ?, ?)",
        (IDENTITY.project_id, IDENTITY.repository),
    )
    raw.commit()
    raw.close()

    database = open_db(tmp_path)
    assert database.schema_version == SCHEMA_VERSION
    tables = {row["name"] for row in database.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"findings", "finding_evidence", "finding_links", "finding_events"} <= tables
    service = FindingsTracker(database, actor="upgrade-test")
    drafts = tmp_path / "drafts"
    write_draft(drafts)
    assert service.sync_drafts(drafts)["synced"] == [DRAFT_STEM]
    database.close()


TRIGGER_MIGRATION = """CREATE TABLE trigger_fixture (id INTEGER PRIMARY KEY, note TEXT);

CREATE TRIGGER trigger_fixture_default AFTER INSERT ON trigger_fixture
WHEN NEW.note IS NULL
BEGIN
  UPDATE trigger_fixture SET note = 'defaulted; by trigger' WHERE id = NEW.id;
END;

INSERT INTO trigger_fixture (id, note) VALUES (1, 'literal; semicolon');
"""


def test_migration_runner_applies_triggers_and_quoted_semicolons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from todo_db import database as database_module

    checksum = hashlib.sha256(TRIGGER_MIGRATION.encode("utf-8")).hexdigest()
    real_files = database_module._migration_files
    real_sql = database_module._migration_sql
    fixture_version = SCHEMA_VERSION + 1
    monkeypatch.setattr(
        database_module,
        "_migration_files",
        lambda: [*real_files(), (fixture_version, "trigger_fixture", checksum)],
    )
    monkeypatch.setattr(
        database_module,
        "_migration_sql",
        lambda version: TRIGGER_MIGRATION if version == fixture_version else real_sql(version),
    )

    database = open_db(tmp_path)
    assert database.schema_version == fixture_version
    with database.transaction():
        database.connection.execute("INSERT INTO trigger_fixture (id) VALUES (2)")
    rows = {row["id"]: row["note"] for row in database.connection.execute("SELECT id, note FROM trigger_fixture")}
    assert rows == {1: "literal; semicolon", 2: "defaulted; by trigger"}
    database.close()


def test_finding_sync_via_env_drafts_dir_lands_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from todo_db.cli import main

    drafts = tmp_path / "drafts"
    monkeypatch.setenv("TODO_DB_FINDING_DRAFTS_DIR", str(drafts))
    db_path = tmp_path / "todo.sqlite"
    assert main([*common(db_path), "init"]) == 0
    write_draft(drafts)
    assert main([*common(db_path), "finding", "sync"]) == 0
    write_draft(drafts, stem=SECOND_STEM)
    capsys.readouterr()

    database = open_db(tmp_path)
    tracker = FindingsTracker(database, actor="test")
    assert tracker.stats()["findings_by_disposition"] == {"open": 1}
    database.close()
