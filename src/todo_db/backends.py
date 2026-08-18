"""Local SQLite and optional Turso/libSQL connection adapters."""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import E_AUTH_MISSING, E_AUTH_REJECTED, HostedAuthError, TodoDBError
from .models import CredentialMode, DatabaseConfig


def connect(
    config: DatabaseConfig, *, credential: "ResolvedCredential | None" = None
) -> sqlite3.Connection | "HostedConnection":
    if config.is_hosted:
        return _connect_hosted(config, credential=credential)
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


@dataclass(frozen=True, slots=True)
class ResolvedCredential:
    token: str = field(repr=False)
    source: str
    capability: str


class HostedConnection:
    """Small sqlite3-compatible surface over the libsql Python client."""

    def __init__(
        self,
        raw: Any,
        *,
        url: str = "",
        credential: ResolvedCredential | None = None,
        token: str = "",
        token_variable: str = "TODO_DB_AUTH_TOKEN",
    ):
        self._raw = raw
        self._url = url
        self._credential = credential or ResolvedCredential(token, token_variable, "unknown")
        self._token = self._credential.token

    def _wrap_error(self, exc: BaseException, context: str) -> Exception:
        if isinstance(exc, TodoDBError):
            return exc
        message = str(exc)
        if isinstance(exc, sqlite3.IntegrityError) or "constraint" in message.lower():
            if self._url:
                message = _redacted_error(exc, url=self._url, token=self._token)
            return sqlite3.IntegrityError(message)
        if self._url:
            return hosted_error(exc, url=self._url, credential=self._credential, context=context)
        if isinstance(exc, sqlite3.OperationalError):
            return exc
        return sqlite3.OperationalError(message)

    def execute(self, sql: str, params=()) -> HostedCursor:
        try:
            return HostedCursor(self._raw.execute(sql, tuple(params)))
        except ValueError as exc:
            raise self._wrap_error(exc, "execute") from None
        except Exception as exc:
            raise self._wrap_error(exc, "execute") from None

    def commit(self) -> None:
        try:
            self._raw.commit()
        except Exception as exc:
            raise self._wrap_error(exc, "commit") from None

    def rollback(self) -> None:
        try:
            self._raw.rollback()
        except Exception as exc:
            raise self._wrap_error(exc, "rollback") from None

    def close(self) -> None:
        try:
            self._raw.close()
        except Exception as exc:
            raise self._wrap_error(exc, "close") from None

    def sync(self) -> None:
        try:
            self._raw.sync()
        except Exception as exc:
            raise self._wrap_error(exc, "sync") from None


def _normalize_url(value: str) -> str:
    value = value.strip()
    scheme, separator, rest = value.partition("://")
    return scheme.lower() + separator + rest if separator else value


def _secure_url(value: str) -> str:
    value = _normalize_url(value)
    if value.startswith("http://"):
        raise TodoDBError("refusing plaintext http:// for the hosted backend; use https:// or libsql://")
    return value


def resolve_credential(config: DatabaseConfig) -> ResolvedCredential:
    """Resolve one hosted credential and retain only non-secret provenance metadata."""

    if config.auth_token:
        return ResolvedCredential(config.auth_token, "DatabaseConfig.auth_token", "unknown")
    if config.credential_mode is CredentialMode.READ_WRITE:
        token = os.environ.get("TODO_DB_AUTH_TOKEN", "")
        if token:
            return ResolvedCredential(token, "TODO_DB_AUTH_TOKEN", "read-write")
        raise HostedAuthError(
            "hosted backend requires TODO_DB_AUTH_TOKEN; inject a bounded read-write credential",
            code=E_AUTH_MISSING,
        )

    read_only = os.environ.get("TODO_DB_RO_AUTH_TOKEN", "")
    if read_only:
        return ResolvedCredential(read_only, "TODO_DB_RO_AUTH_TOKEN", "read-only")
    read_write = os.environ.get("TODO_DB_AUTH_TOKEN", "")
    if read_write:
        return ResolvedCredential(read_write, "TODO_DB_AUTH_TOKEN", "read-write")
    raise HostedAuthError(
        "hosted read access requires TODO_DB_RO_AUTH_TOKEN or TODO_DB_AUTH_TOKEN; "
        "inject a bounded credential",
        code=E_AUTH_MISSING,
    )


def _redacted_error(exc: BaseException, *, url: str, token: str) -> str:
    message = str(exc).replace(url, "[REDACTED]")
    return message.replace(token, "[REDACTED]") if token else message


_AUTH_MARKERS = re.compile(
    r"(?:\b401\b|\bunauthorized\b|\bforbidden\b|\binvalid[ _-]?token\b|"
    r"\bjwt(?:\s+error)?\b.*\b(?:expired|invalid)\b|\b(?:expired|invalid)\b.*\bjwt\b)",
    re.IGNORECASE,
)


def is_auth_shaped(detail: str) -> bool:
    """Best-effort auth classification requiring high-confidence upstream evidence."""

    return _AUTH_MARKERS.search(detail) is not None


def auth_remediation(credential: ResolvedCredential | None = None) -> str:
    source = credential.source if credential is not None else "the configured credential source"
    return f"credential rejected: replace the bounded credential from {source} and retry in a fresh process"


def hosted_error(
    exc: BaseException, *, url: str, credential: ResolvedCredential, context: str
) -> TodoDBError:
    """Redact and classify a hosted failure: HostedAuthError when auth-shaped, TodoDBError otherwise."""

    detail = _redacted_error(exc, url=url, token=credential.token)
    if is_auth_shaped(detail):
        return HostedAuthError(
            f"hosted backend {context} failed: {detail}; {auth_remediation(credential)}",
            code=E_AUTH_REJECTED,
        )
    return TodoDBError(f"hosted backend {context} failed: {detail}")


def _connect_hosted(
    config: DatabaseConfig, *, credential: ResolvedCredential | None = None
) -> HostedConnection:
    url = _secure_url(str(config.path))
    credential = credential or resolve_credential(config)
    token = credential.token
    try:
        import libsql
    except ImportError as exc:
        raise TodoDBError("hosted backend requires the `todo-db[hosted]` extra") from exc

    try:
        raw = libsql.connect(url, auth_token=token, isolation_level=None)
    except Exception as exc:
        raise hosted_error(exc, url=url, credential=credential, context="connection") from None
    connection = HostedConnection(raw, url=url, credential=credential)
    if config.credential_mode is not CredentialMode.READ_ONLY:
        try:
            connection.execute("PRAGMA foreign_keys = ON")
        except Exception:
            pass
    return connection
