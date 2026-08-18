from __future__ import annotations

import sqlite3
import sys
import traceback
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


def test_turso_backend_connects_directly_with_read_write_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from todo_db import DatabaseConfig, ProjectIdentity, TodoDatabase

    fake = FakeLibsql(tmp_path / "primary.sqlite")
    monkeypatch.setitem(sys.modules, "libsql", fake)
    url = "libsql://project.aws-us-east-1.turso.io"
    db = TodoDatabase.open(
        DatabaseConfig(
            path=url,
            identity=ProjectIdentity(project_id="project-test", repository="https://example.test/project"),
            auth_token="rw-token",
        )
    )

    assert db.project_identity.project_id == "project-test"
    assert fake.connect_calls[0] == {
        "database": url,
        "auth_token": "rw-token",
        "isolation_level": None,
    }
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

    assert fake.connect_calls[-1] == {"database": url, "auth_token": "ro-token", "isolation_level": None}
    readonly.close()


def test_read_only_resolution_prefers_ro_then_falls_back_to_rw(monkeypatch: pytest.MonkeyPatch) -> None:
    from todo_db import CredentialMode, DatabaseConfig
    from todo_db.backends import resolve_credential

    config = DatabaseConfig(path="libsql://resolver.example.test", credential_mode=CredentialMode.READ_ONLY)
    monkeypatch.setenv("TODO_DB_AUTH_TOKEN", "rw-fallback")
    resolved = resolve_credential(config)
    assert (resolved.source, resolved.capability, resolved.token) == (
        "TODO_DB_AUTH_TOKEN",
        "read-write",
        "rw-fallback",
    )

    monkeypatch.setenv("TODO_DB_RO_AUTH_TOKEN", "ro-preferred")
    resolved = resolve_credential(config)
    assert (resolved.source, resolved.capability, resolved.token) == (
        "TODO_DB_RO_AUTH_TOKEN",
        "read-only",
        "ro-preferred",
    )
    assert "ro-preferred" not in repr(resolved)


def test_empty_read_only_value_is_absent_and_explicit_token_has_external_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from todo_db import CredentialMode, DatabaseConfig
    from todo_db.backends import resolve_credential

    monkeypatch.setenv("TODO_DB_RO_AUTH_TOKEN", "")
    monkeypatch.setenv("TODO_DB_AUTH_TOKEN", "rw-fallback")
    config = DatabaseConfig(path="libsql://resolver.example.test", credential_mode=CredentialMode.READ_ONLY)
    assert resolve_credential(config).source == "TODO_DB_AUTH_TOKEN"

    explicit = resolve_credential(
        DatabaseConfig(
            path="libsql://resolver.example.test",
            credential_mode=CredentialMode.READ_ONLY,
            auth_token="explicit-token",
        )
    )
    assert (explicit.source, explicit.capability) == ("DatabaseConfig.auth_token", "unknown")
    assert "explicit-token" not in repr(explicit)


def test_missing_hosted_credential_is_a_coded_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from todo_db import CredentialMode, DatabaseConfig
    from todo_db.backends import resolve_credential
    from todo_db.errors import E_AUTH_MISSING, HostedAuthError

    monkeypatch.delenv("TODO_DB_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("TODO_DB_RO_AUTH_TOKEN", raising=False)
    with pytest.raises(HostedAuthError) as raised:
        resolve_credential(
            DatabaseConfig(path="libsql://resolver.example.test", credential_mode=CredentialMode.READ_ONLY)
        )
    assert raised.value.code == E_AUTH_MISSING
    assert "TODO_DB_RO_AUTH_TOKEN or TODO_DB_AUTH_TOKEN" in str(raised.value)


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


def test_hosted_read_write_outage_redacts_url_and_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from todo_db import DatabaseConfig, ProjectIdentity, TodoDatabase
    from todo_db.errors import TodoDBError

    url = "libsql://sensitive-project.example.test"
    token = "sensitive-write-token"
    fake = FakeLibsql(tmp_path / "primary.sqlite")

    def failed_connect(database, **kwargs):
        raise RuntimeError(f"cannot reach {url} using {token}")

    fake.connect = failed_connect
    monkeypatch.setitem(sys.modules, "libsql", fake)
    with pytest.raises(TodoDBError) as raised:
        TodoDatabase.open(
            DatabaseConfig(
                path=url,
                identity=ProjectIdentity(project_id="outage-test", repository="todo-db"),
                auth_token=token,
            )
        )
    message = str(raised.value)
    assert "hosted backend connection failed" in message
    assert url not in message
    assert token not in message
    assert "[REDACTED]" in message
    rendered = "".join(traceback.format_exception(raised.type, raised.value, raised.tb))
    assert url not in rendered
    assert token not in rendered


def test_hosted_read_only_outage_redacts_url_and_token(monkeypatch: pytest.MonkeyPatch) -> None:
    from todo_db import CredentialMode, DatabaseConfig, ProjectIdentity, TodoDatabase
    from todo_db.errors import TodoDBError

    url = "libsql://sensitive-readonly.example.test"
    token = "sensitive-read-token"
    fake = types.ModuleType("libsql")

    def failed_connect(database, **kwargs):
        raise RuntimeError(f"cannot reach {database} using {kwargs['auth_token']}")

    fake.connect = failed_connect
    monkeypatch.setitem(sys.modules, "libsql", fake)
    with pytest.raises(TodoDBError) as raised:
        TodoDatabase.open(
            DatabaseConfig(
                path=url,
                identity=ProjectIdentity(project_id="outage-test", repository="todo-db"),
                auth_token=token,
                credential_mode=CredentialMode.READ_ONLY,
            )
        )
    message = str(raised.value)
    assert "hosted backend connection failed" in message
    assert url not in message
    assert token not in message
    assert "[REDACTED]" in message
    rendered = "".join(traceback.format_exception(raised.type, raised.value, raised.tb))
    assert url not in rendered
    assert token not in rendered


def test_auth_shaped_connect_failure_raises_hosted_auth_error_with_remediation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from todo_db import DatabaseConfig, ProjectIdentity, TodoDatabase
    from todo_db.errors import HostedAuthError

    url = "libsql://auth-project.example.test"
    token = "expired-write-token"
    fake = types.ModuleType("libsql")

    def unauthorized_connect(database, **kwargs):
        raise ValueError(f"Hrana: api error: status=401, `The JWT is expired` for {database} ({kwargs['auth_token']})")

    fake.connect = unauthorized_connect
    monkeypatch.setitem(sys.modules, "libsql", fake)
    with pytest.raises(HostedAuthError) as raised:
        TodoDatabase.open(
            DatabaseConfig(
                path=url,
                identity=ProjectIdentity(project_id="auth-test", repository="todo-db"),
                auth_token=token,
                replica_path=tmp_path / "replica.sqlite",
            )
        )
    message = str(raised.value)
    assert "hosted backend connection failed" in message
    assert "credential rejected: replace the bounded credential from DatabaseConfig.auth_token" in message
    assert raised.value.code == "E_AUTH_REJECTED"
    assert "turso" not in message.lower()
    assert url not in message and token not in message
    assert "[REDACTED]" in message


def _scrub_cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for variable in (
        "TODO_DB_PROJECT_ID",
        "TODO_DB_REPOSITORY",
        "TODO_DB_PATH",
        "TODO_DB_URL",
        "TODO_DB_CONFIG",
        "TODO_DB_AUTH_TOKEN",
        "TODO_DB_RO_AUTH_TOKEN",
        "TODO_DB_AUTH_CONTRACT",
    ):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.chdir(tmp_path)


def test_cli_maps_hosted_auth_error_to_exit_4_with_redacted_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from todo_db.cli import main

    _scrub_cli_env(monkeypatch, tmp_path)
    url = "libsql://auth-cli.example.test"
    monkeypatch.setenv("TODO_DB_RO_AUTH_TOKEN", "expired-ro-token")
    monkeypatch.setenv("TODO_DB_AUTH_TOKEN", "valid-rw-token-must-not-be-used")
    monkeypatch.setenv("TODO_DB_AUTH_CONTRACT", "v2")
    fake = types.ModuleType("libsql")
    attempts: list[str] = []

    def unauthorized_connect(database, **kwargs):
        attempts.append(kwargs["auth_token"])
        raise ValueError(f"Hrana: api error: status=401, Unauthorized for {database} ({kwargs['auth_token']})")

    fake.connect = unauthorized_connect
    monkeypatch.setitem(sys.modules, "libsql", fake)

    assert main(["--db", url, "list"]) == 4
    err = capsys.readouterr().err
    assert "replace the bounded credential from TODO_DB_RO_AUTH_TOKEN" in err
    assert "turso" not in err.lower()
    assert url not in err and "expired-ro-token" not in err
    assert "valid-rw-token-must-not-be-used" not in err
    assert "[REDACTED]" in err
    assert "E_AUTH_REJECTED" in err
    assert attempts == ["expired-ro-token"]


def test_cli_uses_legacy_safe_exit_2_without_v2_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from todo_db.cli import main

    _scrub_cli_env(monkeypatch, tmp_path)
    monkeypatch.setenv("TODO_DB_RO_AUTH_TOKEN", "rejected-ro-token")
    fake = types.ModuleType("libsql")

    def unauthorized_connect(database, **kwargs):
        raise ValueError("Hrana: status=401 Unauthorized")

    fake.connect = unauthorized_connect
    monkeypatch.setitem(sys.modules, "libsql", fake)

    assert main(["--db", "libsql://legacy-contract.example.test", "list"]) == 2
    error = capsys.readouterr().err
    assert "E_AUTH_REJECTED" in error
    assert "legacy-safe exit 2" in error
    assert "TODO_DB_AUTH_CONTRACT=v2" in error
    assert "rejected-ro-token" not in error


@pytest.mark.parametrize(("contract", "expected"), [(None, 2), ("v2", 4)])
def test_cli_missing_token_exit_depends_on_v2_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    contract: str | None,
    expected: int,
) -> None:
    from todo_db.cli import main

    _scrub_cli_env(monkeypatch, tmp_path)
    if contract:
        monkeypatch.setenv("TODO_DB_AUTH_CONTRACT", contract)

    assert main(["--db", "libsql://missing-token.example.test", "list"]) == expected
    error = capsys.readouterr().err
    assert "E_AUTH_MISSING" in error
    assert ("legacy-safe exit 2" in error) is (contract is None)


@pytest.mark.parametrize(
    ("detail", "expected"),
    [
        ("Hrana status=401 Unauthorized", True),
        ("status=400 JWT error: InvalidToken", True),
        ("The JWT is expired", True),
        ("HTTP 403 Forbidden", True),
        ("HTTP 403 quota exceeded", False),
        ("TLS authority validation failed", False),
        ("token bucket exhausted", False),
        ("database suspended by policy", False),
        ("write denied by read-only policy", False),
    ],
)
def test_auth_classifier_uses_high_confidence_evidence(detail: str, expected: bool) -> None:
    from todo_db.backends import is_auth_shaped

    assert is_auth_shaped(detail) is expected


def test_cli_keeps_non_auth_hosted_errors_generic_with_exit_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from todo_db.cli import main

    _scrub_cli_env(monkeypatch, tmp_path)
    monkeypatch.setenv("TODO_DB_RO_AUTH_TOKEN", "ro-token")
    fake = types.ModuleType("libsql")

    def unreachable_connect(database, **kwargs):
        raise RuntimeError("connection reset by peer")

    fake.connect = unreachable_connect
    monkeypatch.setitem(sys.modules, "libsql", fake)

    assert main(["--db", "libsql://outage-cli.example.test", "list"]) == 2
    err = capsys.readouterr().err
    assert "hosted backend connection failed" in err
    assert "credential rejected" not in err


def _open_hosted_tracker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from todo_db import DatabaseConfig, ProjectIdentity, TodoDatabase, TodoTracker

    fake = FakeLibsql(tmp_path / "primary.sqlite")
    monkeypatch.setitem(sys.modules, "libsql", fake)
    database = TodoDatabase.open(
        DatabaseConfig(
            path="libsql://project.aws-us-east-1.turso.io",
            identity=ProjectIdentity(project_id="hosted-verify", repository="todo-db"),
            auth_token="rw-token",
            replica_path=tmp_path / "replica.sqlite",
        )
    )
    tracker = TodoTracker(database, actor="hosted-actor")
    tracker.create_item(
        item_id="verify-item",
        title="Verify item",
        worktree="todo-db",
        priority="medium",
        description="Carries a stored verification command.",
        verifications=[{"description": "smoke", "command": "printf PASS", "expected": "PASS"}],
    )
    return database, tracker


def test_hosted_verify_run_refuses_stored_commands_without_explicit_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from todo_db.errors import TodoError

    monkeypatch.delenv("TODO_DB_ALLOW_HOSTED_VERIFY_RUN", raising=False)
    database, tracker = _open_hosted_tracker(monkeypatch, tmp_path)
    with pytest.raises(TodoError, match="lateral code-execution") as raised:
        tracker.run_verification("verify-item", 1)
    assert "TODO_DB_ALLOW_HOSTED_VERIFY_RUN" in str(raised.value)
    tracker.claim("verify-item")
    with pytest.raises(TodoError, match="lateral code-execution"):
        tracker.complete("verify-item")
    assert tracker.get_item("verify-item")["state"] == "active"
    database.close()


def test_hosted_verify_run_executes_when_explicitly_allowed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TODO_DB_ALLOW_HOSTED_VERIFY_RUN", "1")
    database, tracker = _open_hosted_tracker(monkeypatch, tmp_path)
    result, output = tracker.run_verification("verify-item", 1)
    assert result == "pass"
    assert "PASS" in output
    tracker.claim("verify-item")
    tracker.complete("verify-item")
    assert tracker.get_item("verify-item")["state"] == "done"
    database.close()


def test_local_verify_run_is_ungated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from todo_db import DatabaseConfig, ProjectIdentity, TodoDatabase, TodoTracker

    monkeypatch.delenv("TODO_DB_ALLOW_HOSTED_VERIFY_RUN", raising=False)
    database = TodoDatabase.open(
        DatabaseConfig(
            path=tmp_path / "local.sqlite",
            identity=ProjectIdentity(project_id="local-verify", repository="todo-db"),
        )
    )
    tracker = TodoTracker(database, actor="local-actor")
    tracker.create_item(
        item_id="verify-item",
        title="Verify item",
        worktree="todo-db",
        priority="medium",
        description="A local database runs stored verifications unchanged.",
        verifications=[{"description": "smoke", "command": "printf PASS", "expected": "PASS"}],
    )
    result, _ = tracker.run_verification("verify-item", 1)
    assert result == "pass"
    database.close()


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


def test_replica_sidecar_triggers_hosted_verify_gate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from todo_db import DatabaseConfig, ProjectIdentity, TodoDatabase, TodoTracker
    from todo_db.errors import TodoError

    monkeypatch.delenv("TODO_DB_ALLOW_HOSTED_VERIFY_RUN", raising=False)
    db_path = tmp_path / "replica.db"
    (tmp_path / "replica.db-info").write_bytes(b"sidecar")
    database = TodoDatabase.open(
        DatabaseConfig(
            path=db_path,
            identity=ProjectIdentity(project_id="replica-verify", repository="todo-db"),
        )
    )
    tracker = TodoTracker(database, actor="tester")
    tracker.create_item(
        item_id="item-rep",
        title="Replica item",
        worktree="todo-db",
        priority="high",
        description="Replica verify gate test",
        verifications=[{"description": "v1", "command": "echo PASS", "expected": "PASS"}],
    )
    with pytest.raises(TodoError, match="TODO_DB_ALLOW_HOSTED_VERIFY_RUN"):
        tracker.run_verification("item-rep", 1)
    database.close()


def test_hosted_execute_and_commit_errors_are_redacted_and_classified(tmp_path: Path) -> None:
    from todo_db.backends import HostedConnection
    from todo_db.errors import HostedAuthError

    class FlakyRaw:
        def execute(self, sql, params):
            raise Exception("HTTP 401 Unauthorized: token expired for https://secret.turso.io with token secret-123")

        def commit(self):
            raise Exception("HTTP 403 Forbidden: stream closed")

    conn = HostedConnection(
        FlakyRaw(),
        url="https://secret.turso.io",
        token="secret-123",
        token_variable="TODO_DB_AUTH_TOKEN",
    )
    with pytest.raises(HostedAuthError) as exc:
        conn.execute("SELECT 1")
    msg = str(exc.value)
    assert "secret-123" not in msg
    assert "https://secret.turso.io" not in msg
    assert "[REDACTED]" in msg
    assert "credential rejected" in msg
    assert exc.value.code == "E_AUTH_REJECTED"

    with pytest.raises(HostedAuthError):
        conn.commit()


def test_hosted_non_auth_execute_error_is_redacted_without_auth_classification() -> None:
    from todo_db.backends import HostedConnection
    from todo_db.errors import HostedAuthError, TodoDBError

    class NetworkFailure:
        def execute(self, sql, params):
            raise Exception(
                "TLS authority validation failed for https://secret.turso.io using credential secret-123"
            )

    conn = HostedConnection(
        NetworkFailure(),
        url="https://secret.turso.io",
        token="secret-123",
        token_variable="TODO_DB_AUTH_TOKEN",
    )
    with pytest.raises(TodoDBError) as raised:
        conn.execute("SELECT 1")
    assert not isinstance(raised.value, HostedAuthError)
    assert "authority validation failed" in str(raised.value)
    assert "https://secret.turso.io" not in str(raised.value)
    assert "secret-123" not in str(raised.value)

    class OperationalNetworkFailure:
        def execute(self, sql, params):
            raise sqlite3.OperationalError("connection reset for https://secret.turso.io with secret-123")

    operational = HostedConnection(
        OperationalNetworkFailure(),
        url="https://secret.turso.io",
        token="secret-123",
    )
    with pytest.raises(TodoDBError) as operational_error:
        operational.execute("SELECT 1")
    assert "https://secret.turso.io" not in str(operational_error.value)
    assert "secret-123" not in str(operational_error.value)
