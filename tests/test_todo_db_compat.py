"""Compatibility with state written by the released todo-db 0.15.0.

``tests/fixtures/todo-db-0.15.0-state.bundle`` is a bundle of the ``todo-state``
branch produced by the real 0.15.0 wheel: t1 (done), t2 (needs t1, dropped
with a reason), t3 (open). The rename changed no state format, so tractable
must read it, write onto it, and recover operations 0.15.0 recorded.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tractable import git_backend
from tractable.cli import main
from tractable.git_backend import StateRef
from tractable.service import TrackerService

BUNDLE = Path(__file__).parent / "fixtures" / "todo-db-0.15.0-state.bundle"
OLD_IDENTITY = "todo-db <todo-db@localhost>"
NEW_IDENTITY = "tractable <tractable@localhost>"


def _git(remote: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "--git-dir", str(remote), *args], check=True, capture_output=True, text=True,
    )
    return done.stdout.strip()


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    path = tmp_path / "state.git"
    subprocess.run(["git", "init", "--quiet", "--bare", str(path)], check=True)
    _git(path, "fetch", "--quiet", str(BUNDLE), "todo-state:refs/heads/todo-state")
    return path


def _args(remote: Path, tmp_path: Path, *extra: str) -> list[str]:
    return [
        "--state-remote", str(remote), "--state-branch", "todo-state",
        "--cache-dir", str(tmp_path / "cache"), *extra,
    ]


def _service(remote: Path, tmp_path: Path) -> TrackerService:
    ref = StateRef(remote=str(remote), branch="todo-state")
    return TrackerService(ref=ref, cache_dir=tmp_path / "cache", worker="compat-worker")


def _op_ids(remote: Path) -> list[str]:
    out = _git(remote, "log", "--format=%(trailers:key=Todo-Op-Id,valueonly,separator=%x2c)", "todo-state")
    return [line for line in out.splitlines() if line]


def test_validate_reports_counts(remote: Path, tmp_path: Path, capsys) -> None:
    assert main(["validate", *_args(remote, tmp_path)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["items"] == 3
    assert report["by_status"] == {"done": 1, "dropped": 1, "open": 1}
    assert report["live_claims"] == 0
    assert report["stale"] is False


def test_list_shows_the_three_items(remote: Path, tmp_path: Path, capsys) -> None:
    assert main(["list", *_args(remote, tmp_path)]) == 0
    rows = json.loads(capsys.readouterr().out)["data"]["items"]
    assert {row["id"]: row["status"] for row in rows} == {"t1": "done", "t2": "dropped", "t3": "open"}


def test_take_and_finish_the_open_item(remote: Path, tmp_path: Path) -> None:
    svc = _service(remote, tmp_path)
    took = svc.take("t3")
    assert took["ok"], took
    generation = took["data"]["claim"]["generation"]
    finished = svc.finish("t3", generation)
    assert finished["ok"], finished
    assert finished["data"]["status"] == "done"
    statuses = {row["id"]: row["status"] for row in svc.list_items()["data"]["items"]}
    assert statuses == {"t1": "done", "t2": "dropped", "t3": "done"}


def test_recover_finds_operations_written_by_0_15_0(remote: Path, tmp_path: Path, capsys) -> None:
    tip = _git(remote, "rev-parse", "todo-state")
    op_ids = _op_ids(remote)
    assert len(op_ids) == 7  # bootstrap, three creates, take, finish, drop
    drop_op = op_ids[0]
    assert main(["recover", *_args(remote, tmp_path, "--op-id", drop_op)]) == 0
    found = json.loads(capsys.readouterr().out)
    assert found == {"op_id": drop_op, "rev": tip, "applied": True}

    # An older operation is still found after tractable has written on top.
    svc = _service(remote, tmp_path)
    assert svc.take("t3")["ok"]
    first_op = op_ids[-2]  # the create of t1
    result = git_backend.reconcile(
        StateRef(remote=str(remote), branch="todo-state"), first_op, cache_dir=tmp_path / "cache",
    )
    assert result.state == "applied"
    assert result.sha == _git(remote, "log", "--format=%H", "--grep", f"Todo-Op-Id: {first_op}", "todo-state")


def test_new_commits_use_the_tractable_identity(remote: Path, tmp_path: Path) -> None:
    old_shas = _git(remote, "rev-list", "todo-state").splitlines()
    assert len(old_shas) == 7
    svc = _service(remote, tmp_path)
    created = svc.create_item("t4", "Written by tractable")
    assert created["ok"], created

    new_shas = [sha for sha in _git(remote, "rev-list", "todo-state").splitlines() if sha not in old_shas]
    assert len(new_shas) == 1
    fmt = "%an <%ae>|%cn <%ce>"
    assert _git(remote, "log", "-1", f"--format={fmt}", new_shas[0]) == f"{NEW_IDENTITY}|{NEW_IDENTITY}"
    for sha in old_shas:
        assert _git(remote, "log", "-1", f"--format={fmt}", sha) == f"{OLD_IDENTITY}|{OLD_IDENTITY}"
    assert _git(remote, "log", "-1", "--format=%s", new_shas[0]) == "todo(create): add t4"
