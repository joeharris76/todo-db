"""Deferral lifecycle: open/promote/dismiss transitions and the finish gate."""

from __future__ import annotations

import pytest

from todo_db import store
from todo_db.errors import TodoError


def _snap() -> store.Snapshot:
    snap = store.Snapshot(index=store.empty_index(), details={})
    store.op_create(snap, item_id="alpha", title="Alpha task")
    return snap


def test_defer_creates_open_rows_with_scoped_ids() -> None:
    snap = _snap()
    first = store.op_defer(snap, "alpha", summary="Later work", reason="needs infra")
    second = store.op_defer(snap, "alpha", summary="More later", reason="waiting")
    assert (first["id"], first["resolution"]) == (1, "open")
    assert (second["id"], second["resolution"]) == (2, "open")
    assert first["from_item"] == "alpha"
    assert [row["id"] for row in store.open_deferrals(snap, "alpha")] == [1, 2]
    store.validate_snapshot(snap.index, snap.details)


def test_defer_rejects_bad_input_and_terminal_tasks() -> None:
    snap = _snap()
    for summary, reason in [("", "ok"), ("ok", ""), ("   ", "ok")]:
        with pytest.raises(TodoError):
            store.op_defer(snap, "alpha", summary=summary, reason=reason)
    with pytest.raises(TodoError):
        store.op_defer(snap, "ghost", summary="s", reason="r")
    take = store.op_take(snap, "alpha", "w1")
    store.op_finish(snap, "alpha", "w1", take["claim"]["generation"])
    with pytest.raises(TodoError):
        store.op_defer(snap, "alpha", summary="s", reason="r")


def test_promote_creates_successor_and_links_atomically() -> None:
    snap = _snap()
    store.op_defer(snap, "alpha", summary="Follow-up", reason="later")
    out = store.op_promote_deferral(snap, "alpha", 1)
    assert out["resolved_item"] == "alpha-deferral-1"
    assert snap.details["alpha-deferral-1"]["description"].startswith("Promoted from deferral 1")
    rows = store.query_deferrals(snap, resolution="all")
    assert [(row["id"], row["resolution"], row["resolved_item"]) for row in rows] == [
        (1, "promoted", "alpha-deferral-1"),
    ]
    store.validate_snapshot(snap.index, snap.details)


def test_promote_links_existing_item_and_rejects_double_resolve() -> None:
    snap = _snap()
    store.op_create(snap, item_id="beta", title="Beta")
    store.op_defer(snap, "alpha", summary="Follow-up", reason="later")
    out = store.op_promote_deferral(snap, "alpha", 1, to_item="beta")
    assert out["resolved_item"] == "beta"
    with pytest.raises(TodoError):
        store.op_promote_deferral(snap, "alpha", 1, to_item="beta")
    with pytest.raises(TodoError):
        store.op_dismiss_deferral(snap, "alpha", 1, reason="too late")
    with pytest.raises(TodoError):
        store.op_promote_deferral(snap, "alpha", 99, to_item="beta")
    with pytest.raises(TodoError):
        store.op_promote_deferral(snap, "alpha", 1, to_item="ghost")


def test_dismiss_needs_reason_and_resolves_once() -> None:
    snap = _snap()
    store.op_defer(snap, "alpha", summary="Maybe", reason="undecided")
    with pytest.raises(TodoError):
        store.op_dismiss_deferral(snap, "alpha", 1, reason="  ")
    out = store.op_dismiss_deferral(snap, "alpha", 1, reason="not needed")
    assert out["resolution"] == "dismissed"
    rows = store.query_deferrals(snap, resolution="dismissed")
    assert rows[0]["resolved_reason"] == "not needed"
    with pytest.raises(TodoError):
        store.op_dismiss_deferral(snap, "alpha", 1, reason="again")


def test_finish_refuses_while_open_deferrals_exist() -> None:
    snap = _snap()
    store.op_defer(snap, "alpha", summary="Later", reason="infra")
    take = store.op_take(snap, "alpha", "w1")
    with pytest.raises(TodoError) as excinfo:
        store.op_finish(snap, "alpha", "w1", take["claim"]["generation"])
    assert excinfo.value.code == "E_OPEN_DEFERRALS"
    store.op_dismiss_deferral(snap, "alpha", 1, reason="done elsewhere")
    assert store.op_finish(snap, "alpha", "w1", take["claim"]["generation"])["status"] == "done"


def test_query_deferrals_filters_and_orders() -> None:
    snap = _snap()
    store.op_create(snap, item_id="beta", title="Beta")
    store.op_defer(snap, "beta", summary="B work", reason="later")
    store.op_defer(snap, "alpha", summary="A work", reason="later")
    assert [(row["from_item"], row["id"]) for row in store.query_deferrals(snap)] == [
        ("alpha", 1), ("beta", 1),
    ]
    store.op_dismiss_deferral(snap, "alpha", 1, reason="nope")
    assert store.query_deferrals(snap) == store.query_deferrals(snap, from_item="beta")
    assert len(store.query_deferrals(snap, resolution="all")) == 2
    with pytest.raises(TodoError):
        store.query_deferrals(snap, resolution="bogus")
    with pytest.raises(TodoError):
        store.query_deferrals(snap, from_item="ghost")


def test_max_length_summary_promotes_with_default_title() -> None:
    snap = _snap()
    row = store.op_defer(snap, "alpha", summary="x" * 200, reason="at the bound")
    out = store.op_promote_deferral(snap, "alpha", row["id"])
    assert out["resolved_item"] == "alpha-deferral-1"
    with pytest.raises(TodoError):
        store.op_defer(snap, "alpha", summary="x" * 201, reason="over")


def test_snapshot_validation_rejects_malformed_rows() -> None:
    snap = _snap()
    store.op_defer(snap, "alpha", summary="ok", reason="ok")
    snap.details["alpha"]["deferrals"][0]["resolution"] = "promoted"
    with pytest.raises(TodoError):
        store.validate_snapshot(snap.index, snap.details)
