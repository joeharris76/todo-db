"""Prepared-member delivery contract tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from todo_db import store
from todo_db.errors import TodoError
from todo_db.service import TrackerService


def _snap() -> store.Snapshot:
    return store.Snapshot(index=store.empty_index(), details={})


def _git_checkout(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "source"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    (repo / "README").write_text("source\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "--quiet", "-m", "source"], check=True)
    revision = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    return repo, revision


def _verification(revision: str) -> dict:
    return {
        "status": "passed",
        "revision": revision,
        "clean": True,
        "suite": "bounded-upstream",
        "command": ["uv", "run", "--", "pytest", "tests/test_batch_delivery.py", "-q"],
    }


def test_prepare_releases_claim_without_done_and_unlocks_explicit_member(tmp_path: Path) -> None:
    source, revision = _git_checkout(tmp_path)
    snap = _snap()
    store.op_create(
        snap, item_id="a", title="A",
        batch={"batch_id": "batch-1", "member_id": "a", "implementation_dependencies": []},
    )
    store.op_create(
        snap, item_id="b", title="B", needs=["a"],
        batch={"batch_id": "batch-1", "member_id": "b", "implementation_dependencies": ["a"]},
    )
    store.op_create(snap, item_id="c", title="C", needs=["a"])
    claim = store.op_take(snap, "a", "worker")
    receipt = store.op_prepare(
        snap,
        "a",
        "worker",
        claim["claim"]["generation"],
        batch_id="batch-1",
        member_id="a",
        source_worktree=str(source),
        source_revision=revision,
        verification=_verification(revision),
    )
    assert receipt["status"] == "open"
    assert snap.index["items"]["a"]["status"] == "open"
    assert snap.index["items"]["a"]["claim"] is None
    # The edge is explicitly registered as batch metadata; ordinary needs do
    # not become prepared dependencies by accident.
    store.validate_snapshot(snap.index, snap.details)
    assert store.is_ready("b", snap) is True
    assert store.is_ready("c", snap) is False
    assert snap.index["items"]["a"]["status"] != "done"


def test_prepare_rejects_stale_or_dirty_source_and_bad_evidence(tmp_path: Path) -> None:
    source, revision = _git_checkout(tmp_path)
    assert TrackerService._validate_source_checkout(str(source), revision) == str(source.resolve())
    (source / "dirty").write_text("drift\n", encoding="utf-8")
    with pytest.raises(TodoError):
        TrackerService._validate_source_checkout(str(source), revision)
    (source / "dirty").unlink()
    snap = _snap()
    store.op_create(
        snap, item_id="a", title="A",
        batch={"batch_id": "batch", "member_id": "a", "implementation_dependencies": []},
    )
    claim = store.op_take(snap, "a", "worker")
    with pytest.raises(TodoError):
        store.op_prepare(
            snap, "a", "worker", claim["claim"]["generation"], batch_id="batch", member_id="a",
            source_worktree=str(source), source_revision="0" * 40, verification=_verification(revision),
        )
    with pytest.raises(TodoError):
        store.op_prepare(
            snap, "a", "worker", claim["claim"]["generation"], batch_id="batch", member_id="a",
            source_worktree="relative", source_revision=revision, verification=_verification(revision),
        )


def test_prepared_finish_requires_current_final_tree_evidence(tmp_path: Path) -> None:
    source, revision = _git_checkout(tmp_path)
    snap = _snap()
    store.op_create(
        snap, item_id="a", title="A",
        batch={"batch_id": "batch", "member_id": "a", "implementation_dependencies": []},
    )
    claim = store.op_take(snap, "a", "worker")
    store.op_prepare(
        snap, "a", "worker", claim["claim"]["generation"], batch_id="batch", member_id="a",
        source_worktree=str(source), source_revision=revision, verification=_verification(revision),
    )
    claim = store.op_take(snap, "a", "worker")
    with pytest.raises(TodoError) as exc:
        store.op_finish(snap, "a", "worker", claim["claim"]["generation"])
    assert exc.value.code == "E_FINAL_EVIDENCE"
    evidence = {
        "status": "passed", "tree_worktree": str(source), "tree_revision": revision,
        "scope_digest": "a" * 64, "suite": "combined-tree", "clean": True,
    }
    store.op_finish(snap, "a", "worker", claim["claim"]["generation"], evidence)
    assert snap.index["items"]["a"]["status"] == "done"


def test_member_edit_invalidates_prepared_receipt() -> None:
    snap = _snap()
    store.op_create(
        snap, item_id="a", title="A",
        batch={"batch_id": "batch", "member_id": "a", "implementation_dependencies": []},
    )
    claim = store.op_take(snap, "a", "worker")
    revision = "a" * 40
    store.op_prepare(
        snap, "a", "worker", claim["claim"]["generation"], batch_id="batch", member_id="a",
        source_worktree="/tmp/source", source_revision=revision, verification=_verification(revision),
    )
    assert "prepared" in snap.details["a"]
    updated = store.op_update(snap, "a", title="A changed")
    assert "prepared_receipt" in updated["changed"]
    assert "prepared" not in snap.details["a"]


def test_schema_one_remains_readable_until_preparation_upgrades_it() -> None:
    snap = _snap()
    store.op_create(snap, item_id="legacy", title="Legacy")
    snap.index["schema_version"] = 1
    store.validate_snapshot(snap.index, snap.details)
    assert snap.index["schema_version"] == 1
