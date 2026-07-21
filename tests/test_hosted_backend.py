from __future__ import annotations

import sqlite3
import sys
import types
from pathlib import Path

import pytest


class FakeCursor:
    def __init__(self, cursor: sqlite3.Cursor):
        self._cursor = cursor

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def __iter__(self):
        return iter(self._cursor.fetchall())

    @property
    def description(self):
        return self._cursor.description

    @property
    def lastrowid(self):
        return self._cursor.lastrowid

    @property
    def rowcount(self):
        return self._cursor.rowcount


class FakeRawConnection:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        self._connection.isolation_level = None
        self.sync_calls = 0

    def execute(self, sql: str, params=()):
        try:
            return FakeCursor(self._connection.execute(sql, tuple(params)))
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"Hrana: SQLite error: {exc}; code: SQLITE_CONSTRAINT") from None

    def commit(self):
        self._connection.commit()

    def rollback(self):
        self._connection.rollback()

    def close(self):
        self._connection.close()

    def sync(self):
        self.sync_calls += 1


class FakeLibsql(types.ModuleType):
    def __init__(self, primary: Path):
        super().__init__("libsql")
        self.primary = primary
        self.connect_calls: list[dict[str, object]] = []
        self.connections: list[FakeRawConnection] = []

    def connect(self, database, **kwargs):
        target = self.primary if "://" in str(database) else Path(database)
        self.connect_calls.append({"database": str(database), **kwargs})
        connection = FakeRawConnection(target)
        self.connections.append(connection)
        return connection


def test_turso_backend_uses_replica_and_read_write_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from todo_db import DatabaseConfig, ProjectIdentity, TodoDatabase

    fake = FakeLibsql(tmp_path / "primary.sqlite")
    monkeypatch.setitem(sys.modules, "libsql", fake)
    replica = tmp_path / "replica.sqlite"
    db = TodoDatabase.open(
        DatabaseConfig(
            path="libsql://project.aws-us-east-1.turso.io",
            identity=ProjectIdentity(project_id="project-test", repository="https://example.test/project"),
            auth_token="rw-token",
            replica_path=replica,
        )
    )

    assert db.project_identity.project_id == "project-test"
    assert fake.connect_calls[0] == {
        "database": str(replica),
        "sync_url": "libsql://project.aws-us-east-1.turso.io",
        "auth_token": "rw-token",
        "isolation_level": None,
    }
    assert fake.connections[0].sync_calls == 1
    db.close()


def test_turso_read_only_uses_read_only_token_against_primary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from todo_db import CredentialMode, DatabaseConfig, ProjectIdentity, TodoDatabase

    fake = FakeLibsql(tmp_path / "primary.sqlite")
    monkeypatch.setitem(sys.modules, "libsql", fake)
    url = "libsql://project.aws-us-east-1.turso.io"
    identity = ProjectIdentity(project_id="project-test", repository="https://example.test/project")
    TodoDatabase.open(
        DatabaseConfig(path=url, identity=identity, auth_token="rw-token", replica_path=tmp_path / "primary.sqlite")
    ).close()

    monkeypatch.setenv("TODO_DB_RO_AUTH_TOKEN", "ro-token")
    readonly = TodoDatabase.open(DatabaseConfig(path=url, identity=identity, credential_mode=CredentialMode.READ_ONLY))

    assert fake.connect_calls[-1] == {"database": url, "auth_token": "ro-token"}
    readonly.close()


def test_turso_backend_rejects_plaintext_urls(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from todo_db import DatabaseConfig, ProjectIdentity, TodoDatabase
    from todo_db.errors import TodoDBError

    monkeypatch.setitem(sys.modules, "libsql", FakeLibsql(tmp_path / "primary.sqlite"))
    with pytest.raises(TodoDBError, match="plaintext"):
        TodoDatabase.open(
            DatabaseConfig(
                path="http://project.example.test",
                identity=ProjectIdentity(project_id="project-test", repository="https://example.test/project"),
                auth_token="rw-token",
            )
        )


def test_hosted_tracker_lifecycle_uses_same_transactional_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from todo_db import DatabaseConfig, ProjectIdentity, TodoDatabase, TodoTracker

    fake = FakeLibsql(tmp_path / "primary.sqlite")
    monkeypatch.setitem(sys.modules, "libsql", fake)
    database = TodoDatabase.open(
        DatabaseConfig(
            path="libsql://project.aws-us-east-1.turso.io",
            identity=ProjectIdentity(project_id="hosted-test", repository="todo-db"),
            auth_token="rw-token",
            replica_path=tmp_path / "replica.sqlite",
        )
    )
    tracker = TodoTracker(database, actor="hosted-actor")
    tracker.create_item(
        item_id="hosted-item",
        title="Hosted item",
        worktree="todo-db",
        priority="medium",
        description="A hosted lifecycle test item.",
        work=[{"id": "w0", "summary": "Run hosted test"}],
    )
    tracker.claim("hosted-item")
    tracker.done_unit("hosted-item", "w0", "hosted evidence")
    tracker.complete("hosted-item")
    assert database.verify_audit()["event_count"] == 4
    database.close()
