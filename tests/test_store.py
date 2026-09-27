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


def test_session_and_client_validators_accept_bounded_single_lines() -> None:
    assert store.validate_session_id("session-a") == "session-a"
    assert store.validate_session_id("  padded  ") == "padded"
    assert store.validate_client_name("claude-code") == "claude-code"
    for bad in ("", "   ", "has\nnewline", "x" * 257):
        with pytest.raises(TodoError):
            store.validate_session_id(bad)
    for bad in ("", "has\nnewline", "x" * 65):
        with pytest.raises(TodoError):
            store.validate_client_name(bad)


def test_record_session_appends_once_per_operation_id() -> None:
    snap = _snap()
    store.op_create(snap, item_id="task", title="Task")
    assert store.record_session(
        snap, "task", actor="w1", session_id="session-a", client="cli",
        op="create", op_id="a" * 32,
    ) is True
    assert store.record_session(
        snap, "task", actor="w1", session_id="session-a", client="cli",
        op="create", op_id="a" * 32,
    ) is False
    log = snap.details["task"]["sessions"]
    assert len(log) == 1
    entry = log[0]
    assert entry["actor"] == "w1" and entry["session_id"] == "session-a"
    assert entry["client"] == "cli" and entry["op"] == "create"
    assert entry["op_id"] == "a" * 32 and "generation" not in entry
    store.validate_snapshot(snap.index, snap.details)


def test_record_session_rejects_bad_attribution_and_unknown_tasks() -> None:
    snap = _snap()
    store.op_create(snap, item_id="task", title="Task")
    with pytest.raises(TodoError):
        store.record_session(
            snap, "ghost", actor="w1", session_id="s", client=None, op="take", op_id="b" * 32,
        )
    with pytest.raises(TodoError):
        store.record_session(
            snap, "task", actor="w1", session_id="s", client=None, op="merge", op_id="b" * 32,
        )
    with pytest.raises(TodoError):
        store.record_session(
            snap, "task", actor="w1", session_id="bad\nline", client=None, op="take", op_id="b" * 32,
        )
    with pytest.raises(TodoError):
        store.record_session(
            snap, "task", actor="w1", session_id="s", client=None, op="take", op_id="short",
        )
    assert "sessions" not in snap.details["task"]


def test_sessions_validation_covers_shape_dedup_and_absence() -> None:
    snap = _snap()
    store.op_create(snap, item_id="task", title="Task")
    store.validate_snapshot(snap.index, snap.details)  # absence stays valid
    snap.details["task"]["sessions"] = [
        {"actor": "w1", "session_id": "s1", "op": "create", "at": "2026-09-14T00:00:00Z", "op_id": "c" * 32},
        {"actor": "w1", "session_id": "s1", "client": "cli", "op": "take",
         "at": "2026-09-14T01:00:00Z", "op_id": "d" * 32, "generation": "e" * 32},
    ]
    store.validate_snapshot(snap.index, snap.details)
    for broken in (
        "not-a-list",
        [{"actor": "w1"}],  # missing keys
        # Unknown ops stay loadable (forward compat); malformed ones do not.
        [{"actor": "w1", "session_id": "s", "op": "", "at": "2026-09-14T00:00:00Z", "op_id": "f" * 32}],
        [{"actor": "w1", "session_id": "s", "op": "x" * 65, "at": "2026-09-14T00:00:00Z", "op_id": "f" * 32}],
        [{"actor": "w1", "session_id": "s", "op": "has\nnewline", "at": "2026-09-14T00:00:00Z", "op_id": "f" * 32}],
        [{"actor": "w1", "session_id": "s", "op": "take", "at": "not-a-time", "op_id": "f" * 32}],
        [{"actor": "w1", "session_id": "s", "op": "take", "at": "2026-09-14T00:00:00Z", "op_id": "f" * 32},
         {"actor": "w1", "session_id": "s", "op": "take", "at": "2026-09-14T00:00:00Z", "op_id": "f" * 32}],
    ):
        snap.details["task"]["sessions"] = broken
        with pytest.raises(TodoError):
            store.validate_snapshot(snap.index, snap.details)


def test_takeover_reason_validation() -> None:
    assert store.validate_takeover_reason("holder session exhausted") == "holder session exhausted"
    assert store.validate_takeover_reason("  padded  ") == "padded"
    for bad in ("", "   ", "x" * 281, "has\nnewline", "has\rtab", "a\u0085b", "a\u2028b", "a\u2029b"):
        with pytest.raises(TodoError):
            store.validate_takeover_reason(bad)


def test_op_takeover_guards_and_fresh_claim() -> None:
    snap = _snap()
    store.op_create(snap, item_id="task", title="Task")
    with pytest.raises(TodoError):
        store.op_takeover(snap, "task", "taker", "victim", "dead session")
    took = store.op_take(snap, "task", "victim")
    gen = took["claim"]["generation"]
    with pytest.raises(TodoError):
        store.op_takeover(snap, "task", "victim", "victim", "dead session")
    with pytest.raises(TodoError) as exc:
        store.op_takeover(snap, "task", "taker", "someone-else", "dead session")
    assert exc.value.code == "E_CONFLICT"
    out = store.op_takeover(snap, "task", "taker", "victim", "session exhausted")
    assert out["displaced"] == "victim"
    assert out["claim"]["worker"] == "taker"
    assert out["claim"]["generation"] != gen
    assert out["claim"]["renewals"] == 0
    assert out["transferred_batches"] == []
    assert snap.index["items"]["task"]["status"] == "active"
    with pytest.raises(TodoError):
        store.op_renew(snap, "task", "victim", gen)


def test_sessions_accept_future_operations_leniently() -> None:
    snap = _snap()
    store.op_create(snap, item_id="task", title="Task")
    snap.details["task"]["sessions"] = [
        {"actor": "w1", "session_id": "s1", "op": "restore",
         "at": "2026-09-14T00:00:00Z", "op_id": "a" * 32},
    ]
    store.validate_snapshot(snap.index, snap.details)  # unknown op still loads
    with pytest.raises(TodoError):
        store.record_session(
            snap, "task", actor="w1", session_id="s1", client=None,
            op="restore", op_id="b" * 32,
        )
    snap.details["task"]["sessions"] = [
        {"actor": "w1", "session_id": "s1", "op": "x" * 65,
         "at": "2026-09-14T00:00:00Z", "op_id": "a" * 32},
    ]
    with pytest.raises(TodoError):
        store.validate_snapshot(snap.index, snap.details)


def test_identities_reject_unicode_line_separators() -> None:
    for bad in ("a\u0085b", "a\u2028b", "a\u2029b"):
        with pytest.raises(TodoError):
            store.validate_worker(bad)
        with pytest.raises(TodoError):
            store.validate_session_id(bad)
        with pytest.raises(TodoError):
            store.validate_client_name(bad)


def test_updates_preserve_session_history() -> None:
    snap = _snap()
    store.op_create(snap, item_id="task", title="Task")
    assert store.record_session(
        snap, "task", actor="w1", session_id="s1", client=None, op="create", op_id="a" * 32,
    ) is True
    store.op_update(snap, "task", title="Task v2")
    assert len(snap.details["task"]["sessions"]) == 1
    assert snap.details["task"]["sessions"][0]["op_id"] == "a" * 32


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


def test_open_and_blocked_reject_any_claim() -> None:
    snap = _snap()
    store.op_create(snap, item_id="task", title="Strict")
    snap.index["items"]["task"]["claim"] = {
        "worker": "w", "expires_at": "2000-01-01T00:00:00Z",
        "generation": "0" * 32, "renewals": 0,
    }
    with pytest.raises(TodoError):
        store.validate_snapshot(snap.index, snap.details)


def test_drop_needs_generation_while_claimed() -> None:
    snap = _snap()
    store.op_create(snap, item_id="task", title="Held")
    take = store.op_take(snap, "task", "w1")
    gen = take["claim"]["generation"]
    with pytest.raises(TodoError) as excinfo:
        store.op_drop(snap, "task", "w1", "0" * 32)
    assert excinfo.value.code == "E_CLAIM_STALE"
    with pytest.raises(TodoError):
        store.op_drop(snap, "task")
    # Rotation strands the old generation for drop too.
    store.op_take(snap, "task", "w1")
    with pytest.raises(TodoError):
        store.op_drop(snap, "task", "w1", gen)
    fresh = snap.index["items"]["task"]["claim"]["generation"]
    assert store.op_drop(snap, "task", "w1", fresh)["status"] == "dropped"


def test_worker_identities_are_single_line() -> None:
    snap = _snap()
    store.op_create(snap, item_id="task", title="T")
    with pytest.raises(TodoError):
        store.op_take(snap, "task", "w1\nTodo-Op-Id: " + "a" * 32)


def test_symlinked_items_dir_is_refused(tmp_path) -> None:
    snap = _snap()
    store.op_create(snap, item_id="aa", title="Real", description="here")
    store.save_snapshot(tmp_path, snap)
    real_items = tmp_path / "items"
    real_items.rename(tmp_path / "items-real")
    real_items.symlink_to(tmp_path / "items-real", target_is_directory=True)
    with pytest.raises(TodoError):
        store.load_snapshot(tmp_path)


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
