"""`todo-db doctor`: read-only preflight with classified exit codes (0 healthy, 2 generic, 4 auth)."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from test_hosted_backend import FakeLibsql


ENVIRONMENT = (
    "TODO_DB_PROJECT_ID",
    "TODO_DB_REPOSITORY",
    "TODO_DB_PATH",
    "TODO_DB_URL",
    "TODO_DB_CONFIG",
    "TODO_DB_AUTH_TOKEN",
    "TODO_DB_RO_AUTH_TOKEN",
    "TODO_DB_AUTH_CONTRACT",
    "TODO_DB_FINDING_DRAFTS_DIR",
)


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for variable in ENVIRONMENT:
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TODO_DB_FINDING_DRAFTS_DIR", str(tmp_path / "drafts"))


def _scaffold(tmp_path: Path) -> None:
    from todo_db.cli import main

    assert main(["init-project", "--project-id", "doctor-test", "--repository", "todo-db"]) == 0


def test_doctor_passes_on_a_healthy_scaffolded_local_repo(tmp_path: Path, capsys) -> None:
    from todo_db.cli import main

    _scaffold(tmp_path)
    capsys.readouterr()
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "PASS config:" in out
    assert "PASS identity:" in out and "doctor-test" in out
    assert "PASS database:" in out and "schema v" in out
    assert "PASS finding-drafts:" in out
    assert "FAIL" not in out


def test_doctor_json_emits_check_records_and_exit_hint(tmp_path: Path, capsys) -> None:
    from todo_db.cli import main

    _scaffold(tmp_path)
    capsys.readouterr()
    assert main(["doctor", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["exit"] == 0
    names = [check["name"] for check in payload["checks"]]
    assert names == ["config", "identity", "database", "finding-drafts"]
    for check in payload["checks"]:
        assert check["status"] in {"PASS", "WARN", "FAIL"}
        assert check["detail"]


def test_doctor_fails_with_exit_4_when_the_hosted_probe_is_auth_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    from todo_db.cli import main

    monkeypatch.setenv("TODO_DB_AUTH_TOKEN", "expired-doctor-token")
    monkeypatch.setenv("TODO_DB_AUTH_CONTRACT", "v2")
    empty_bin = tmp_path / "emptybin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    url = "libsql://doctor-auth.example.test"
    fake = types.ModuleType("libsql")

    def unauthorized_connect(database, **kwargs):
        raise ValueError(f"Hrana: api error: status=401, Unauthorized for {database} ({kwargs['auth_token']})")

    fake.connect = unauthorized_connect
    monkeypatch.setitem(sys.modules, "libsql", fake)

    assert main(["--db", url, "doctor"]) == 4
    out = capsys.readouterr().out
    assert "FAIL database:" in out
    assert "E_AUTH_REJECTED" in out
    assert "credential: TODO_DB_AUTH_TOKEN (read-write)" in out
    assert "turso" not in out.lower()
    assert url not in out and "expired-doctor-token" not in out
    assert "[REDACTED]" in out


def test_doctor_json_uses_legacy_safe_exit_and_reports_auth_provenance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    from todo_db.cli import main

    monkeypatch.setenv("TODO_DB_RO_AUTH_TOKEN", "rejected-ro-token")
    fake = types.ModuleType("libsql")

    def unauthorized_connect(database, **kwargs):
        raise ValueError("Hrana: status=401 Unauthorized")

    fake.connect = unauthorized_connect
    monkeypatch.setitem(sys.modules, "libsql", fake)

    assert main(["--db", "libsql://doctor-legacy.example.test", "doctor", "--json"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["exit"] == 2
    database = next(check for check in payload["checks"] if check["name"] == "database")
    assert database["code"] == "E_AUTH_REJECTED"
    assert database["source"] == "TODO_DB_RO_AUTH_TOKEN"
    assert database["capability"] == "read-only"
    assert any(check["name"] == "auth-contract" for check in payload["checks"])
    assert "rejected-ro-token" not in json.dumps(payload)


def test_doctor_reports_missing_hosted_token_as_auth_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    from todo_db.cli import main

    empty_bin = tmp_path / "emptybin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    monkeypatch.setitem(sys.modules, "libsql", types.ModuleType("libsql"))
    monkeypatch.setenv("TODO_DB_AUTH_CONTRACT", "v2")

    assert main(["--db", "libsql://doctor-notoken.example.test", "doctor"]) == 4
    out = capsys.readouterr().out
    assert "FAIL database: hosted read access requires a credential" in out
    assert "TODO_DB_RO_AUTH_TOKEN or TODO_DB_AUTH_TOKEN" in out
    assert "TODO_DB_CREDENTIAL_COMMAND" in out
    assert "Provision once" in out
    assert "code: E_AUTH_MISSING" in out


def test_doctor_passes_against_a_healthy_hosted_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    from todo_db import DatabaseConfig, ProjectIdentity, TodoDatabase
    from todo_db.cli import main

    url = "libsql://doctor-hosted.example.test"
    fake = FakeLibsql(tmp_path / "primary.sqlite")
    monkeypatch.setitem(sys.modules, "libsql", fake)
    TodoDatabase.open(
        DatabaseConfig(
            path=url,
            identity=ProjectIdentity(project_id="doctor-hosted", repository="todo-db"),
            auth_token="rw-token",
        )
    ).close()
    empty_bin = tmp_path / "emptybin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    monkeypatch.setenv("TODO_DB_AUTH_TOKEN", "rw-token")

    assert main(["--db", url, "doctor"]) == 0
    out = capsys.readouterr().out
    assert "PASS database: read-only probe ok" in out
    assert "credential: TODO_DB_AUTH_TOKEN (read-write)" in out
    assert "PASS identity:" in out and "doctor-hosted" in out
    assert "turso-cli" not in out

    assert main(["--db", url, "doctor", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    database_check = next(check for check in payload["checks"] if check["name"] == "database")
    assert database_check["source"] == "TODO_DB_AUTH_TOKEN"
    assert database_check["capability"] == "read-write"
    assert "rw-token" not in json.dumps(payload)


def test_doctor_warns_when_the_local_schema_is_behind(tmp_path: Path, capsys) -> None:
    import sqlite3

    from todo_db import database as database_module
    from todo_db.cli import main

    path = tmp_path / "behind.sqlite"
    connection = sqlite3.connect(path)
    for version, name, checksum in database_module._migration_files()[:-1]:
        for statement in database_module._split_statements(database_module._migration_sql(version)):
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_migrations(version, name, checksum, applied_at, tool_version) VALUES (?, ?, ?, ?, ?)",
            (version, name, checksum, "2026-01-01T00:00:00Z", "test"),
        )
    connection.execute(
        "INSERT INTO project_identity(singleton, project_id, repository) VALUES (1, 'behind-test', 'todo-db')"
    )
    connection.commit()
    connection.close()

    assert main(["--db", str(path), "doctor"]) == 0
    out = capsys.readouterr().out
    assert "WARN database:" in out
    assert "behind -- run init to migrate" in out


def test_doctor_reports_provider_provenance_without_the_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    """ADR 0005 G4: doctor names the provider as the source and never its output."""

    import json as _json

    from todo_db import DatabaseConfig, ProjectIdentity, TodoDatabase
    from todo_db.backends import reset_credential_provider_cache
    from todo_db.cli import main

    url = "libsql://doctor-provider.example.test"
    fake = FakeLibsql(tmp_path / "primary.sqlite")
    monkeypatch.setitem(sys.modules, "libsql", fake)
    TodoDatabase.open(
        DatabaseConfig(
            path=url,
            identity=ProjectIdentity(project_id="doctor-provider", repository="todo-db"),
            auth_token="ro-token",
        )
    ).close()

    provider = tmp_path / "provider.sh"
    provider.write_text('#!/bin/sh\necho "ro-token"\n')
    provider.chmod(0o755)
    empty_bin = tmp_path / "emptybin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    monkeypatch.delenv("TODO_DB_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("TODO_DB_RO_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("TODO_DB_CREDENTIAL_COMMAND", str(provider))
    reset_credential_provider_cache()

    assert main(["--db", url, "doctor"]) == 0
    out = capsys.readouterr().out
    assert "credential: TODO_DB_CREDENTIAL_COMMAND (requested:read-only)" in out
    assert "ro-token" not in out

    assert main(["--db", url, "doctor", "--json"]) == 0
    payload = _json.loads(capsys.readouterr().out)
    database_check = next(check for check in payload["checks"] if check["name"] == "database")
    assert database_check["source"] == "TODO_DB_CREDENTIAL_COMMAND"
    assert database_check["capability"] == "requested:read-only"
    assert "ro-token" not in _json.dumps(payload)
    reset_credential_provider_cache()
