"""Store-level proving cases: round-trip, validation, readiness, claims."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from todo_db import store
from todo_db.errors import TodoError


def _snap() -> store.Snapshot:
    return store.Snapshot(index=store.empty_index(), details={})


def test_round_trip_create_edit_list_show_close() -> None:
    snap = _snap()
    store.op_create(snap, item_id="alpha", title="Alpha task", priority="high", description="Do alpha")
    store.op_create(snap, item_id="beta", title="Beta task", needs=["alpha"])
    rows = store.query_items(snap)
    assert [item_id for item_id, _ in rows] == ["alpha", "beta"]
    assert store.is_ready("alpha", snap) is True
    assert store.is_ready("beta", snap) is False
    out = store.op_update(snap, "alpha", title="Alpha task v2")
    assert out["changed"] == ["title"]
    take = store.op_take(snap, "alpha", "w1")
    assert snap.index["items"]["alpha"]["status"] == "active"
    store.op_finish(snap, "alpha", "w1", take["claim"]["generation"])
    assert snap.index["items"]["alpha"]["status"] == "done"
    assert store.is_ready("beta", snap) is True
    store.validate_snapshot(snap.index, snap.details)


def test_finish_needs_no_work_units_scope_or_attestation() -> None:
    snap = _snap()
    store.op_create(snap, item_id="plain", title="Ordinary todo")
    take = store.op_take(snap, "plain", "w1")
    result = store.op_finish(snap, "plain", "w1", take["claim"]["generation"])
    assert result["status"] == "done"


def test_rejects_duplicate_ids_unknown_states_invalid_deps_cycles() -> None:
    snap = _snap()
    store.op_create(snap, item_id="a", title="A task")
    with pytest.raises(TodoError):
        store.op_create(snap, item_id="a", title="A dup")
    with pytest.raises(TodoError):
        store.op_create(snap, item_id="b", title="B task", priority="urgent")
    with pytest.raises(TodoError):
        store.op_create(snap, item_id="c", title="C task", needs=["ghost"])
    store.op_create(snap, item_id="x", title="X task")
    store.op_create(snap, item_id="y", title="Y task", needs=["x"])
    with pytest.raises(TodoError):
        store.op_update(snap, "x", needs=["y"])
    with pytest.raises(TodoError):
        store.op_update(snap, "x", needs=["x"])
    with pytest.raises(TodoError):
        store.op_update(snap, "nope", title="ghost")


def test_rejects_unsafe_ids_orphans_missing_details_bad_schema() -> None:
    snap = _snap()
    for bad in ("../evil", "a/b", "UPPER", "-lead", "trail-", ""):
        with pytest.raises(TodoError):
            store.op_create(snap, item_id=bad, title="Bad id")
    store.op_create(snap, item_id="ok", title="Ok task")
    snap.details["stray"] = {"description": "no index entry"}
    with pytest.raises(TodoError):
        store.validate_snapshot(snap.index, snap.details)
    del snap.details["stray"]
    del snap.details["ok"]
    with pytest.raises(TodoError):
        store.validate_snapshot(snap.index, snap.details)
    snap.details["ok"] = {"description": "back"}
    snap.index["schema_version"] = 999
    with pytest.raises(TodoError) as excinfo:
        store.validate_snapshot(snap.index, snap.details)
    assert excinfo.value.code == "E_SCHEMA"
    snap.index["schema_version"] = 1
    snap.details["ok"] = {"title": "shadow copy"}
    with pytest.raises(TodoError):
        store.validate_snapshot(snap.index, snap.details)


def test_priority_ordering_and_filters() -> None:
    snap = _snap()
    store.op_create(snap, item_id="m1", title="low thing", priority="low")
    store.op_create(snap, item_id="c1", title="critical thing", priority="critical")
    store.op_create(snap, item_id="h1", title="high thing", priority="high")
    rows = store.query_items(snap)
    assert [item_id for item_id, _ in rows] == ["c1", "h1", "m1"]
    assert [item_id for item_id, _ in store.query_items(snap, priority="high")] == ["h1"]
    assert [item_id for item_id, _ in store.query_items(snap, text="LOW")] == ["m1"]
    assert [item_id for item_id, _ in store.query_items(snap, status="open")] == ["c1", "h1", "m1"]


def test_claim_loser_cannot_progress_release_or_close() -> None:
    snap = _snap()
    store.op_create(snap, item_id="task", title="Contested")
    take = store.op_take(snap, "task", "winner")
    gen = take["claim"]["generation"]
    with pytest.raises(TodoError):
        store.op_take(snap, "task", "loser")
    with pytest.raises(TodoError) as excinfo:
        store.op_release(snap, "task", "loser", gen)
    assert excinfo.value.code == "E_CLAIM_STALE"
    with pytest.raises(TodoError):
        store.op_finish(snap, "task", "loser", gen)
    with pytest.raises(TodoError):
        store.op_renew(snap, "task", "loser", gen)
    store.op_renew(snap, "task", "winner", gen)
    store.op_release(snap, "task", "winner", gen)
    assert snap.index["items"]["task"]["status"] == "open"


def test_expired_claim_allows_adoption_and_restart() -> None:
    snap = _snap()
    store.op_create(snap, item_id="task", title="Expiring")
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    take = store.op_take(snap, "task", "w1", now=past - timedelta(hours=25))
    assert store.claim_is_live(take["claim"]) is False
    retake = store.op_take(snap, "task", "w2")
    assert retake["adopted"] is False
    assert retake["claim"]["worker"] == "w2"
    gen1 = retake["claim"]["generation"]
    same = store.op_take(snap, "task", "w2")
    assert same["adopted"] is True
    # Re-adoption rotates the generation: the old one goes stale so a
    # duplicated worker identity fails closed instead of sharing control.
    assert same["claim"]["generation"] != gen1
    with pytest.raises(TodoError) as excinfo:
        store.op_release(snap, "task", "w2", gen1)
    assert excinfo.value.code == "E_CLAIM_STALE"
    store.op_finish(snap, "task", "w2", same["claim"]["generation"])


def test_stale_generation_rejected_after_adoption() -> None:
    snap = _snap()
    store.op_create(snap, item_id="task", title="Adopted")
    past = datetime.now(timezone.utc) - timedelta(hours=30)
    old = store.op_take(snap, "task", "w1", now=past)
    new = store.op_take(snap, "task", "w2")
    assert new["claim"]["generation"] != old["claim"]["generation"]
    for op in ("renew", "release", "finish"):
        with pytest.raises(TodoError) as excinfo:
            if op == "renew":
                store.op_renew(snap, "task", "w1", old["claim"]["generation"])
            elif op == "release":
                store.op_release(snap, "task", "w1", old["claim"]["generation"])
            else:
                store.op_finish(snap, "task", "w1", old["claim"]["generation"])
        assert excinfo.value.code == "E_CLAIM_STALE"


def test_index_entries_reject_unknown_keys() -> None:
    snap = _snap()
    store.op_create(snap, item_id="task", title="Strict")
    snap.index["items"]["task"]["description"] = "shadow copy in the index"
    with pytest.raises(TodoError):
        store.validate_snapshot(snap.index, snap.details)


def test_update_cannot_bypass_claim_paths() -> None:
    snap = _snap()
    store.op_create(snap, item_id="task", title="Parked")
    # open -> blocked -> open is the only update status path.
    store.op_update(snap, "task", status="blocked")
    assert snap.index["items"]["task"]["status"] == "blocked"
    store.op_update(snap, "task", status="open")
    with pytest.raises(TodoError):
        store.op_update(snap, "task", status="active")
    take = store.op_take(snap, "task", "w1")
    assert snap.index["items"]["task"]["status"] == "active"
    with pytest.raises(TodoError):
        store.op_update(snap, "task", status="open")
    with pytest.raises(TodoError):
        store.op_update(snap, "task", status="blocked")
    store.op_release(snap, "task", "w1", take["claim"]["generation"])
    store.op_update(snap, "task", status="blocked")
    with pytest.raises(TodoError):
        # A live claim blocks even the parked moves.
        store.op_take(snap, "task", "w1")
        store.op_update(snap, "task", status="open")


def test_symlinked_state_files_are_refused(tmp_path) -> None:
    snap = _snap()
    store.op_create(snap, item_id="aa", title="Real", description="here")
    store.save_snapshot(tmp_path, snap)
    outside = tmp_path / "outside.json"
    outside.write_text('{"description": "smuggled"}', encoding="utf-8")
    target = tmp_path / "items" / "aa.json"
    target.unlink()
    target.symlink_to(outside)
    with pytest.raises(TodoError):
        store.load_snapshot(tmp_path)


def test_terminal_tasks_carry_no_claim_and_drop_clears() -> None:
    snap = _snap()
    store.op_create(snap, item_id="task", title="Droppable")
    store.op_drop(snap, "task")
    assert snap.index["items"]["task"]["status"] == "dropped"
    with pytest.raises(TodoError):
        store.op_take(snap, "task", "w1")


def test_quiescence_refuses_live_claims() -> None:
    snap = _snap()
    store.op_create(snap, item_id="task", title="Busy")
    store.op_take(snap, "task", "w1")
    with pytest.raises(TodoError) as excinfo:
        store.assert_quiescent(snap)
    assert excinfo.value.code == "E_ACTIVE_CLAIMS"


def test_atomic_save_and_load_round_trip(tmp_path) -> None:
    snap = _snap()
    store.op_create(snap, item_id="aa", title="Persist me", description="hello")
    store.save_snapshot(tmp_path, snap)
    assert (tmp_path / "index.json").is_file()
    assert (tmp_path / "items" / "aa.json").is_file()
    loaded = store.load_snapshot(tmp_path)
    assert loaded.index["items"]["aa"]["title"] == "Persist me"
    assert loaded.details["aa"]["description"] == "hello"
