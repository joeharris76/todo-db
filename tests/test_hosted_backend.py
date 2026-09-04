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


# The keyword arguments libsql.connect actually accepts in the calls this
# package makes. The double rejects anything else: a permissive double would
# accept a misspelled or invented keyword and pass, while real libsql raised.
_LIBSQL_CONNECT_KEYWORDS = frozenset({"auth_token", "isolation_level"})


class FakeLibsql(types.ModuleType):
    def __init__(self, primary: Path):
        super().__init__("libsql")
        self.primary = primary
        self.connect_calls: list[dict[str, object]] = []
        self.connections: list[FakeRawConnection] = []

    def connect(self, database, **kwargs):
        unexpected = sorted(set(kwargs) - _LIBSQL_CONNECT_KEYWORDS)
        if unexpected:
            raise TypeError(f"libsql.connect() got unexpected keyword argument(s): {', '.join(unexpected)}")
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
    TodoDatabase.open(DatabaseConfig(path=url, identity=identity, auth_token="rw-token")).close()

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


@pytest.mark.parametrize("url", ["http://project.example.test", "ws://project.example.test"])
def test_turso_backend_rejects_cleartext_transports(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, url: str
) -> None:
    """`ws://` carries the bearer token in cleartext exactly as `http://` does.

    The scheme check is an allowlist, so a transport is refused unless it is
    known to be encrypted -- adding a libsql scheme must not silently open a
    plaintext path.
    """

    from todo_db import DatabaseConfig, ProjectIdentity, TodoDatabase
    from todo_db.errors import TodoDBError

    monkeypatch.setitem(sys.modules, "libsql", FakeLibsql(tmp_path / "primary.sqlite"))
    with pytest.raises(TodoDBError, match="plaintext"):
        TodoDatabase.open(
            DatabaseConfig(
                path=url,
                identity=ProjectIdentity(project_id="project-test", repository="https://example.test/project"),
                auth_token="rw-token",
            )
        )


@pytest.mark.parametrize("url", ["https://p.example.test", "libsql://p.example.test", "wss://p.example.test"])
def test_turso_backend_accepts_encrypted_transports(url: str) -> None:
    from todo_db.backends import _secure_url

    assert _secure_url(url) == url


@pytest.mark.parametrize(
    "detail",
    [
        "authentication failed for database",
        "credential expired",
        "credentials rejected by server",
        "HTTP 401 Unauthorized",
    ],
)
def test_auth_classifier_matches_unambiguous_auth_prose(detail: str) -> None:
    from todo_db.backends import is_auth_shaped

    assert is_auth_shaped(detail)


@pytest.mark.parametrize("detail", ["429 quota exceeded", "connection reset by peer", "database is suspended"])
def test_auth_classifier_leaves_ambiguous_failures_generic(detail: str) -> None:
    """Ambiguity must stay generic so a caller never auto-mints a credential."""

    from todo_db.backends import is_auth_shaped

    assert not is_auth_shaped(detail)


def test_hosted_write_refuses_when_foreign_keys_cannot_be_enforced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The schema relies on ON DELETE CASCADE; a silent downgrade orphans rows."""

    from todo_db import DatabaseConfig, ProjectIdentity, TodoDatabase
    from todo_db.errors import TodoDBError

    fake = FakeLibsql(tmp_path / "primary.sqlite")
    real_connect = fake.connect

    def connect(url, **kwargs):
        raw = real_connect(url, **kwargs)
        original_execute = raw.execute

        def execute(sql, *args, **kwargs):
            if "foreign_keys" in str(sql).lower():
                raise RuntimeError("PRAGMA not supported over hrana")
            return original_execute(sql, *args, **kwargs)

        raw.execute = execute
        return raw

    fake.connect = connect
    monkeypatch.setitem(sys.modules, "libsql", fake)

    with pytest.raises(TodoDBError, match="foreign_keys"):
        TodoDatabase.open(
            DatabaseConfig(
                path="libsql://project.example.test",
                identity=ProjectIdentity(project_id="project-test", repository="https://example.test/project"),
                auth_token="rw-token",
            )
        )


def test_hosted_read_write_outage_redacts_url_and_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from todo_db import DatabaseConfig, ProjectIdentity, TodoDatabase
    from todo_db.errors import TodoDBError

    url = "libsql://sensitive-project.example.test/database?region=private"
    translated = "https://sensitive-project.example.test/database?region=private"
    authority = "sensitive-project.example.test:443"
    token = "sensitive-write-token"
    fake = FakeLibsql(tmp_path / "primary.sqlite")

    def failed_connect(database, **kwargs):
        raise RuntimeError(f"cannot reach {translated} through {authority} using {token}")

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
    assert translated not in message
    assert "sensitive-project.example.test" not in message
    assert authority not in message
    assert token not in message
    assert "[REDACTED]" in message
    rendered = "".join(traceback.format_exception(raised.type, raised.value, raised.tb))
    assert url not in rendered
    assert translated not in rendered
    assert "sensitive-project.example.test" not in rendered
    assert authority not in rendered
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

    assert main(["--db", url, "audit", "verify"]) == 4
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

    assert main(["--db", "libsql://legacy-contract.example.test", "audit", "verify"]) == 2
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

    assert main(["--db", "libsql://missing-token.example.test", "audit", "verify"]) == expected
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

    assert main(["--db", "libsql://outage-cli.example.test", "audit", "verify"]) == 2
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

    configured = "libsql://secret.turso.io/database?region=private"
    translated = "https://secret.turso.io/database?region=private"
    authority = "secret.turso.io:443"

    class NetworkFailure:
        def execute(self, sql, params):
            raise Exception(
                f"TLS authority validation failed for {translated} through {authority} using secret-123"
            )

    conn = HostedConnection(
        NetworkFailure(),
        url=configured,
        token="secret-123",
        token_variable="TODO_DB_AUTH_TOKEN",
    )
    with pytest.raises(TodoDBError) as raised:
        conn.execute("SELECT 1")
    assert not isinstance(raised.value, HostedAuthError)
    assert "authority validation failed" in str(raised.value)
    assert configured not in str(raised.value)
    assert translated not in str(raised.value)
    assert "secret.turso.io" not in str(raised.value)
    assert authority not in str(raised.value)
    assert "secret-123" not in str(raised.value)

    class OperationalNetworkFailure:
        def execute(self, sql, params):
            raise sqlite3.OperationalError(f"connection reset for {translated} via {authority} with secret-123")

    operational = HostedConnection(
        OperationalNetworkFailure(),
        url=configured,
        token="secret-123",
    )
    with pytest.raises(TodoDBError) as operational_error:
        operational.execute("SELECT 1")
    assert configured not in str(operational_error.value)
    assert translated not in str(operational_error.value)
    assert "secret.turso.io" not in str(operational_error.value)
    assert authority not in str(operational_error.value)
    assert "secret-123" not in str(operational_error.value)


def _provider_script(tmp_path: Path, name: str, body: str) -> str:
    """Write an executable stub standing in for an operator's secret store.

    A literal ``\\n`` in ``body`` is a line break. Writing it through unchanged
    would collapse the stub to a single malformed line, which is how several of
    these stubs first passed without running the logic they claimed to test.
    """

    script = tmp_path / name
    lines = body.replace("\\n", "\n")
    script.write_text(f"#!/usr/bin/env bash\nset -u\n{lines}\n")
    script.chmod(0o755)
    return str(script)


@pytest.fixture(autouse=True)
def _clear_provider_cache():
    from todo_db.backends import reset_credential_provider_cache

    reset_credential_provider_cache()
    yield
    reset_credential_provider_cache()


def _hosted_config(mode):
    from todo_db import DatabaseConfig

    return DatabaseConfig(path="libsql://provider.example.test", credential_mode=mode)


def _no_injected_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TODO_DB_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("TODO_DB_RO_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("TODO_DB_CREDENTIAL_COMMAND", raising=False)


def test_credential_provider_supplies_read_write_when_nothing_is_injected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from todo_db import CredentialMode
    from todo_db.backends import resolve_credential

    _no_injected_credentials(monkeypatch)
    provider = _provider_script(tmp_path, "provider.sh", 'echo "rw-from-store"')
    monkeypatch.setenv("TODO_DB_CREDENTIAL_COMMAND", provider)

    resolved = resolve_credential(_hosted_config(CredentialMode.READ_WRITE))
    assert (resolved.source, resolved.capability, resolved.token) == (
        "TODO_DB_CREDENTIAL_COMMAND",
        "requested:read-write",
        "rw-from-store",
    )
    assert "rw-from-store" not in repr(resolved)


def test_credential_provider_receives_the_requested_capability(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from todo_db import CredentialMode
    from todo_db.backends import resolve_credential

    _no_injected_credentials(monkeypatch)
    provider = _provider_script(
        tmp_path,
        "capability.sh",
        'test $# -eq 0 || exit 64\necho "token-for-$TODO_DB_CREDENTIAL_CAPABILITY"',
    )
    monkeypatch.setenv("TODO_DB_CREDENTIAL_COMMAND", provider)

    resolved = resolve_credential(_hosted_config(CredentialMode.READ_ONLY))
    assert resolved.token == "token-for-read-only"
    # Requested, never asserted as proven: the provider may ignore the request.
    assert resolved.capability == "requested:read-only"


def test_credential_provider_absent_read_only_falls_back_to_read_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from todo_db import CredentialMode
    from todo_db.backends import resolve_credential

    _no_injected_credentials(monkeypatch)
    provider = _provider_script(
        tmp_path,
        "ro-absent.sh",
        'test $# -eq 0 || exit 64\n'
        'if [ "$TODO_DB_CREDENTIAL_CAPABILITY" = "read-only" ]; then exit 0; fi\necho "rw-only"',
    )
    monkeypatch.setenv("TODO_DB_CREDENTIAL_COMMAND", provider)

    resolved = resolve_credential(_hosted_config(CredentialMode.READ_ONLY))
    assert (resolved.capability, resolved.token) == ("requested:read-write", "rw-only")


def test_credential_provider_error_never_escalates_to_read_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from todo_db import CredentialMode
    from todo_db.backends import resolve_credential
    from todo_db.errors import E_AUTH_MISSING, HostedAuthError

    _no_injected_credentials(monkeypatch)
    provider = _provider_script(
        tmp_path,
        "ro-broken.sh",
        'test $# -eq 0 || exit 64\n'
        'if [ "$TODO_DB_CREDENTIAL_CAPABILITY" = "read-only" ]; then\n'
        '  echo "leaked-rw-token" >&2; exit 3\nfi\necho "must-not-be-used"',
    )
    monkeypatch.setenv("TODO_DB_CREDENTIAL_COMMAND", provider)

    with pytest.raises(HostedAuthError) as raised:
        resolve_credential(_hosted_config(CredentialMode.READ_ONLY))
    assert raised.value.code == E_AUTH_MISSING
    assert "exited 3" in str(raised.value)
    assert "leaked-rw-token" not in str(raised.value)
    assert "must-not-be-used" not in str(raised.value)


def test_credential_provider_never_discloses_stdout_or_stderr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from todo_db import CredentialMode
    from todo_db.backends import resolve_credential
    from todo_db.errors import HostedAuthError

    _no_injected_credentials(monkeypatch)
    provider = _provider_script(
        tmp_path, "noisy.sh", 'echo "stdout-secret"\necho "stderr-secret" >&2\nexit 1'
    )
    monkeypatch.setenv("TODO_DB_CREDENTIAL_COMMAND", provider)

    with pytest.raises(HostedAuthError) as raised:
        resolve_credential(_hosted_config(CredentialMode.READ_WRITE))
    rendered = str(raised.value) + "".join(traceback.format_exception(raised.value))
    assert "stdout-secret" not in rendered
    assert "stderr-secret" not in rendered


def test_credential_provider_timeout_and_oversized_output_are_coded_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from todo_db import CredentialMode
    from todo_db import backends
    from todo_db.backends import resolve_credential
    from todo_db.errors import E_AUTH_MISSING, HostedAuthError

    _no_injected_credentials(monkeypatch)
    monkeypatch.setattr(backends, "CREDENTIAL_COMMAND_TIMEOUT_SECONDS", 0.2)
    slow = _provider_script(tmp_path, "slow.sh", "sleep 5")
    monkeypatch.setenv("TODO_DB_CREDENTIAL_COMMAND", slow)
    with pytest.raises(HostedAuthError) as timed_out:
        resolve_credential(_hosted_config(CredentialMode.READ_WRITE))
    assert timed_out.value.code == E_AUTH_MISSING
    assert "exceeded" in str(timed_out.value)

    backends.reset_credential_provider_cache()
    monkeypatch.setattr(backends, "CREDENTIAL_COMMAND_MAX_BYTES", 16)
    huge = _provider_script(tmp_path, "huge.sh", 'printf "%0.sA" $(seq 1 200)')
    monkeypatch.setenv("TODO_DB_CREDENTIAL_COMMAND", huge)
    with pytest.raises(HostedAuthError) as too_big:
        resolve_credential(_hosted_config(CredentialMode.READ_WRITE))
    assert "more than 16 bytes" in str(too_big.value)


def test_credential_provider_missing_or_unparsable_command_is_a_coded_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from todo_db import CredentialMode
    from todo_db.backends import resolve_credential
    from todo_db.errors import E_AUTH_MISSING, HostedAuthError

    _no_injected_credentials(monkeypatch)
    monkeypatch.setenv("TODO_DB_CREDENTIAL_COMMAND", str(tmp_path / "does-not-exist"))
    with pytest.raises(HostedAuthError) as absent:
        resolve_credential(_hosted_config(CredentialMode.READ_WRITE))
    assert absent.value.code == E_AUTH_MISSING
    assert "was not found" in str(absent.value)

    monkeypatch.setenv("TODO_DB_CREDENTIAL_COMMAND", 'op read "unterminated')
    with pytest.raises(HostedAuthError, match="not a parsable command line"):
        resolve_credential(_hosted_config(CredentialMode.READ_WRITE))


def test_injected_credentials_take_precedence_over_the_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from todo_db import CredentialMode, DatabaseConfig
    from todo_db.backends import resolve_credential

    _no_injected_credentials(monkeypatch)
    marker = tmp_path / "provider-ran"
    provider = _provider_script(tmp_path, "marker.sh", f'touch "{marker}"\necho "provider-token"')
    monkeypatch.setenv("TODO_DB_CREDENTIAL_COMMAND", provider)
    monkeypatch.setenv("TODO_DB_AUTH_TOKEN", "injected-rw")

    assert resolve_credential(_hosted_config(CredentialMode.READ_WRITE)).source == "TODO_DB_AUTH_TOKEN"
    explicit = resolve_credential(
        DatabaseConfig(
            path="libsql://provider.example.test",
            credential_mode=CredentialMode.READ_WRITE,
            auth_token="explicit",
        )
    )
    assert explicit.source == "DatabaseConfig.auth_token"
    assert not marker.exists()


def test_unset_provider_changes_no_resolution_outcome_and_adds_no_provider_noise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codes, capabilities, and precedence are what they were before the provider existed.

    The remediation wording deliberately changed to name the provisioning
    procedure; what must not change is which credential is selected, which code
    is raised, and that an operator who never adopts a provider is not told
    about a failed one.
    """

    from todo_db import CredentialMode
    from todo_db.backends import resolve_credential
    from todo_db.errors import E_AUTH_MISSING, HostedAuthError

    _no_injected_credentials(monkeypatch)
    for mode in (CredentialMode.READ_ONLY, CredentialMode.READ_WRITE):
        with pytest.raises(HostedAuthError) as raised:
            resolve_credential(_hosted_config(mode))
        assert raised.value.code == E_AUTH_MISSING
        assert "returned no credential" not in str(raised.value)
        assert "provider" not in str(raised.value).lower()

    monkeypatch.setenv("TODO_DB_AUTH_TOKEN", "rw")
    monkeypatch.setenv("TODO_DB_RO_AUTH_TOKEN", "ro")
    assert resolve_credential(_hosted_config(CredentialMode.READ_ONLY)).source == "TODO_DB_RO_AUTH_TOKEN"
    assert resolve_credential(_hosted_config(CredentialMode.READ_WRITE)).source == "TODO_DB_AUTH_TOKEN"


def test_missing_credential_messages_name_the_provisioning_procedure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from todo_db import CredentialMode
    from todo_db.backends import auth_remediation, resolve_credential
    from todo_db.errors import HostedAuthError

    _no_injected_credentials(monkeypatch)
    for mode in (CredentialMode.READ_ONLY, CredentialMode.READ_WRITE):
        with pytest.raises(HostedAuthError) as raised:
            resolve_credential(_hosted_config(mode))
        message = str(raised.value)
        assert "TODO_DB_CREDENTIAL_COMMAND" in message
        assert "docs/operations/hosted-credentials.md, Provision once" in message
        assert "inject a bounded" not in message

    assert "Rotate: routine replacement" in auth_remediation()


def test_provider_is_consulted_at_most_once_per_capability_per_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from todo_db import CredentialMode
    from todo_db.backends import resolve_credential

    _no_injected_credentials(monkeypatch)
    counter = tmp_path / "calls"
    provider = _provider_script(tmp_path, "counting.sh", f'echo x >> "{counter}"\necho "cached-token"')
    monkeypatch.setenv("TODO_DB_CREDENTIAL_COMMAND", provider)

    for _ in range(4):
        assert resolve_credential(_hosted_config(CredentialMode.READ_WRITE)).token == "cached-token"
    assert counter.read_text().count("x") == 1


def test_local_backend_never_consults_the_provider(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from todo_db import DatabaseConfig, ProjectIdentity, TodoDatabase

    _no_injected_credentials(monkeypatch)
    marker = tmp_path / "provider-ran-locally"
    provider = _provider_script(tmp_path, "local.sh", f'touch "{marker}"\necho "unused"')
    monkeypatch.setenv("TODO_DB_CREDENTIAL_COMMAND", provider)

    identity = ProjectIdentity(project_id="provider-local", repository="https://example.test/local")
    database = TodoDatabase.open(DatabaseConfig(path=tmp_path / "local.sqlite", identity=identity))
    database.close()
    assert not marker.exists()


def test_provider_argv_is_passed_through_untouched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression: an appended positional broke every documented provider.

    `security find-generic-password -w -s <service>` reads a trailing word as
    the keychain to search and exits 44; `op read` and `pass show` reject the
    extra argument. The stub here is argument-strict on purpose, the way a real
    tool is, because a permissive stub is exactly why this was missed.
    """

    from todo_db import CredentialMode
    from todo_db.backends import resolve_credential

    _no_injected_credentials(monkeypatch)
    argv_dump = tmp_path / "argv"
    provider = _provider_script(
        tmp_path,
        "strict.sh",
        f'printf "%s\\n" "$#" "$@" > "{argv_dump}"\\n'
        # Strict like a real tool: exactly the operator's own two arguments.
        'if [ "$#" -ne 2 ]; then echo "unexpected argument" >&2; exit 44; fi\\n'
        'echo "strict-token"',
    )
    monkeypatch.setenv("TODO_DB_CREDENTIAL_COMMAND", f"{provider} --flag value")

    resolved = resolve_credential(_hosted_config(CredentialMode.READ_WRITE))
    assert resolved.token == "strict-token"
    # The operator's own arguments survive; nothing is appended after them.
    assert argv_dump.read_text().split("\n")[:3] == ["2", "--flag", "value"]


def test_provider_returning_invalid_utf8_is_a_coded_error_not_a_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression: text=True decoded strictly inside communicate().

    UnicodeDecodeError is a ValueError, so it slipped past every except clause,
    crashed the process, and bypassed the E_AUTH_MISSING degradation ADR 0005
    G4 requires. Bytes are captured now, so a malformed payload degrades.
    """

    from todo_db import CredentialMode
    from todo_db import backends
    from todo_db.backends import resolve_credential
    from todo_db.errors import HostedAuthError

    _no_injected_credentials(monkeypatch)
    provider = _provider_script(tmp_path, "binary.sh", r'printf "\xff\xfe\x00binary"')
    monkeypatch.setenv("TODO_DB_CREDENTIAL_COMMAND", provider)

    # Undecodable output is not an error by itself: it is simply not a usable
    # credential once stripped, and the caller gets the ordinary missing-credential
    # path rather than a traceback.
    try:
        resolved = resolve_credential(_hosted_config(CredentialMode.READ_WRITE))
    except HostedAuthError as exc:
        assert exc.code == "E_AUTH_MISSING"
    else:
        assert "�" in resolved.token or resolved.token

    # Oversized binary output is still rejected on the raw bytes, before any decode.
    backends.reset_credential_provider_cache()
    monkeypatch.setattr(backends, "CREDENTIAL_COMMAND_MAX_BYTES", 8)
    big = _provider_script(tmp_path, "bigbinary.sh", r'printf "\xff\xfe\x00binarybinarybinary"')
    monkeypatch.setenv("TODO_DB_CREDENTIAL_COMMAND", big)
    with pytest.raises(HostedAuthError, match="more than 8 bytes"):
        resolve_credential(_hosted_config(CredentialMode.READ_WRITE))


def test_single_entry_provider_never_claims_a_capability_it_cannot_prove(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A store with one entry serves both capabilities from one token.

    The resolver asks for read-only, the provider returns the read-write token
    because that is all it has, and nothing in the output may imply the token is
    read-only. ADR 0004 already says read-only is server-enforced only when the
    token was minted read-only; this keeps the client label honest too.
    """

    from todo_db import CredentialMode
    from todo_db.backends import resolve_credential

    _no_injected_credentials(monkeypatch)
    provider = _provider_script(tmp_path, "single.sh", 'echo "the-one-and-only-rw-token"')
    monkeypatch.setenv("TODO_DB_CREDENTIAL_COMMAND", provider)

    read_only = resolve_credential(_hosted_config(CredentialMode.READ_ONLY))
    assert read_only.token == "the-one-and-only-rw-token"
    assert read_only.capability == "requested:read-only"
    assert read_only.capability != "read-only"


def test_provider_absent_branch_must_exit_zero_to_permit_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The sharp edge a branching provider script has to get right.

    Real tools exit non-zero when an entry is missing, and a non-zero exit is an
    error that stops resolution. Only exit 0 with empty output means absent, so
    a script that wants read-only to fall back must exit 0 explicitly.
    """

    from todo_db import CredentialMode
    from todo_db import backends
    from todo_db.backends import resolve_credential
    from todo_db.errors import E_AUTH_MISSING, HostedAuthError

    _no_injected_credentials(monkeypatch)
    wrong = _provider_script(
        tmp_path,
        "missing-entry.sh",
        'if [ "$TODO_DB_CREDENTIAL_CAPABILITY" = "read-only" ]; then exit 44; fi\n'
        'echo "rw-token"',
    )
    monkeypatch.setenv("TODO_DB_CREDENTIAL_COMMAND", wrong)
    with pytest.raises(HostedAuthError) as raised:
        resolve_credential(_hosted_config(CredentialMode.READ_ONLY))
    assert raised.value.code == E_AUTH_MISSING
    assert "exited 44" in str(raised.value)

    backends.reset_credential_provider_cache()
    right = _provider_script(
        tmp_path,
        "absent-branch.sh",
        'if [ "$TODO_DB_CREDENTIAL_CAPABILITY" = "read-only" ]; then exit 0; fi\n'
        'echo "rw-token"',
    )
    monkeypatch.setenv("TODO_DB_CREDENTIAL_COMMAND", right)
    resolved = resolve_credential(_hosted_config(CredentialMode.READ_ONLY))
    assert (resolved.capability, resolved.token) == ("requested:read-write", "rw-token")
