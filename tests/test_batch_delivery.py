"""Prepared-member delivery contract tests."""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
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


def _register(snap: store.Snapshot, revision: str, members: list[str], scope: dict[str, list[str]] | None = None) -> None:
    scope = scope or {member: [f"{member}/**"] for member in members}
    store.op_register_batch(
        snap, batch_id="batch-1", project_id="todo-db", repository="repo",
        owner="worker", owner_generation="1" * 32, integration_branch="main",
        integration_worktree="/tmp/integration", start_head=revision,
        members=members, scope=scope, scope_hash=store.scope_digest(scope),
        delivery_boundary="final-pr", terminal_outcome="merged",
    )


def test_prepare_releases_claim_without_done_and_unlocks_explicit_member(tmp_path: Path) -> None:
    source, revision = _git_checkout(tmp_path)
    snap = _snap()
    _register(snap, revision, ["a", "b"])
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
        source_base=revision,
        accepted_head=revision,
        integration_head=revision,
        scope_hash=snap.index["batches"]["batch-1"]["scope_hash"],
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
    _register(snap, revision, ["a"])
    store.op_create(
        snap, item_id="a", title="A",
        batch={"batch_id": "batch-1", "member_id": "a", "implementation_dependencies": []},
    )
    claim = store.op_take(snap, "a", "worker")
    with pytest.raises(TodoError):
        store.op_prepare(
            snap, "a", "worker", claim["claim"]["generation"], batch_id="batch-1", member_id="a",
            source_worktree=str(source), source_revision="0" * 40, verification=_verification(revision),
            source_base=revision, accepted_head=revision, integration_head=revision,
            scope_hash=snap.index["batches"]["batch-1"]["scope_hash"],
        )
    with pytest.raises(TodoError):
        store.op_prepare(
            snap, "a", "worker", claim["claim"]["generation"], batch_id="batch-1", member_id="a",
            source_worktree="relative", source_revision=revision, verification=_verification(revision),
            source_base=revision, accepted_head=revision, integration_head=revision,
            scope_hash=snap.index["batches"]["batch-1"]["scope_hash"],
        )


def test_prepared_finish_requires_current_final_tree_evidence(tmp_path: Path) -> None:
    source, revision = _git_checkout(tmp_path)
    snap = _snap()
    _register(snap, revision, ["a"])
    store.op_create(
        snap, item_id="a", title="A",
        batch={"batch_id": "batch-1", "member_id": "a", "implementation_dependencies": []},
    )
    claim = store.op_take(snap, "a", "worker")
    store.op_prepare(
        snap, "a", "worker", claim["claim"]["generation"], batch_id="batch-1", member_id="a",
        source_worktree=str(source), source_revision=revision, source_base=revision,
        accepted_head=revision, integration_head=revision,
        scope_hash=snap.index["batches"]["batch-1"]["scope_hash"], verification=_verification(revision),
    )
    claim = store.op_take(snap, "a", "worker")
    with pytest.raises(TodoError) as exc:
        store.op_finish(snap, "a", "worker", claim["claim"]["generation"])
    assert exc.value.code == "E_FINAL_EVIDENCE"
    evidence = {
        "status": "passed", "project_id": "todo-db", "repository": "repo",
        "batch_id": "batch-1", "tree_worktree": "/tmp/integration", "tree_revision": revision,
        "integration_branch": "main", "integration_head": revision,
        "scope_hash": snap.index["batches"]["batch-1"]["scope_hash"],
        "member_heads": {"a": revision}, "member_ranges": {"a": {"base": revision, "head": revision}},
        "changed_files": [], "final_pr": {"number": 1, "node_id": "PR_1", "head": revision},
        "suite": "combined-tree", "clean": True,
    }
    store.op_bind_batch_pr(snap, "batch-1", "worker", "1" * 32, number=1, node_id="PR_1", head=revision)
    store.op_finish(snap, "a", "worker", claim["claim"]["generation"], evidence)
    assert snap.index["items"]["a"]["status"] == "done"
    assert snap.index["batches"]["batch-1"]["lifecycle"] == "completed"


def test_registered_batch_member_cannot_finish_before_prepare() -> None:
    snap = _snap()
    revision = "f" * 40
    _register(snap, revision, ["a"])
    store.op_create(
        snap, item_id="a", title="A",
        batch={"batch_id": "batch-1", "member_id": "a", "implementation_dependencies": []},
    )
    claim = store.op_take(snap, "a", "worker")
    with pytest.raises(TodoError) as exc:
        store.op_finish(snap, "a", "worker", claim["claim"]["generation"])
    assert exc.value.code == "E_FINAL_EVIDENCE"
    assert snap.index["items"]["a"]["status"] == "active"
    assert snap.index["batches"]["batch-1"]["lifecycle"] == "active"


def test_owner_abort_is_claim_safe_idempotent_and_clears_prepared_work() -> None:
    snap = _snap()
    revision = "e" * 40
    _register(snap, revision, ["a"])
    store.op_create(
        snap, item_id="a", title="A",
        batch={"batch_id": "batch-1", "member_id": "a", "implementation_dependencies": []},
    )
    claim = store.op_take(snap, "a", "worker")
    with pytest.raises(TodoError) as exc:
        store.op_abort_batch(snap, "batch-1", "worker", "1" * 32)
    assert exc.value.code == "E_ACTIVE_CLAIMS"
    store.op_release(snap, "a", "worker", claim["claim"]["generation"])
    claim = store.op_take(snap, "a", "worker")
    store.op_prepare(
        snap, "a", "worker", claim["claim"]["generation"], batch_id="batch-1", member_id="a",
        source_worktree="/tmp/source", source_revision=revision, source_base=revision,
        accepted_head=revision, integration_head=revision,
        scope_hash=snap.index["batches"]["batch-1"]["scope_hash"], verification=_verification(revision),
    )
    store.op_bind_batch_pr(snap, "batch-1", "worker", "1" * 32, number=7, node_id="PR_7", head=revision)
    store.op_abort_batch(snap, "batch-1", "worker", "1" * 32)
    assert snap.index["batches"]["batch-1"]["lifecycle"] == "aborted"
    assert snap.index["items"]["a"]["status"] == "open"
    assert "batch" not in snap.details["a"]
    assert "prepared" not in snap.details["a"]
    assert snap.index["batches"]["batch-1"]["accepted_members"] == {}
    assert snap.index["batches"]["batch-1"]["integrated_members"] == {}
    assert snap.index["batches"]["batch-1"]["final_pr"] == {
        "number": 7, "node_id": "PR_7", "head": revision,
    }
    ordinary_claim = store.op_take(snap, "a", "worker")
    assert store.op_finish(snap, "a", "worker", ordinary_claim["claim"]["generation"]) == {
        "id": "a", "status": "done",
    }
    assert store.op_abort_batch(snap, "batch-1", "worker", "1" * 32)["idempotent"] is True


def test_abort_expired_active_member_returns_it_to_open() -> None:
    snap = _snap()
    revision = "c" * 40
    _register(snap, revision, ["a"])
    store.op_create(
        snap, item_id="a", title="A",
        batch={"batch_id": "batch-1", "member_id": "a", "implementation_dependencies": []},
    )
    store.op_take(snap, "a", "worker", now=datetime(2020, 1, 1, tzinfo=timezone.utc))
    assert snap.index["items"]["a"]["status"] == "active"
    store.op_abort_batch(snap, "batch-1", "worker", "1" * 32)
    assert snap.index["items"]["a"]["status"] == "open"
    assert snap.index["items"]["a"]["claim"] is None
    assert "batch" not in snap.details["a"]


def test_abort_preserves_dropped_member_state() -> None:
    snap = _snap()
    revision = "b" * 40
    _register(snap, revision, ["a"])
    store.op_create(
        snap, item_id="a", title="A",
        batch={"batch_id": "batch-1", "member_id": "a", "implementation_dependencies": []},
    )
    store.op_drop(snap, "a")
    store.op_abort_batch(snap, "batch-1", "worker", "1" * 32)
    assert snap.index["items"]["a"]["status"] == "dropped"
    assert "batch" not in snap.details["a"]


def test_abort_refuses_done_members_without_reopening_them() -> None:
    snap = _snap()
    revision = "d" * 40
    _register(snap, revision, ["a", "b"])
    for item_id in ("a", "b"):
        store.op_create(
            snap, item_id=item_id, title=item_id.upper(),
            batch={"batch_id": "batch-1", "member_id": item_id, "implementation_dependencies": []},
        )
    snap.index["items"]["a"]["status"] = "done"
    with pytest.raises(TodoError) as exc:
        store.op_abort_batch(snap, "batch-1", "worker", "1" * 32)
    assert exc.value.code == "E_STATE"
    assert snap.index["items"]["a"]["status"] == "done"
    assert snap.index["items"]["b"]["status"] == "open"
    assert snap.index["batches"]["batch-1"]["lifecycle"] == "active"


def test_register_failure_restores_legacy_schema_version() -> None:
    snap = _snap()
    snap.index["schema_version"] = 1
    with pytest.raises(TodoError):
        store.op_register_batch(
            snap, batch_id="batch-1", project_id="todo-db", repository="repo", owner="worker",
            owner_generation="1" * 32, integration_branch="main", integration_worktree="/tmp/integration",
            start_head="f" * 40, members=["a"], scope={"a": []}, scope_hash="0" * 64,
            delivery_boundary="final-pr", terminal_outcome="merged",
        )
    assert snap.index["schema_version"] == 1


def test_member_edit_invalidates_prepared_receipt() -> None:
    snap = _snap()
    revision = "a" * 40
    _register(snap, revision, ["a"])
    store.op_create(
        snap, item_id="a", title="A",
        batch={"batch_id": "batch-1", "member_id": "a", "implementation_dependencies": []},
    )
    claim = store.op_take(snap, "a", "worker")
    store.op_prepare(
        snap, "a", "worker", claim["claim"]["generation"], batch_id="batch-1", member_id="a",
        source_worktree="/tmp/source", source_revision=revision, source_base=revision,
        accepted_head=revision, integration_head=revision,
        scope_hash=snap.index["batches"]["batch-1"]["scope_hash"], verification=_verification(revision),
    )
    assert "prepared" in snap.details["a"]
    updated = store.op_update(snap, "a", title="A changed")
    assert "prepared_receipt" in updated["changed"]
    assert "prepared" not in snap.details["a"]


def test_member_edit_invalidates_transitive_downstream_receipts() -> None:
    snap = _snap()
    revision = "b" * 40
    _register(snap, revision, ["a", "b", "c"])
    for item_id, needs, deps in (("a", [], []), ("b", ["a"], ["a"]), ("c", ["b"], ["b"])):
        store.op_create(
            snap, item_id=item_id, title=item_id.upper(), needs=needs,
            batch={"batch_id": "batch-1", "member_id": item_id, "implementation_dependencies": deps},
        )
    for item_id, deps in (("a", []), ("b", ["a"]), ("c", ["b"])):
        claim = store.op_take(snap, item_id, "worker")
        store.op_prepare(
            snap, item_id, "worker", claim["claim"]["generation"], batch_id="batch-1", member_id=item_id,
            source_worktree="/tmp/source", source_revision=revision, source_base=revision,
            accepted_head=revision, integration_head=revision,
            scope_hash=snap.index["batches"]["batch-1"]["scope_hash"], verification=_verification(revision),
            implementation_dependencies=deps,
        )
    updated = store.op_update(snap, "a", title="A changed")
    assert updated["invalidated"] == ["b", "c"]
    assert all("prepared" not in snap.details[item_id] for item_id in ("a", "b", "c"))


def test_distinct_integrated_heads_keep_multiple_predecessors_ready() -> None:
    snap = _snap()
    base, ahead, bhead, merge_head = ("a" * 40, "b" * 40, "c" * 40, "d" * 40)
    _register(snap, base, ["a", "b", "c"], {"a": ["a/**"], "b": ["b/**"], "c": ["c/**"]})
    for item_id, needs, deps in (("a", [], []), ("b", ["a"], ["a"]), ("c", ["a", "b"], ["a", "b"])):
        store.op_create(
            snap, item_id=item_id, title=item_id.upper(), needs=needs,
            batch={"batch_id": "batch-1", "member_id": item_id, "implementation_dependencies": deps},
        )
    claim = store.op_take(snap, "a", "worker")
    store.op_prepare(
        snap, "a", "worker", claim["claim"]["generation"], batch_id="batch-1", member_id="a",
        source_worktree="/tmp/source", source_revision=ahead, source_base=base,
        accepted_head=ahead, integration_head=ahead, scope_hash=snap.index["batches"]["batch-1"]["scope_hash"],
        changed_files=["a/file"], verification=_verification(ahead),
    )
    claim = store.op_take(snap, "b", "worker")
    store.op_prepare(
        snap, "b", "worker", claim["claim"]["generation"], batch_id="batch-1", member_id="b",
        source_worktree="/tmp/source", source_revision=bhead, source_base=ahead,
        accepted_head=bhead, integration_head=merge_head, scope_hash=snap.index["batches"]["batch-1"]["scope_hash"],
        changed_files=["b/file"], verification=_verification(bhead),
        implementation_dependencies=["a"],
    )
    assert store.is_ready("c", snap) is True


def test_schema_one_remains_readable_until_preparation_upgrades_it() -> None:
    snap = _snap()
    store.op_create(snap, item_id="legacy", title="Legacy")
    snap.index["schema_version"] = 1
    store.validate_snapshot(snap.index, snap.details)
    assert snap.index["schema_version"] == 1


def test_duplicate_batch_registration_and_foreign_member_fail_closed() -> None:
    snap = _snap()
    revision = "c" * 40
    _register(snap, revision, ["a"])
    with pytest.raises(TodoError):
        _register(snap, revision, ["a"])
    with pytest.raises(TodoError):
        store.op_create(
            snap, item_id="foreign", title="Foreign",
            batch={"batch_id": "batch-1", "member_id": "z", "implementation_dependencies": []},
        )


def test_real_git_scope_and_repository_identity_checks(tmp_path: Path) -> None:
    source, base = _git_checkout(tmp_path)
    subprocess.run(["git", "-C", str(source), "remote", "add", "origin", "https://example.invalid/todo-db.git"], check=True)
    (source / "a").mkdir()
    (source / "a" / "ok.txt").write_text("ok\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "add", "a/ok.txt"], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "--quiet", "-m", "member"], check=True)
    head = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    assert TrackerService._validate_source_checkout(
        str(source), head, expected_repository="https://example.invalid/todo-db.git"
    ) == str(source.resolve())
    with pytest.raises(TodoError):
        TrackerService._validate_source_checkout(str(source), head, expected_repository="https://example.invalid/other.git")
    assert TrackerService._changed_paths(str(source), base, head) == ["a/ok.txt"]


def test_runtime_readiness_fails_closed_when_integration_checkout_moves(tmp_path: Path) -> None:
    source, revision = _git_checkout(tmp_path)
    snap = _snap()
    _register(snap, revision, ["a", "b"], {"a": ["a/**"], "b": ["b/**"]})
    snap.index["batches"]["batch-1"]["integration_worktree"] = str(source)
    snap.index["batches"]["batch-1"]["repository"] = str(source)
    store.op_create(
        snap, item_id="a", title="A",
        batch={"batch_id": "batch-1", "member_id": "a", "implementation_dependencies": []},
    )
    store.op_create(
        snap, item_id="b", title="B", needs=["a"],
        batch={"batch_id": "batch-1", "member_id": "b", "implementation_dependencies": ["a"]},
    )
    claim = store.op_take(snap, "a", "worker")
    store.op_prepare(
        snap, "a", "worker", claim["claim"]["generation"], batch_id="batch-1", member_id="a",
        source_worktree=str(source), source_revision=revision, source_base=revision,
        accepted_head=revision, integration_head=revision,
        scope_hash=snap.index["batches"]["batch-1"]["scope_hash"], verification=_verification(revision),
    )
    service = TrackerService(ref=None, cache_dir=tmp_path, worker="worker")  # type: ignore[arg-type]
    assert service._batch_runtime_ready(snap, "b") is True
    moved = tmp_path / "moved"
    source.rename(moved)
    assert service._batch_runtime_ready(snap, "b") is False


def test_real_git_merge_head_keeps_cumulative_prepared_readiness(tmp_path: Path) -> None:
    repo, base = _git_checkout(tmp_path)
    def run(*args: str) -> None:
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    run("checkout", "-b", "worker-a")
    (repo / "a").mkdir()
    (repo / "a" / "file").write_text("A\n", encoding="utf-8")
    run("add", "a/file")
    run("commit", "--quiet", "-m", "A")
    ahead = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    run("checkout", "main")
    (repo / "integration").write_text("I\n", encoding="utf-8")
    run("add", "integration")
    run("commit", "--quiet", "-m", "integration base")
    run("merge", "--no-ff", "worker-a", "-m", "merge A")
    merge_a = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    run("checkout", "-b", "worker-b")
    (repo / "b").mkdir()
    (repo / "b" / "file").write_text("B\n", encoding="utf-8")
    run("add", "b/file")
    run("commit", "--quiet", "-m", "B")
    bhead = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    run("checkout", "main")
    (repo / "integration2").write_text("I2\n", encoding="utf-8")
    run("add", "integration2")
    run("commit", "--quiet", "-m", "integration second")
    run("merge", "--no-ff", "worker-b", "-m", "merge B")
    merge_b = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()

    snap = _snap()
    _register(snap, base, ["a", "b", "c"], {"a": ["a/**"], "b": ["b/**"], "c": ["c/**"]})
    batch = snap.index["batches"]["batch-1"]
    batch["integration_worktree"] = str(repo)
    batch["repository"] = str(repo)
    for item_id, needs, deps in (("a", [], []), ("b", ["a"], ["a"]), ("c", ["a", "b"], ["a", "b"])):
        store.op_create(snap, item_id=item_id, title=item_id.upper(), needs=needs,
                        batch={"batch_id": "batch-1", "member_id": item_id, "implementation_dependencies": deps})
    claim = store.op_take(snap, "a", "worker")
    store.op_prepare(snap, "a", "worker", claim["claim"]["generation"], batch_id="batch-1", member_id="a",
                     source_worktree=str(repo), source_revision=ahead, source_base=base,
                     accepted_head=ahead, integration_head=merge_a, scope_hash=batch["scope_hash"],
                     changed_files=["a/file"], verification=_verification(ahead))
    claim = store.op_take(snap, "b", "worker")
    store.op_prepare(snap, "b", "worker", claim["claim"]["generation"], batch_id="batch-1", member_id="b",
                     source_worktree=str(repo), source_revision=bhead, source_base=merge_a,
                     accepted_head=bhead, integration_head=merge_b, scope_hash=batch["scope_hash"],
                     changed_files=["b/file"], verification=_verification(bhead), implementation_dependencies=["a"])
    service = TrackerService(ref=None, cache_dir=tmp_path, worker="worker")  # type: ignore[arg-type]
    assert store.is_ready("c", snap) is True
    assert service._batch_runtime_ready(snap, "c") is True

    run("checkout", "main")
    (repo / "a" / "file").write_text("rewritten\n", encoding="utf-8")
    run("add", "a/file")
    run("commit", "--quiet", "-m", "rewrite A")
    assert service._batch_runtime_ready(snap, "c") is False
