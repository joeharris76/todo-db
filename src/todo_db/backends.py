"""Local SQLite and optional Turso/libSQL connection adapters."""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Any

from .errors import HostedAuthError, TodoDBError
from .models import CredentialMode, DatabaseConfig


def connect(config: DatabaseConfig) -> sqlite3.Connection | "HostedConnection":
    if config.is_hosted:
        return _connect_hosted(config)
    return _connect_sqlite(config)


def _connect_sqlite(config: DatabaseConfig) -> sqlite3.Connection:
    path = Path(config.path)
    if config.credential_mode is CredentialMode.READ_ONLY:
        connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True, timeout=config.timeout)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=config.timeout)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


class HostedRow:
    __slots__ = ("_names", "_values")

    def __init__(self, names: list[str], values: tuple[Any, ...]):
        self._names = [name.lower() for name in names]
        self._values = values

    def __getitem__(self, key: int | str) -> Any:
        if isinstance(key, str):
            try:
                return self._values[self._names.index(key.lower())]
            except ValueError:
                raise IndexError(f"no such column: {key}") from None
        return self._values[key]

    def keys(self) -> list[str]:
        return list(self._names)

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


class HostedCursor:
    def __init__(self, cursor: Any):
        self._cursor = cursor

    def _row(self, values: Any) -> HostedRow | None:
        if values is None:
            return None
        names = [column[0] for column in (self._cursor.description or [])]
        return HostedRow(names, tuple(values))

    def fetchone(self) -> HostedRow | None:
        return self._row(self._cursor.fetchone())

    def fetchall(self) -> list[HostedRow]:
        return [row for value in self._cursor.fetchall() if (row := self._row(value)) is not None]

    def __iter__(self):
        while True:
            row = self.fetchone()
            if row is None:
                return
            yield row

    @property
    def lastrowid(self):
        return self._cursor.lastrowid

    @property
    def rowcount(self):
        return self._cursor.rowcount


class HostedConnection:
    """Small sqlite3-compatible surface over the libsql Python client."""

    def __init__(self, raw: Any):
        self._raw = raw

    def execute(self, sql: str, params=()) -> HostedCursor:
        try:
            return HostedCursor(self._raw.execute(sql, tuple(params)))
        except ValueError as exc:
            message = str(exc)
            if "constraint" in message.lower():
                raise sqlite3.IntegrityError(message) from exc
            raise sqlite3.OperationalError(message) from exc

    def commit(self) -> None:
        self._raw.commit()

    def rollback(self) -> None:
        self._raw.rollback()

    def close(self) -> None:
        self._raw.close()

    def sync(self) -> None:
        self._raw.sync()


def _normalize_url(value: str) -> str:
    value = value.strip()
    scheme, separator, rest = value.partition("://")
    return scheme.lower() + separator + rest if separator else value


def _secure_url(value: str) -> str:
    value = _normalize_url(value)
    if value.startswith("http://"):
        raise TodoDBError("refusing plaintext http:// for the hosted backend; use https:// or libsql://")
    return value


def _token(config: DatabaseConfig) -> str:
    if config.auth_token:
        return config.auth_token
    variable = "TODO_DB_RO_AUTH_TOKEN" if config.credential_mode is CredentialMode.READ_ONLY else "TODO_DB_AUTH_TOKEN"
    token = os.environ.get(variable, "")
    if not token:
        raise TodoDBError(f"hosted backend requires {variable}")
    return token


def _redacted_error(exc: BaseException, *, url: str, token: str) -> str:
    message = str(exc).replace(url, "[REDACTED]")
    return message.replace(token, "[REDACTED]") if token else message


_AUTH_MARKERS = re.compile(r"\b(?:401|403|unauthorized|forbidden|auth\w*|token\w*|jwt)\b", re.IGNORECASE)


def is_auth_shaped(detail: str) -> bool:
    """Conservative auth classification over an already-redacted error message."""

    return _AUTH_MARKERS.search(detail) is not None


def auth_remediation(token_variable: str = "TODO_DB_AUTH_TOKEN") -> str:
    return (
        f"token invalid or expired: refresh {token_variable}, "
        "e.g. export TODO_DB_AUTH_TOKEN=$(turso db tokens create <db>); "
        "if the turso CLI itself is logged out, run 'turso auth login'"
    )


def hosted_error(
    exc: BaseException, *, url: str, token: str, context: str, token_variable: str = "TODO_DB_AUTH_TOKEN"
) -> TodoDBError:
    """Redact and classify a hosted failure: HostedAuthError when auth-shaped, TodoDBError otherwise."""

    detail = _redacted_error(exc, url=url, token=token)
    if is_auth_shaped(detail):
        return HostedAuthError(f"hosted backend {context} failed: {detail}; {auth_remediation(token_variable)}")
    return TodoDBError(f"hosted backend {context} failed: {detail}")


def _connect_hosted(config: DatabaseConfig) -> HostedConnection:
    url = _secure_url(str(config.path))
    token = _token(config)
    try:
        import libsql
    except ImportError as exc:
        raise TodoDBError("hosted backend requires the `todo-db[hosted]` extra") from exc

    variable = "TODO_DB_RO_AUTH_TOKEN" if config.credential_mode is CredentialMode.READ_ONLY else "TODO_DB_AUTH_TOKEN"
    try:
        raw = libsql.connect(url, auth_token=token)
    except Exception as exc:
        raise hosted_error(exc, url=url, token=token, context="connection", token_variable=variable) from None
    connection = HostedConnection(raw)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection
