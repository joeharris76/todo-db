"""Importable Fake Hrana / LibSQL transport for in-process and sub-process tests."""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import sys
import types
from typing import Any


class FakeHranaCursor:
    def __init__(self, cursor: sqlite3.Cursor, parent_conn: FakeHranaConnection | None = None):
        self._cursor = cursor
        self._parent = parent_conn

    def fetchone(self):
        row = self._cursor.fetchone()
        if row is not None and self._parent is not None:
            self._parent.bytes_received += sum(len(str(v).encode("utf-8")) for v in row)
        return row

    def fetchall(self):
        rows = self._cursor.fetchall()
        if rows and self._parent is not None:
            for row in rows:
                self._parent.bytes_received += sum(len(str(v).encode("utf-8")) for v in row)
        return rows

    def __iter__(self):
        for row in self.fetchall():
            yield row

    @property
    def description(self):
        return self._cursor.description

    @property
    def lastrowid(self):
        return self._cursor.lastrowid

    @property
    def rowcount(self):
        return self._cursor.rowcount


class FakeHranaConnection:
    def __init__(self, path: Path, sync_url: str | None = None, auth_token: str | None = None):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._sync_url = sync_url
        self._auth_token = auth_token
        self._connection = sqlite3.connect(path, timeout=30.0)
        self._connection.isolation_level = None
        self.sync_calls = 0
        self.bytes_sent = 0
        self.bytes_received = 0

    def execute(self, sql: str, params: Any = ()):
        self.bytes_sent += len(sql.encode("utf-8")) + sum(len(str(p).encode("utf-8")) for p in params)
        try:
            return FakeHranaCursor(self._connection.execute(sql, tuple(params)), self)
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


# See tests/test_hosted_backend.py: a double that accepts any keyword tests that
# the code called something, not that it called the right thing.
_HRANA_CONNECT_KEYWORDS = frozenset({"auth_token", "isolation_level", "sync_url", "sync_interval"})


class FakeHranaModule(types.ModuleType):
    def __init__(self, primary_path: Path):
        super().__init__("libsql")
        self.primary_path = primary_path
        self.connect_calls: list[dict[str, Any]] = []
        self.connections: list[FakeHranaConnection] = []

    def connect(self, database: Any, **kwargs: Any):
        unexpected = sorted(set(kwargs) - _HRANA_CONNECT_KEYWORDS)
        if unexpected:
            raise TypeError(f"libsql.connect() got unexpected keyword argument(s): {', '.join(unexpected)}")
        target = self.primary_path
        self.connect_calls.append({"database": str(database), **kwargs})
        conn = FakeHranaConnection(target, sync_url=kwargs.get("sync_url"), auth_token=kwargs.get("auth_token"))
        self.connections.append(conn)
        return conn

    def reset(self) -> None:
        self.connect_calls.clear()
        self.connections.clear()


def install_fake_hrana(primary_path: Path | str) -> FakeHranaModule:
    mod = FakeHranaModule(Path(primary_path))
    sys.modules["libsql"] = mod
    return mod


if os.environ.get("TODO_DB_FAKE_HRANA_DB"):
    install_fake_hrana(os.environ["TODO_DB_FAKE_HRANA_DB"])
