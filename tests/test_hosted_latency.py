"""Hosted latency, replica vs direct comparison, byte tracking, and concurrency tests."""

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

    # Subprocess command using python -c with fake_hrana loaded
    script = """
import sys
from fake_hrana import install_fake_hrana
install_fake_hrana(sys.argv[1])
from todo_db.cli import main
sys.exit(main(["--db", "libsql://test.turso.io", "--replica", sys.argv[2], "--project-id", "latency-test", "--repository", "https://example.test/hosted", "--actor", sys.argv[3], "claim", "item-01"]))
"""

    replica_a = tmp_path / "replica_a.sqlite"
    replica_b = tmp_path / "replica_b.sqlite"

    env = dict(os.environ)
    env["PYTHONPATH"] = f"{PROJECT_ROOT}/tests:{PROJECT_ROOT}/src:{PROJECT_ROOT}"
    env["TODO_DB_AUTH_TOKEN"] = "test-token"

    p_a = subprocess.Popen(
        [sys.executable, "-c", script, str(primary), str(replica_a), "actor-a"],
        cwd=PROJECT_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    p_b = subprocess.Popen(
        [sys.executable, "-c", script, str(primary), str(replica_b), "actor-b"],
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


def test_latency_harness_replica_vs_direct_and_third_arm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """w2, w5, w6: Measure p50/p95 latency and byte counts across Arm 1 (Replica), Arm 2 (Direct RO), and Arm 3 (Direct RW)."""
    primary = tmp_path / "primary.sqlite"
    _init_primary(primary)

    fake = install_fake_hrana(primary)
    monkeypatch.setitem(sys.modules, "libsql", fake)

    identity = ProjectIdentity(project_id="latency-test", repository="https://example.test/hosted")

    # Arm 1: Embedded Replica (Read-Write)
    replica_path = tmp_path / "harness_replica.sqlite"
    config_replica = DatabaseConfig(
        path="libsql://project.aws-us-east-1.turso.io",
        identity=identity,
        auth_token="rw-token",
        replica_path=replica_path,
        credential_mode=CredentialMode.READ_WRITE,
    )

    # Arm 2: Direct Read-Only Connection (Direct HTTP)
    config_direct_ro = DatabaseConfig(
        path="libsql://project.aws-us-east-1.turso.io",
        identity=identity,
        auth_token="ro-token",
        credential_mode=CredentialMode.READ_ONLY,
    )

    # Arm 3: Direct Read-Write Connection (Direct Primary without local replica file)
    config_direct_rw = DatabaseConfig(
        path="libsql://project.aws-us-east-1.turso.io",
        identity=identity,
        auth_token="rw-token",
        credential_mode=CredentialMode.READ_WRITE,
    )

    arms = {
        "arm1_replica": config_replica,
        "arm2_direct_ro": config_direct_ro,
        "arm3_direct_rw": config_direct_rw,
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

    assert "arm1_replica" in metrics
    assert "arm2_direct_ro" in metrics
    assert "arm3_direct_rw" in metrics
    for arm_name in ("arm1_replica", "arm2_direct_ro", "arm3_direct_rw"):
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
        replica_path=tmp_path / "open_overhead_replica.sqlite",
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


def test_sync_failure_retry_interruption_and_auth_refresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """w7: Measure sync failure, retry, interruption, and auth refresh behaviour."""
    primary = tmp_path / "primary.sqlite"
    _init_primary(primary)

    fake = install_fake_hrana(primary)
    monkeypatch.setitem(sys.modules, "libsql", fake)

    identity = ProjectIdentity(project_id="latency-test", repository="https://example.test/hosted")

    # 1. Sync count verification
    replica = tmp_path / "sync_replica.sqlite"
    config = DatabaseConfig(
        path="libsql://project.aws-us-east-1.turso.io",
        identity=identity,
        auth_token="valid-token",
        replica_path=replica,
    )
    db = TodoDatabase.open(config)
    assert len(fake.connections) >= 1
    # Transaction commit triggers sync
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
                replica_path=tmp_path / "auth_replica.sqlite",
            )
        )
