"""Hosted direct-primary latency, byte tracking, and concurrency tests."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

import pytest

try:
    from .fake_hrana import install_fake_hrana
except ImportError:
    from fake_hrana import install_fake_hrana  # type: ignore[no-redef]
from todo_db import CredentialMode, DatabaseConfig, ProjectIdentity, TodoDatabase, TodoTracker


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _init_primary(path: Path) -> None:
    identity = ProjectIdentity(project_id="latency-test", repository="https://example.test/hosted")
    config = DatabaseConfig(path=path, identity=identity)
    db = TodoDatabase.open(config)
    tracker = TodoTracker(db, actor="setup")
    tracker.create_item(
        item_id="item-01",
        title="Latency test item 1",
        worktree="todo-db",
        priority="high",
        description="First benchmark item",
        work=[{"id": "w0", "summary": "step 0"}],
        verifications=[{"description": "v1", "command": "true"}],
    )
    tracker.create_item(
        item_id="item-02",
        title="Latency test item 2",
        worktree="todo-db",
        priority="medium",
        description="Second benchmark item",
        work=[{"id": "w0", "summary": "step 0"}],
        verifications=[{"description": "v1", "command": "true"}],
    )
    db.close()


def test_two_process_hosted_claim_race(tmp_path: Path) -> None:
    """w1: Two subprocesses contending for a claim against a hosted primary via fake Hrana."""
    primary = tmp_path / "primary.sqlite"
    _init_primary(primary)

    # Subprocess command using python -c with fake_hrana loaded. The `claim` CLI
    # verb was removed in 0.6.0 (MCP is the agent surface); the cross-process
    # BEGIN IMMEDIATE contention invariant is exercised via the tracker API,
    # which is exactly what both the CLI floor and the MCP server call.
    script = """
import sys
from fake_hrana import install_fake_hrana
install_fake_hrana(sys.argv[1])
from todo_db import CredentialMode, DatabaseConfig, ProjectIdentity, TodoDatabase, TodoTracker
from todo_db.errors import TodoError
config = DatabaseConfig(
    path="libsql://test.turso.io",
    identity=ProjectIdentity(project_id="latency-test", repository="https://example.test/hosted"),
    auth_token="test-token",
    credential_mode=CredentialMode.READ_WRITE,
)
db = TodoDatabase.open(config)
try:
    TodoTracker(db, actor=sys.argv[2]).claim("item-01")
except TodoError as exc:
    print(exc, file=sys.stderr)
    sys.exit(2)
finally:
    db.close()
sys.exit(0)
"""

    env = dict(os.environ)
    env["PYTHONPATH"] = f"{PROJECT_ROOT}/tests:{PROJECT_ROOT}/src:{PROJECT_ROOT}"
    env["TODO_DB_AUTH_TOKEN"] = "test-token"

    p_a = subprocess.Popen(
        [sys.executable, "-c", script, str(primary), "actor-a"],
        cwd=PROJECT_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    p_b = subprocess.Popen(
        [sys.executable, "-c", script, str(primary), "actor-b"],
        cwd=PROJECT_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    out_a, err_a = p_a.communicate(timeout=15)
    out_b, err_b = p_b.communicate(timeout=15)

    codes = [p_a.returncode, p_b.returncode]
    assert 0 in codes
    assert 2 in codes
    assert codes.count(0) == 1
    assert codes.count(2) == 1


def test_latency_harness_direct_read_only_and_read_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """w2, w5, w6: Measure p50/p95 latency and byte counts for direct hosted modes."""
    primary = tmp_path / "primary.sqlite"
    _init_primary(primary)

    fake = install_fake_hrana(primary)
    monkeypatch.setitem(sys.modules, "libsql", fake)

    identity = ProjectIdentity(project_id="latency-test", repository="https://example.test/hosted")

    # Arm 1: Direct Read-Only Connection
    config_direct_ro = DatabaseConfig(
        path="libsql://project.aws-us-east-1.turso.io",
        identity=identity,
        auth_token="ro-token",
        credential_mode=CredentialMode.READ_ONLY,
    )

    # Arm 2: Direct Read-Write Connection
    config_direct_rw = DatabaseConfig(
        path="libsql://project.aws-us-east-1.turso.io",
        identity=identity,
        auth_token="rw-token",
        credential_mode=CredentialMode.READ_WRITE,
    )

    arms = {
        "direct_ro": config_direct_ro,
        "direct_rw": config_direct_rw,
    }

    metrics: dict[str, dict[str, Any]] = {}

    for arm_name, cfg in arms.items():
        fake.reset()
        durations: list[float] = []
        bytes_sent_total = 0
        bytes_recv_total = 0

        for _ in range(5):
            t0 = time.perf_counter()
            db = TodoDatabase.open(cfg)
            tracker = TodoTracker(db, actor=f"bench-{arm_name}")
            ready = tracker.ready_items()
            assert len(ready) >= 1
            t1 = time.perf_counter()
            durations.append(t1 - t0)
            db.close()

        durations.sort()
        p50 = durations[len(durations) // 2]
        p95 = durations[-1]

        # Aggregate bytes from fake hrana connections for this arm
        for conn in fake.connections:
            bytes_sent_total += conn.bytes_sent
            bytes_recv_total += conn.bytes_received

        metrics[arm_name] = {
            "p50_seconds": p50,
            "p95_seconds": p95,
            "bytes_sent": bytes_sent_total,
            "bytes_received": bytes_recv_total,
        }

    assert "direct_ro" in metrics
    assert "direct_rw" in metrics
    for arm_name in ("direct_ro", "direct_rw"):
        assert metrics[arm_name]["p50_seconds"] > 0
        assert metrics[arm_name]["bytes_sent"] > 0
        assert metrics[arm_name]["bytes_received"] > 0


def test_read_write_open_overhead_in_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """w3: Measure read-write open overhead in isolation."""
    primary = tmp_path / "primary.sqlite"
    _init_primary(primary)

    fake = install_fake_hrana(primary)
    monkeypatch.setitem(sys.modules, "libsql", fake)

    identity = ProjectIdentity(project_id="latency-test", repository="https://example.test/hosted")
    config = DatabaseConfig(
        path="libsql://project.aws-us-east-1.turso.io",
        identity=identity,
        auth_token="rw-token",
    )

    open_times: list[float] = []
    for _ in range(10):
        t0 = time.perf_counter()
        db = TodoDatabase.open(config)
        t1 = time.perf_counter()
        open_times.append(t1 - t0)
        db.close()

    open_times.sort()
    p50_open = open_times[len(open_times) // 2]
    # Fast in-memory / local open overhead
    assert p50_open < 0.5


def test_direct_primary_write_latency(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Measure direct-primary write latency separately from connection-open latency."""
    primary = tmp_path / "primary.sqlite"
    _init_primary(primary)

    fake = install_fake_hrana(primary)
    monkeypatch.setitem(sys.modules, "libsql", fake)
    identity = ProjectIdentity(project_id="latency-test", repository="https://example.test/hosted")
    config = DatabaseConfig(
        path="libsql://project.aws-us-east-1.turso.io",
        identity=identity,
        auth_token="rw-token",
        credential_mode=CredentialMode.READ_WRITE,
    )

    durations: list[float] = []
    for index in range(5):
        database = TodoDatabase.open(config)
        started = time.perf_counter()
        database.record_event(actor="latency-test", action="write-probe", detail={"index": index})
        durations.append(time.perf_counter() - started)
        database.close()

    durations.sort()
    assert durations[len(durations) // 2] > 0
    assert sum(connection.bytes_sent for connection in fake.connections) > 0
    assert sum(connection.bytes_received for connection in fake.connections) > 0


def test_direct_primary_commit_outcome_requires_state_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A post-commit transport error must be reconciled before a retry."""
    from todo_db.errors import TodoDBError

    primary = tmp_path / "primary.sqlite"
    _init_primary(primary)
    fake = install_fake_hrana(primary)
    monkeypatch.setitem(sys.modules, "libsql", fake)
    identity = ProjectIdentity(project_id="latency-test", repository="https://example.test/hosted")
    config = DatabaseConfig(
        path="libsql://project.aws-us-east-1.turso.io",
        identity=identity,
        auth_token="rw-token",
        credential_mode=CredentialMode.READ_WRITE,
    )

    database = TodoDatabase.open(config)
    raw = database.connection._raw
    commit = raw.commit

    def commit_then_fail() -> None:
        commit()
        raise ValueError("stream closed after commit")

    raw.commit = commit_then_fail
    with pytest.raises(TodoDBError, match="hosted backend commit failed"):
        database.record_event(actor="outcome-test", action="ambiguous-write", detail={"value": 1})
    database.close()

    reconciled = TodoDatabase.open(config)
    events = [
        dict(row)
        for row in reconciled.connection.execute(
            "SELECT action, detail FROM events WHERE action = ?", ("ambiguous-write",)
        )
    ]
    assert len(events) == 1
    assert events[0]["detail"] == '{"value":1}'
    reconciled.close()


def test_hosted_connection_failure_and_auth_refresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """w7: Exercise direct-primary write and authentication failure behavior."""
    primary = tmp_path / "primary.sqlite"
    _init_primary(primary)

    fake = install_fake_hrana(primary)
    monkeypatch.setitem(sys.modules, "libsql", fake)

    identity = ProjectIdentity(project_id="latency-test", repository="https://example.test/hosted")

    # 1. Direct-primary write verification
    config = DatabaseConfig(
        path="libsql://project.aws-us-east-1.turso.io",
        identity=identity,
        auth_token="valid-token",
    )
    db = TodoDatabase.open(config)
    assert len(fake.connections) >= 1
    # A write commits against the direct primary.
    db.record_event(actor="tester", action="probe", detail={"x": 1})
    db.close()

    # 2. Auth failure simulation
    from todo_db.errors import HostedAuthError

    def failing_connect(database: Any, **kwargs: Any):
        raise ValueError("Hrana: unauthorized: invalid authentication token")

    monkeypatch.setattr(fake, "connect", failing_connect)
    with pytest.raises(HostedAuthError, match="hosted backend connection failed"):
        TodoDatabase.open(
            DatabaseConfig(
                path="libsql://project.aws-us-east-1.turso.io",
                identity=identity,
                auth_token="expired-token",
            )
        )
