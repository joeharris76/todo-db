from __future__ import annotations

import sqlite3
import tomllib
import json
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_project_metadata_and_console_contract() -> None:
    manifest = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = manifest["project"]

    assert project["name"] == "todo-db"
    assert project["requires-python"] == ">=3.10"
    assert project["scripts"] == {
        "todo-db": "todo_db.cli:main",
        "todo": "todo_db.cli:main",
    }
    assert manifest["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["src/todo_db"]


def test_database_config_accepts_hosted_targets_without_coercing_them_to_paths() -> None:
    from todo_db import DatabaseConfig, ProjectIdentity

    config = DatabaseConfig(
        path="libsql://project.aws-us-east-1.turso.io",
        identity=ProjectIdentity(project_id="project-test", repository="https://example.test/project"),
    )

    assert config.path == "libsql://project.aws-us-east-1.turso.io"
    assert config.is_hosted is True


def test_public_import_surface() -> None:
    from todo_db import CredentialMode, DatabaseConfig, ProjectIdentity, TodoDatabase
    from todo_db.errors import ProjectIdentityMismatchError, SchemaMismatchError

    assert CredentialMode.READ_WRITE.value == "read-write"
    assert CredentialMode.READ_ONLY.value == "read-only"
    assert DatabaseConfig and ProjectIdentity and TodoDatabase and ProjectIdentityMismatchError and SchemaMismatchError


def test_cli_accepts_global_db_before_subcommand(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from todo_db.cli import main

    db_path = tmp_path / "todo.sqlite"
    assert (
        main(
            [
                "--db",
                str(db_path),
                "init",
                "--project-id",
                "project-test",
                "--repository",
                "https://example.test/project",
            ]
        )
        == 0
    )
    assert "schema" in capsys.readouterr().out.lower()


def test_sqlite_bootstrap_binds_project_identity(tmp_path: Path) -> None:
    from todo_db import DatabaseConfig, ProjectIdentity, TodoDatabase

    identity = ProjectIdentity(project_id="project-test", repository="https://example.test/project")
    db = TodoDatabase.open(DatabaseConfig(path=tmp_path / "todo.sqlite", identity=identity))

    assert db.project_identity == identity
    assert db.schema_version == 3
    db.close()


def test_project_identity_mismatch_is_rejected_before_access(tmp_path: Path) -> None:
    from todo_db import DatabaseConfig, ProjectIdentity, TodoDatabase
    from todo_db.errors import ProjectIdentityMismatchError

    path = tmp_path / "todo.sqlite"
    TodoDatabase.open(
        DatabaseConfig(
            path=path,
            identity=ProjectIdentity(project_id="project-a", repository="https://example.test/a"),
        )
    ).close()

    with pytest.raises(ProjectIdentityMismatchError, match="project identity mismatch"):
        TodoDatabase.open(
            DatabaseConfig(
                path=path,
                identity=ProjectIdentity(project_id="project-b", repository="https://example.test/b"),
            )
        )


def test_cli_reports_identity_mismatch_as_a_failed_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from todo_db.cli import main

    db_path = tmp_path / "todo.sqlite"
    assert (
        main(
            [
                "--db",
                str(db_path),
                "init",
                "--project-id",
                "project-a",
                "--repository",
                "https://example.test/a",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert (
        main(
            [
                "--db",
                str(db_path),
                "init",
                "--project-id",
                "project-b",
                "--repository",
                "https://example.test/b",
            ]
        )
        == 2
    )
    assert "project identity mismatch" in capsys.readouterr().err


def test_cli_exports_and_verifies_the_audit_chain(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from todo_db import DatabaseConfig, ProjectIdentity, TodoDatabase
    from todo_db.cli import main

    db_path = tmp_path / "todo.sqlite"
    identity = ProjectIdentity(project_id="project-test", repository="https://example.test/project")
    db = TodoDatabase.open(DatabaseConfig(path=db_path, identity=identity))
    db.record_event(actor="test", action="probe", detail={"value": 7})
    db.close()

    export_path = tmp_path / "export.json"
    assert (
        main(
            [
                "--db",
                str(db_path),
                "export",
                "--project-id",
                identity.project_id,
                "--repository",
                identity.repository,
                "--output",
                str(export_path),
            ]
        )
        == 0
    )
    assert json.loads(export_path.read_text(encoding="utf-8"))["integrity"]["event_count"] == 1
    capsys.readouterr()

    assert (
        main(
            [
                "--db",
                str(db_path),
                "audit",
                "verify",
                "--project-id",
                identity.project_id,
                "--repository",
                identity.repository,
            ]
        )
        == 0
    )
    assert '"head_seq": 1' in capsys.readouterr().out


def test_local_read_only_mode_rejects_writes(tmp_path: Path) -> None:
    from todo_db import CredentialMode, DatabaseConfig, ProjectIdentity, TodoDatabase

    path = tmp_path / "todo.sqlite"
    identity = ProjectIdentity(project_id="project-test", repository="https://example.test/project")
    writable = TodoDatabase.open(DatabaseConfig(path=path, identity=identity))
    writable.set_metadata("example", "value")
    writable.close()

    readonly = TodoDatabase.open(DatabaseConfig(path=path, identity=identity, credential_mode=CredentialMode.READ_ONLY))
    assert readonly.get_metadata("example") == "value"
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        readonly.set_metadata("example", "changed")
    readonly.close()


def test_export_envelope_preserves_metadata_and_audit_events(tmp_path: Path) -> None:
    from todo_db import DatabaseConfig, ProjectIdentity, TodoDatabase

    identity = ProjectIdentity(project_id="project-test", repository="https://example.test/project")
    db = TodoDatabase.open(DatabaseConfig(path=tmp_path / "todo.sqlite", identity=identity))
    db.set_metadata("lint.require_scope_rules", "on")
    db.record_event(actor="test", action="probe", detail={"value": 7})

    exported = db.export()

    assert exported["format_version"] == 2
    assert exported["project"] == {
        "project_id": "project-test",
        "repository": "https://example.test/project",
    }
    assert exported["schema"]["version"] == 3
    assert exported["metadata"]["lint.require_scope_rules"] == "on"
    assert exported["events"][-1]["action"] == "probe"
    assert exported["events"][-1]["detail"] == {"value": 7}
    assert "tables" in exported
    db.close()


def test_migration_checksum_is_recorded_and_tampering_is_rejected(tmp_path: Path) -> None:
    from todo_db import DatabaseConfig, ProjectIdentity, TodoDatabase
    from todo_db.errors import SchemaMismatchError

    path = tmp_path / "todo.sqlite"
    identity = ProjectIdentity(project_id="project-test", repository="https://example.test/project")
    TodoDatabase.open(DatabaseConfig(path=path, identity=identity)).close()

    raw = sqlite3.connect(path)
    migration = raw.execute("SELECT version, checksum FROM schema_migrations").fetchone()
    assert migration[0] == 1
    assert len(migration[1]) == 64
    raw.execute("UPDATE schema_migrations SET checksum = 'tampered'")
    raw.commit()
    raw.close()

    with pytest.raises(SchemaMismatchError, match="schema migration mismatch"):
        TodoDatabase.open(DatabaseConfig(path=path, identity=identity))


def test_prior_package_refuses_a_database_with_a_newer_schema(tmp_path: Path) -> None:
    from todo_db import DatabaseConfig, ProjectIdentity, TodoDatabase
    from todo_db.errors import SchemaMismatchError

    path = tmp_path / "future.sqlite"
    identity = ProjectIdentity(project_id="project-test", repository="https://example.test/project")
    TodoDatabase.open(DatabaseConfig(path=path, identity=identity)).close()
    raw = sqlite3.connect(path)
    raw.execute(
        "INSERT INTO schema_migrations(version, name, checksum, applied_at, tool_version) "
        "VALUES (4, 'future', 'future-checksum', '2026-01-01T00:00:00Z', '0.2.0')"
    )
    raw.commit()
    raw.close()

    with pytest.raises(SchemaMismatchError, match="schema migration mismatch"):
        TodoDatabase.open(DatabaseConfig(path=path, identity=identity))


def test_export_is_deterministic_for_unchanged_state(tmp_path: Path) -> None:
    from todo_db import DatabaseConfig, ProjectIdentity, TodoDatabase

    identity = ProjectIdentity(project_id="project-test", repository="https://example.test/project")
    db = TodoDatabase.open(DatabaseConfig(path=tmp_path / "todo.sqlite", identity=identity))
    db.record_event(actor="test", action="probe", detail={"value": 7})

    assert json.dumps(db.export(), sort_keys=True) == json.dumps(db.export(), sort_keys=True)
    db.close()


def test_export_can_be_restored_with_a_verified_audit_chain(tmp_path: Path) -> None:
    from todo_db import DatabaseConfig, ProjectIdentity, TodoDatabase, TodoTracker

    identity = ProjectIdentity(project_id="restore-test", repository="todo-db")
    source = TodoDatabase.open(DatabaseConfig(path=tmp_path / "source.sqlite", identity=identity))
    tracker = TodoTracker(source, actor="restore-test")
    tracker.create_item(
        item_id="restore-item",
        title="Restore item",
        worktree="todo-db",
        priority="medium",
        description="An item used to validate lossless restore.",
    )
    source.set_metadata("backup", "included")
    exported = source.export()
    for row in exported["schema"]["migrations"]:
        row["applied_at"] = "2026-01-01T00:00:00Z"
    for row in exported["tables"]["schema_migrations"]:
        row["applied_at"] = "2026-01-01T00:00:00Z"
    source.close()

    restored = TodoDatabase.open(DatabaseConfig(path=tmp_path / "restored.sqlite", identity=identity))
    restored.restore(exported)
    assert restored.export()["events"] == exported["events"]
    assert restored.get_metadata("backup") == "included"
    assert restored.export()["tables"]["items"] == exported["tables"]["items"]
    assert restored.export()["tables"]["schema_migrations"] == exported["tables"]["schema_migrations"]
    assert restored.export() == exported
    restored.close()


def test_legacy_snapshot_restore_preserves_tables_and_rehashes_events(tmp_path: Path) -> None:
    from todo_db import DatabaseConfig, ProjectIdentity, TodoDatabase
    from todo_db.errors import SchemaMismatchError

    identity = ProjectIdentity(project_id="benchbox", repository="https://github.com/joeharris76/BenchBox")
    database = TodoDatabase.open(DatabaseConfig(path=tmp_path / "restored.sqlite", identity=identity))
    database.set_metadata("stale-target-value", "must be replaced")
    snapshot = {
        "items": [
            {
                "id": "legacy-item",
                "title": "Legacy item",
                "worktree": "main",
                "priority": "medium",
                "state": "planning",
                "blocked_reason": None,
                "category": None,
                "description": "A legacy item restored into standalone storage.",
                "approach": None,
                "claimed_by": None,
                "claimed_at": None,
                "created_at": "2026-01-01T00:00:00Z",
                "completed_at": None,
                "completed_pr": None,
                "work": [],
                "deps": [],
                "scope": [],
                "verifications": [],
                "preserves": [],
                "anti_patterns": [],
                "prior_art": [],
                "deferrals": [],
            }
        ],
        "events": [
            {
                "seq": 1,
                "at": "2026-01-01T00:00:01Z",
                "actor": "legacy",
                "item_id": "legacy-item",
                "action": "create",
                "detail": '{"state":"planning","title":"Legacy item"}',
            }
        ],
        "meta": [{"key": "schema_version", "value": "2"}],
    }
    for table in (
        "work_units",
        "work_needs",
        "item_deps",
        "scope_rules",
        "verifications",
        "preserves",
        "anti_patterns",
        "prior_art",
        "deferrals",
    ):
        snapshot[table] = []

    database.restore_legacy(snapshot)
    exported = database.export()

    assert exported["tables"]["items"][0]["id"] == "legacy-item"
    assert exported["tables"]["meta"] == [{"key": "schema_version", "value": "2"}]
    assert exported["metadata"] == {}
    assert exported["events"][0]["detail"] == {
        "item_id": "legacy-item",
        "state": "planning",
        "title": "Legacy item",
    }
    assert database.verify_audit()["event_count"] == 1
    assert len(exported["tables"]["schema_migrations"]) == 3

    before_failed_restore = database.export()
    missing_table = {key: value for key, value in snapshot.items() if key != "deferrals"}
    with pytest.raises(SchemaMismatchError, match="missing required tables: deferrals"):
        database.restore_legacy(missing_table)
    assert database.export() == before_failed_restore

    malformed = {**snapshot, "items": [snapshot["items"][0], snapshot["items"][0]]}
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        database.restore_legacy(malformed)
    assert database.export() == before_failed_restore
    database.close()
