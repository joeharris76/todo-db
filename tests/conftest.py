from __future__ import annotations

import contextlib
import dataclasses
import sqlite3
from pathlib import Path
from typing import Any, Generator, Iterator

import pytest

import todo_db.backends as backends
from todo_db import DatabaseConfig, ProjectIdentity, TodoDatabase
from todo_db.backends import HostedConnection


@dataclasses.dataclass
class SQLStatement:
    sql: str
    params: tuple[Any, ...] = ()
    phase: str = "operation"
    is_write: bool = False


class SQLTrace:
    """Records SQL statements, execution phases, output bytes, and tool invocations."""

    def __init__(self) -> None:
        self.statements: list[SQLStatement] = []
        self.current_phase: str = "operation"
        self.stdout_bytes: int = 0
        self.stderr_bytes: int = 0
        self.estimated_tokens: int = 0
        self.tool_invocations: list[str] = []

    def reset(self) -> None:
        self.statements.clear()
        self.current_phase = "operation"
        self.stdout_bytes = 0
        self.stderr_bytes = 0
        self.estimated_tokens = 0
        self.tool_invocations.clear()

    @contextlib.contextmanager
    def phase(self, name: str) -> Iterator[None]:
        previous = self.current_phase
        self.current_phase = name
        try:
            yield
        finally:
            self.current_phase = previous

    def record_statement(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        stripped = sql.strip().upper()
        is_write = any(
            stripped.startswith(prefix)
            for prefix in ("INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER", "BEGIN", "COMMIT")
        )
        self.statements.append(
            SQLStatement(
                sql=sql,
                params=params,
                phase=self.current_phase,
                is_write=is_write,
            )
        )

    def record_output(self, stdout: str = "", stderr: str = "") -> None:
        out_b = len(stdout.encode("utf-8"))
        err_b = len(stderr.encode("utf-8"))
        self.stdout_bytes += out_b
        self.stderr_bytes += err_b
        # Standard token heuristic: roughly 4 chars per token
        self.estimated_tokens += (len(stdout) + len(stderr) + 3) // 4

    def record_tool_invocation(self, name: str) -> None:
        self.tool_invocations.append(name)

    @property
    def total_statements(self) -> int:
        return len(self.statements)

    @property
    def open_statements(self) -> int:
        return sum(1 for s in self.statements if s.phase == "open")

    @property
    def op_statements(self) -> int:
        return sum(1 for s in self.statements if s.phase == "operation")

    @property
    def render_statements(self) -> int:
        return sum(1 for s in self.statements if s.phase == "render")

    def count_phase(self, phase_name: str) -> int:
        return sum(1 for s in self.statements if s.phase == phase_name)

    def matching(self, substring: str) -> list[SQLStatement]:
        sub = substring.lower()
        return [s for s in self.statements if sub in s.sql.lower()]


class TracedSQLiteConnection:
    """Transparent wrapper around sqlite3.Connection that records statements."""

    def __init__(self, raw: sqlite3.Connection, trace: SQLTrace) -> None:
        self._raw = raw
        self._trace = trace

    def __getattr__(self, name: str) -> Any:
        return getattr(self._raw, name)

    def execute(self, sql: str, params: Any = ()) -> sqlite3.Cursor:
        self._trace.record_statement(sql, tuple(params) if isinstance(params, (list, tuple)) else (params,))
        return self._raw.execute(sql, params)

    def executemany(self, sql: str, seq_of_params: Any) -> sqlite3.Cursor:
        self._trace.record_statement(sql, ())
        return self._raw.executemany(sql, seq_of_params)

    def executescript(self, sql_script: str) -> sqlite3.Cursor:
        self._trace.record_statement(sql_script, ())
        return self._raw.executescript(sql_script)

    def cursor(self) -> sqlite3.Cursor:
        # Wrap cursor to catch cursor.execute calls
        cursor = self._raw.cursor()
        orig_execute = cursor.execute

        def wrapped_execute(sql: str, params: Any = ()):
            self._trace.record_statement(sql, tuple(params) if isinstance(params, (list, tuple)) else (params,))
            return orig_execute(sql, params)

        cursor.execute = wrapped_execute  # type: ignore[method-assign]
        return cursor


@pytest.fixture
def sql_trace(monkeypatch: pytest.MonkeyPatch) -> Generator[SQLTrace, None, None]:
    trace = SQLTrace()

    orig_connect_sqlite = backends._connect_sqlite
    orig_hosted_execute = HostedConnection.execute

    def traced_connect_sqlite(config: DatabaseConfig) -> sqlite3.Connection:
        conn = orig_connect_sqlite(config)
        return TracedSQLiteConnection(conn, trace)  # type: ignore[return-value]

    def traced_hosted_execute(self: HostedConnection, sql: str, params: Any = ()) -> Any:
        trace.record_statement(sql, tuple(params) if isinstance(params, (list, tuple)) else (params,))
        return orig_hosted_execute(self, sql, params)

    monkeypatch.setattr(backends, "_connect_sqlite", traced_connect_sqlite)
    monkeypatch.setattr(HostedConnection, "execute", traced_hosted_execute)

    yield trace


@pytest.fixture
def seeded_db(tmp_path: Path) -> TodoDatabase:
    """A database seeded with >=20 items, dependencies, and >=3 work units each."""
    from todo_db.tracker import TodoTracker

    db_path = tmp_path / "seeded_todo.sqlite"
    config = DatabaseConfig(
        path=db_path,
        identity=ProjectIdentity(project_id="test-project", repository="https://github.com/example/repo"),
    )
    db = TodoDatabase.open(config)
    tracker = TodoTracker(db, actor="test-actor")

    # Seed 22 items with dependencies and work units
    for i in range(22):
        item_id = f"item-{i:02d}"
        deps = [f"item-{i - 1:02d}"] if i > 0 and i % 3 == 0 else []
        tracker.create_item(
            item_id=item_id,
            title=f"Test Item {i}",
            worktree="todo-db",
            category="feature" if i % 2 == 0 else "bugfix",
            priority="high" if i < 5 else "medium",
            description=f"Detailed description for test item {i} to verify budget behaviors.",
            deps=deps,
            approach=f"Approach for item {i}",
            preserves=[f"Preserve invariant {i}.A", f"Preserve invariant {i}.B"],
            anti_patterns=[{"dont": f"Don't do anti-{i}", "instead": f"Do pattern-{i}", "why": f"Why-{i}"}],
            work=[
                {"id": "w0", "summary": f"Work unit 0 for item {i}"},
                {"id": "w1", "summary": f"Work unit 1 for item {i}", "needs": ["w0"]},
                {"id": "w2", "summary": f"Work unit 2 for item {i}", "needs": ["w1"]},
            ],
            verifications=[
                {"seq": 1, "description": f"Verification 1 for {i}", "command": f"echo verify-{i}"},
            ],
        )

    return db
