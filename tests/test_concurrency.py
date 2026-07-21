"""Real multi-process tracker contention tests."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _command(database: Path, actor: str, *args: str) -> list[str]:
    return [
        "uv",
        "run",
        "todo-db",
        "--db",
        str(database),
        "--project-id",
        "concurrency-test",
        "--repository",
        "todo-db",
        "--actor",
        actor,
        *args,
    ]


def _run(database: Path, actor: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _command(database, actor, *args),
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_two_processes_contending_for_one_claim_have_one_winner(tmp_path: Path) -> None:
    database = tmp_path / "tracker.sqlite"
    assert _run(database, "setup", "init").returncode == 0
    created = _run(
        database,
        "setup",
        "create",
        "contested-item",
        "--title",
        "Contested item",
        "--worktree",
        "local",
        "--priority",
        "medium",
        "--description",
        "An item claimed concurrently by separate processes.",
    )
    assert created.returncode == 0, created.stderr

    processes = {
        actor: subprocess.Popen(
            _command(database, actor, "claim", "contested-item"),
            cwd=PROJECT_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for actor in ("actor-a", "actor-b")
    }
    results = {actor: process.communicate(timeout=20) + (process.returncode,) for actor, process in processes.items()}
    winners = [actor for actor, (_, _, returncode) in results.items() if returncode == 0]
    losers = [actor for actor, (_, _, returncode) in results.items() if returncode == 2]
    assert len(winners) == 1, results
    assert len(losers) == 1, results
    assert "claimed" in results[losers[0]][1]

    shown = _run(database, "observer", "show", "contested-item", "--json")
    assert shown.returncode == 0, shown.stderr
    assert json.loads(shown.stdout)["claimed_by"] == winners[0]
