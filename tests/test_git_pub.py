"""Git publication proving cases against disposable local bare remotes."""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest

from todo_db import git_backend
from todo_db import store as S
from todo_db.errors import TodoError


def _remote(tmp_path: Path) -> git_backend.StateRef:
    remote = tmp_path / "state.git"
    subprocess.run(["git", "init", "--quiet", "--bare", str(remote)], check=True)
    return git_backend.StateRef(remote=str(remote), branch="todo-state")


def _tip(ref: git_backend.StateRef) -> str | None:
    return git_backend.ls_remote_tip(ref)


def test_bootstrap_refuses_to_overwrite() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        ref = _remote(Path(d))
        sha = git_backend.bootstrap(ref)
        assert sha
        with pytest.raises(TodoError):
            git_backend.bootstrap(ref)


def test_round_trip_mutate_and_read(tmp_path: Path) -> None:
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    out = git_backend.mutate(
        ref, op="create", summary="add t1", worker="w1",
        apply=lambda snap: (S.op_create(snap, item_id="t1", title="Task one"), {"id": "t1"}),
    )
    assert out.ok and out.sha, out
    ro = git_backend.read(ref, tmp_path / "cache")
    assert ro.rev == out.sha and ro.stale is False
    assert ro.snapshot.index["items"]["t1"]["title"] == "Task one"


def test_missing_branch_read_reports_bootstrap(tmp_path: Path) -> None:
    ref = _remote(tmp_path)
    with pytest.raises(TodoError):
        git_backend.read(ref, tmp_path / "cache")


def test_race_for_one_task_has_one_winner(tmp_path: Path) -> None:
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    git_backend.mutate(
        ref, op="create", summary="add contested", worker="setup",
        apply=lambda snap: (S.op_create(snap, item_id="c", title="Contested"), {}),
    )
    results: dict[str, git_backend.MutationOutcome] = {}

    def take(worker: str) -> None:
        results[worker] = git_backend.mutate(
            ref, op="take", summary=f"{worker} takes c", worker=worker,
            apply=lambda snap: (S.op_take(snap, "c", worker), {}),
        )

    threads = [threading.Thread(target=take, args=(w,)) for w in ("aaa", "bbb")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    winners = [w for w, r in results.items() if r.ok]
    losers = [w for w, r in results.items() if not r.ok]
    assert len(winners) == 1 and len(losers) == 1
    loser = losers[0]
    assert results[loser].code in ("E_CONFLICT", "E_CLAIM_STALE")
    # Loser cannot release or finish the winner's task.
    winner = winners[0]
    ro = git_backend.read(ref, tmp_path / "cache")
    gen = ro.snapshot.index["items"]["c"]["claim"]["generation"]
    assert ro.snapshot.index["items"]["c"]["claim"]["worker"] == winner
    bad_release = git_backend.mutate(
        ref, op="release", summary="loser releases", worker=loser,
        apply=lambda snap: (S.op_release(snap, "c", loser, gen), {}),
    )
    assert not bad_release.ok
    bad_finish = git_backend.mutate(
        ref, op="finish", summary="loser finishes", worker=loser,
        apply=lambda snap: (S.op_finish(snap, "c", loser, gen), {}),
    )
    assert not bad_finish.ok


def test_unrelated_concurrent_ops_both_survive(tmp_path: Path) -> None:
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    results: dict[str, git_backend.MutationOutcome] = {}

    def create(item_id: str) -> None:
        results[item_id] = git_backend.mutate(
            ref, op="create", summary=f"add {item_id}", worker=item_id,
            apply=lambda snap: (S.op_create(snap, item_id=item_id, title=f"Task {item_id}"), {}),
        )

    threads = [threading.Thread(target=create, args=(i,)) for i in ("u1", "u2")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert all(r.ok for r in results.values()), results
    ro = git_backend.read(ref, tmp_path / "cache2")
    assert set(ro.snapshot.index["items"]) == {"u1", "u2"}


def test_conflicting_edits_never_silently_overwrite(tmp_path: Path) -> None:
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    git_backend.mutate(
        ref, op="create", summary="add t", worker="setup",
        apply=lambda snap: (S.op_create(snap, item_id="t", title="v1"), {}),
    )

    def update_if_v1(snap: S.Snapshot) -> dict:
        if snap.index["items"]["t"]["title"] != "v1":
            raise TodoError("conflict: title is no longer v1", code="E_CONFLICT")
        return S.op_update(snap, "t", title="v2")

    def update_if_v1_other(snap: S.Snapshot) -> dict:
        if snap.index["items"]["t"]["title"] != "v1":
            raise TodoError("conflict: title is no longer v1", code="E_CONFLICT")
        return S.op_update(snap, "t", title="v3")

    results: dict[str, git_backend.MutationOutcome] = {}
    threads = [
        threading.Thread(target=lambda: results.setdefault("a", git_backend.mutate(
            ref, op="update", summary="set v2", worker="a", apply=update_if_v1))),
        threading.Thread(target=lambda: results.setdefault("b", git_backend.mutate(
            ref, op="update", summary="set v3", worker="b", apply=update_if_v1_other))),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    oks = [k for k, r in results.items() if r.ok]
    assert len(oks) == 1
    ro = git_backend.read(ref, tmp_path / "cache")
    assert ro.snapshot.index["items"]["t"]["title"] in ("v2", "v3")


def test_lost_reply_reconciles_without_duplicate(tmp_path: Path) -> None:
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    op_id = git_backend.new_op_id()

    def create(snap: S.Snapshot) -> dict:
        S.op_create(snap, item_id="once", title="Once")
        return {"id": "once"}

    first = git_backend.mutate(ref, op="create", summary="add once", worker="w", op_id=op_id, apply=create)
    assert first.ok
    before = _tip(ref)
    second = git_backend.mutate(ref, op="create", summary="add once", worker="w", op_id=op_id, apply=create)
    assert second.ok and second.sha == first.sha
    assert _tip(ref) == before
    assert git_backend.reconcile(ref, op_id) == first.sha


def test_crash_before_commit_leaves_tip_unchanged(tmp_path: Path) -> None:
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    before = _tip(ref)

    def boom(snap: S.Snapshot) -> dict:
        raise RuntimeError("simulated crash before commit")

    with pytest.raises(RuntimeError):
        git_backend.mutate(ref, op="create", summary="boom", worker="w", apply=boom)
    assert _tip(ref) == before


def test_offline_mutation_never_succeeds_locally(tmp_path: Path) -> None:
    ref = git_backend.StateRef(remote=str(tmp_path / "nope.git"), branch="todo-state")
    out = git_backend.mutate(
        ref, op="create", summary="offline", worker="w",
        apply=lambda snap: (S.op_create(snap, item_id="x", title="X"), {}),
    )
    assert not out.ok and out.outcome == "offline"


def test_readers_stay_on_one_accepted_snapshot(tmp_path: Path) -> None:
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    git_backend.mutate(
        ref, op="create", summary="add t", worker="w",
        apply=lambda snap: (S.op_create(snap, item_id="t", title="v1"), {}),
    )
    ro = git_backend.read(ref, tmp_path / "cache")
    git_backend.mutate(
        ref, op="update", summary="set v2", worker="w",
        apply=lambda snap: S.op_update(snap, "t", title="v2"),
    )
    assert ro.snapshot.index["items"]["t"]["title"] == "v1"
    ro2 = git_backend.read(ref, tmp_path / "cache")
    assert ro2.snapshot.index["items"]["t"]["title"] == "v2"
    assert ro.rev != ro2.rev
