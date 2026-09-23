"""Time-held readiness: a ``not_before`` hold keeps an open task out of the ready queue."""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from todo_db import git_backend, store
from todo_db.errors import TodoError
from todo_db.service import TrackerService

NOW = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)
HOLD = "2026-09-24T12:00:00Z"
HOLD_AT = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)


def _snap() -> store.Snapshot:
    return store.Snapshot(index=store.empty_index(), details={})


def _held(item_id: str = "held", **kwargs) -> store.Snapshot:
    snap = _snap()
    store.op_create(snap, item_id=item_id, title="Held task", not_before=HOLD, now=NOW, **kwargs)
    return snap


def test_ready_only_at_or_after_the_hold() -> None:
    snap = _held()
    assert snap.index["items"]["held"]["status"] == "open"
    assert store.is_ready("held", snap, NOW) is False
    assert store.is_ready("held", snap, HOLD_AT - timedelta(seconds=1)) is False
    assert store.is_ready("held", snap, HOLD_AT) is True
    assert store.is_ready("held", snap, HOLD_AT + timedelta(days=1)) is True
    assert store.ready_rows(snap, NOW) == []
    assert [item_id for item_id, _ in store.ready_rows(snap, HOLD_AT)] == ["held"]


def test_waiting_until_reports_open_and_blocked_future_holds() -> None:
    snap = _held()
    assert store.waiting_until("held", snap, NOW) == HOLD
    assert store.waiting_until("held", snap, HOLD_AT) is None
    store.op_update(snap, "held", status="blocked")
    assert store.waiting_until("held", snap, NOW) == HOLD
    assert store.is_ready("held", snap, HOLD_AT) is False


def test_closed_task_keeps_an_inert_hold() -> None:
    snap = _held()
    take = store.op_take(snap, "held", "w1", now=NOW)
    store.op_finish(snap, "held", "w1", take["claim"]["generation"])
    assert snap.details["held"]["not_before"] == HOLD
    assert store.waiting_until("held", snap, NOW) is None
    store.validate_snapshot(snap.index, snap.details)


def test_hold_combines_with_unmet_needs() -> None:
    snap = _snap()
    store.op_create(snap, item_id="dep", title="Dependency")
    store.op_create(snap, item_id="held", title="Held task", needs=["dep"], not_before=HOLD, now=NOW)
    assert store.is_ready("held", snap, HOLD_AT) is False
    take = store.op_take(snap, "dep", "w1", now=NOW)
    store.op_finish(snap, "dep", "w1", take["claim"]["generation"])
    assert store.is_ready("held", snap, NOW) is False
    assert store.is_ready("held", snap, HOLD_AT) is True


def test_claimed_held_task_is_not_ready_or_waiting() -> None:
    # op_take stays readiness-agnostic; the service take is the gate.
    snap = _held()
    store.op_take(snap, "held", "w1", now=NOW)
    assert store.is_ready("held", snap, HOLD_AT) is False
    assert store.waiting_until("held", snap, NOW) is None


def test_hold_does_not_change_unlock_counts() -> None:
    snap = _snap()
    store.op_create(snap, item_id="held", title="Held task", not_before=HOLD, now=NOW)
    store.op_create(snap, item_id="after", title="After", needs=["held"])
    assert store.downstream_unlocks("held", snap) == 1
    assert store.is_ready("after", snap, HOLD_AT) is False


def test_update_sets_normalizes_and_clears() -> None:
    snap = _snap()
    store.op_create(snap, item_id="task", title="Task")
    out = store.op_update(snap, "task", not_before="2026-09-24T14:00:00+02:00", now=NOW)
    assert out["changed"] == ["not_before"]
    assert snap.details["task"]["not_before"] == HOLD
    store.op_update(snap, "task", not_before="", now=NOW)
    assert "not_before" not in snap.details["task"]
    assert store.is_ready("task", snap, NOW) is True


def test_fractional_seconds_round_up_so_a_hold_never_ends_early() -> None:
    snap = _snap()
    store.op_create(snap, item_id="task", title="Task", not_before="2026-09-24T11:59:59.25Z", now=NOW)
    assert snap.details["task"]["not_before"] == HOLD
    store.op_update(snap, "task", not_before="2026-09-24T12:00:00.000000Z", now=NOW)
    assert snap.details["task"]["not_before"] == HOLD


@pytest.mark.parametrize(
    "bad",
    ["2026-09-24T12:00:00", "2026-09-24", "tomorrow", "2026-13-01T00:00:00Z", "   ", "2026-09-23T11:59:59Z", "2026-09-23T12:00:00Z"],
)
def test_rejects_naive_malformed_and_past_holds(bad: str) -> None:
    snap = _snap()
    with pytest.raises(TodoError):
        store.op_create(snap, item_id="task", title="Task", not_before=bad, now=NOW)
    assert "task" not in snap.index["items"]
    store.op_create(snap, item_id="task", title="Task")
    with pytest.raises(TodoError):
        store.op_update(snap, "task", not_before=bad, now=NOW)
    assert "not_before" not in snap.details["task"]


def test_stored_hold_loads_after_it_passes_and_on_every_schema_version() -> None:
    for version in store.SUPPORTED_SCHEMA_VERSIONS:
        snap = _held()
        snap.index["schema_version"] = version
        if version < 3:
            snap.index.pop("batches", None)
        store.validate_snapshot(snap.index, snap.details)
    snap = _snap()
    store.op_create(snap, item_id="task", title="Task")
    before = snap.index["schema_version"]
    store.op_update(snap, "task", not_before=HOLD, now=NOW)
    assert snap.index["schema_version"] == before


@pytest.mark.parametrize("bad", ["2026-09-24T12:00:00+00:00", "2026-09-24T12:00:00", 1790000000, None])
def test_malformed_stored_hold_fails_validation(bad) -> None:
    snap = _held()
    snap.details["held"]["not_before"] = bad
    with pytest.raises(TodoError):
        store.validate_snapshot(snap.index, snap.details)


def _svc(tmp_path: Path) -> TrackerService:
    remote = tmp_path / "hold.git"
    subprocess.run(["git", "init", "--quiet", "--bare", str(remote)], check=True)
    ref = git_backend.StateRef(remote=str(remote), branch="todo-state")
    git_backend.bootstrap(ref)
    return TrackerService(ref=ref, cache_dir=tmp_path / "cache", worker="w1")


def test_service_hides_refuses_and_releases_a_held_task(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    far = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert svc.create_item("held", "Held task", not_before=far)["ok"]
    assert svc.create_item("plain", "Plain task")["ok"]

    listed = svc.list_items()["data"]["items"]
    rows = {row["id"]: row for row in listed}
    assert rows["held"]["ready"] is False and rows["held"]["waiting_until"] == far
    assert "waiting_until" not in rows["plain"]
    ready = svc.list_items(ready_only=True)["data"]["items"]
    assert [row["id"] for row in ready] == ["plain"]
    shown = svc.show_item("held")["data"]
    assert shown["ready"] is False and shown["waiting_until"] == far

    refused = svc.take("held")
    assert not refused["ok"] and refused["code"] == "E_NOTHING_READY" and refused["kind"] == "gate"
    assert far in refused["error"] and 'not_before=""' in refused["error"]

    # Parking a held task must not unlock take.
    assert svc.update_item("held", status="blocked")["ok"]
    parked = svc.take("held")
    assert not parked["ok"] and parked["code"] == "E_NOTHING_READY"
    assert svc.show_item("held")["data"]["waiting_until"] == far
    assert svc.update_item("held", status="open")["ok"]

    bad = svc.update_item("held", not_before="2026-09-24T12:00:00")
    assert not bad["ok"] and bad["code"] == "E_STATE"
    assert svc.update_item("held", not_before="")["ok"]
    shown = svc.show_item("held")["data"]
    assert shown["ready"] is True and "waiting_until" not in shown
    took = svc.take("held")
    assert took["ok"], took
