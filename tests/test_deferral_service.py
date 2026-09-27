"""Deferral service proving cases: publication, paging, audit, finish gate."""

from __future__ import annotations

import subprocess
from pathlib import Path

from todo_db import git_backend
from todo_db.service import TrackerService


def _svc(tmp_path: Path, worker: str = "w1", name: str = "defer.git") -> TrackerService:
    remote = tmp_path / name
    subprocess.run(["git", "init", "--quiet", "--bare", str(remote)], check=True)
    ref = git_backend.StateRef(remote=str(remote), branch="todo-state")
    git_backend.bootstrap(ref)
    return TrackerService(
        ref=ref, cache_dir=tmp_path / "cache", worker=worker,
        session_id="session-a", client_name="pytest",
    )


def test_deferral_lifecycle_over_publication(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    assert svc.create_item("alpha", "Alpha task")["ok"]
    deferred = svc.defer_item("alpha", "Later work", "needs infra")
    assert deferred["ok"] and deferred["data"]["deferral"]["id"] == 1
    listed = svc.list_deferrals()
    assert listed["ok"] and listed["data"]["total"] == 1
    shown = svc.show_item("alpha")
    assert shown["ok"] and shown["data"]["open_deferrals"] == [1]
    took = svc.take("alpha")
    assert took["ok"], took
    blocked = svc.finish("alpha", took["data"]["claim"]["generation"])
    assert not blocked["ok"] and blocked["code"] == "E_OPEN_DEFERRALS"
    promoted = svc.promote_deferral("alpha", 1)
    assert promoted["ok"], promoted
    assert promoted["data"]["resolved_item"] == "alpha-deferral-1"
    assert svc.show_item("alpha-deferral-1")["ok"]
    assert svc.list_deferrals()["data"]["total"] == 0
    assert svc.list_deferrals(resolution="all")["data"]["total"] == 1
    finished = svc.finish("alpha", took["data"]["claim"]["generation"])
    assert finished["ok"], finished


def test_dismiss_resolves_and_records_history(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    assert svc.create_item("alpha", "Alpha task")["ok"]
    assert svc.defer_item("alpha", "Maybe", "undecided")["ok"]
    refused = svc.dismiss_deferral("alpha", 1, "  ")
    assert not refused["ok"]
    dismissed = svc.dismiss_deferral("alpha", 1, "not needed")
    assert dismissed["ok"], dismissed
    double = svc.promote_deferral("alpha", 1)
    assert not double["ok"]
    snap = git_backend.read(svc.ref, tmp_path / "cache").snapshot
    ops = [entry["op"] for entry in snap.details["alpha"]["sessions"]]
    assert ops == ["create", "defer", "dismiss"]
    assert svc.show_item("alpha")["data"].get("open_deferrals") is None


def test_list_deferrals_pages_with_stable_cursors(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    assert svc.create_item("alpha", "Alpha task")["ok"]
    for n in range(3):
        assert svc.defer_item("alpha", f"Work {n}", "later")["ok"]
    first = svc.list_deferrals(limit=2)
    assert first["ok"] and len(first["data"]["deferrals"]) == 2
    cursor = first["data"]["next_cursor"]
    second = svc.list_deferrals(limit=2, cursor=cursor)
    assert second["ok"] and len(second["data"]["deferrals"]) == 1
    assert second["data"]["deferrals"][0]["id"] == 3
    scoped = svc.list_deferrals(from_item="alpha", resolution="open")
    assert scoped["ok"] and scoped["data"]["total"] == 3
    unknown = svc.list_deferrals(from_item="ghost")
    assert not unknown["ok"]


def test_promote_to_existing_item_leaves_successor_history_untouched(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    assert svc.create_item("alpha", "Alpha task")["ok"]
    assert svc.create_item("beta", "Beta task")["ok"]
    assert svc.defer_item("alpha", "Follow-up", "later")["ok"]
    out = svc.promote_deferral("alpha", 1, to_item="beta")
    assert out["ok"] and out["data"]["resolved_item"] == "beta"
    snap = git_backend.read(svc.ref, tmp_path / "cache").snapshot
    assert [entry["op"] for entry in snap.details["alpha"]["sessions"]] == ["create", "defer", "promote"]
    assert [entry["op"] for entry in snap.details["beta"]["sessions"]] == ["create"]
