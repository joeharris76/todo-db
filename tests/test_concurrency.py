"""Real multi-process tracker contention tests.

The lifecycle-mutation CLI verbs (`create`, `claim`, `show`, ...) were removed in
0.6.0 when MCP became the sole agent interface. The cross-process claim
contention invariant (`BEGIN IMMEDIATE` + `PRAGMA busy_timeout`) is unchanged and
still load-bearing, so it is exercised here directly against `TodoTracker` -- the
same class both the floor CLI and the MCP server call.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from todo_db import DatabaseConfig, ProjectIdentity, TodoDatabase, TodoTracker


PROJECT_ROOT = Path(__file__).resolve().parents[1]

IDENTITY = ProjectIdentity(project_id="concurrency-test", repository="todo-db")

_CLAIM_SCRIPT = """
import sys
from todo_db import DatabaseConfig, ProjectIdentity, TodoDatabase, TodoTracker
from todo_db.errors import TodoError

config = DatabaseConfig(
    path=sys.argv[1],
    identity=ProjectIdentity(project_id="concurrency-test", repository="todo-db"),
)
db = TodoDatabase.open(config)
try:
    TodoTracker(db, actor=sys.argv[2]).claim("contested-item")
except TodoError as exc:
    print(exc, file=sys.stderr)
    sys.exit(2)
finally:
    db.close()
sys.exit(0)
"""


def test_two_processes_contending_for_one_claim_have_one_winner(tmp_path: Path) -> None:
    database = tmp_path / "tracker.sqlite"
    db = TodoDatabase.open(DatabaseConfig(path=database, identity=IDENTITY))
    TodoTracker(db, actor="setup").create_item(
        item_id="contested-item",
        title="Contested item",
        worktree="local",
        priority="medium",
        description="An item claimed concurrently by separate processes.",
    )
    db.close()

    processes = {
        actor: subprocess.Popen(
            [sys.executable, "-c", _CLAIM_SCRIPT, str(database), actor],
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

    db = TodoDatabase.open(DatabaseConfig(path=database, identity=IDENTITY))
    claimed_by = TodoTracker(db, actor="observer").get_item("contested-item")["claimed_by"]
    db.close()
    assert claimed_by == winners[0]
