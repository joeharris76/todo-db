"""Service-level proving cases: budgets, paging, sections, lifecycle."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from todo_db import git_backend
from todo_db.service import MAX_BYTES, TrackerService, count_tokens


def _svc(tmp_path: Path, worker: str = "w1", name: str = "s.git") -> TrackerService:
    remote = tmp_path / name
    subprocess.run(["git", "init", "--quiet", "--bare", str(remote)], check=True)
    ref = git_backend.StateRef(remote=str(remote), branch="todo-state")
    git_backend.bootstrap(ref)
    return TrackerService(ref=ref, cache_dir=tmp_path / "cache", worker=worker)


def _size(env: dict) -> int:
    return len(json.dumps(env, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode())


def test_lifecycle_acks_are_compact(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    created = svc.create_item("t1", "First task", description="hello")
    assert created["ok"], created
    assert _size(created) <= 100 * 8  # well under the cap; ~100-token target
    took = svc.take("t1")
    assert took["ok"], took
    gen = took["data"]["claim"]["generation"]
    assert took["data"]["description_excerpt"] == "hello"
    finished = svc.finish("t1", gen)
    assert finished["ok"] and finished["data"]["status"] == "done"
    tokens, tokenizer = count_tokens(json.dumps(finished))
    assert tokenizer in ("o200k_base", "chars/4-fallback")
    assert tokens <= 100


def test_five_row_list_target(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    for i in range(7):
        assert svc.create_item(f"t{i:02d}", f"Task {i}").get("ok")
    page = svc.list_items()
    assert page["ok"], page
    assert len(page["data"]["items"]) == 5
    assert page["data"]["total"] == 7
    assert "next_cursor" in page["data"]
    tokens, _ = count_tokens(json.dumps(page))
    assert tokens <= 600  # ~300-token target with headroom for IDs
    second = svc.list_items(cursor=page["data"]["next_cursor"])
    assert second["ok"] and len(second["data"]["items"]) == 2
    assert "next_cursor" not in second["data"]


def test_stale_cursor_never_skips_silently(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    for i in range(6):
        svc.create_item(f"t{i:02d}", f"Task {i}")
    page = svc.list_items()
    svc.create_item("t-new", "Newcomer")
    stale = svc.list_items(cursor=page["data"]["next_cursor"])
    assert not stale["ok"] and stale["code"] == "E_CURSOR_STALE"


def test_cursor_bound_to_filters(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    for i in range(6):
        svc.create_item(f"t{i:02d}", f"Task {i}")
    page = svc.list_items()
    assert page["ok"]
    # Same cursor with different filters restarts instead of skipping.
    reused = svc.list_items(cursor=page["data"]["next_cursor"], text="t00")
    assert not reused["ok"] and reused["code"] == "E_CURSOR_STALE"


def test_empty_ready_queue_reports_gate(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    svc.create_item("t1", "Task one")
    empty = svc.list_items(ready_only=True)
    assert empty["ok"]  # claimable work exists
    took = svc.take("t1")
    assert took["ok"]
    idle = svc.list_items(ready_only=True)
    assert not idle["ok"] and idle["code"] == "E_NOTHING_READY"


def test_update_retry_conflicts_on_touched_fields(tmp_path: Path, monkeypatch) -> None:
    from todo_db import store as S
    from todo_db.errors import TodoError

    svc = _svc(tmp_path)
    assert svc.create_item("t", "v1")["ok"]

    def fake_mutate(ref, *, op, summary, worker, op_id, apply, max_retries=5):
        # First attempt records the pre-image; a competing writer changes
        # the same field before the retry is evaluated.
        first = git_backend.read(ref, tmp_path / "c1").snapshot
        apply(first)
        second = git_backend.read(ref, tmp_path / "c2").snapshot
        S.op_update(second, "t", title="winner")
        try:
            apply(second)
        except TodoError as exc:
            return git_backend.MutationOutcome(
                ok=False, outcome="conflict", op_id=op_id,
                code=exc.code or "E_STATE", error=str(exc),
            )
        raise AssertionError("retry should have conflicted")

    monkeypatch.setattr(git_backend, "mutate", fake_mutate)
    out = svc.update_item("t", title="loser")
    assert not out["ok"] and out["code"] == "E_CONFLICT"


def test_take_with_huge_needs_keeps_generation(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    for i in range(500):
        assert svc.create_item(f"d{i:03d}", f"dep {i}")["ok"]
    assert svc.create_item("hub", "Hub", needs=[f"d{i:03d}" for i in range(500)])["ok"]
    took = svc.take("hub")
    assert took["ok"], took
    assert took["data"]["claim"]["generation"]
    assert _size(took) <= MAX_BYTES
    shown = svc.show_item("hub")
    assert shown["ok"] and _size(shown) <= MAX_BYTES
    assert shown["data"]["needs"]["total"] == 500
    first = svc.show_item("hub", field="needs", offset=0, budget=6000)
    assert first["ok"] and first["data"]["window"]
    assert "continuation" in first["data"]
    cont = first["data"]["continuation"]
    second = svc.show_item(
        "hub", field="needs", offset=cont["offset"], budget=6000, rev=cont["rev"])
    assert second["ok"]
    stale_section = svc.show_item(
        "hub", field="needs", offset=0, budget=6000, rev="0" * 40)
    assert not stale_section["ok"] and stale_section["code"] == "E_CURSOR_STALE"


def test_boundary_take_never_strands_generation(tmp_path: Path, monkeypatch) -> None:
    import todo_db.service as svcmod

    svc = _svc(tmp_path)
    assert svc.create_item("edge", "Edge task", description="D" * 5000)["ok"]
    # Shrink the cap so the ack lands exactly on the boundary: the claim
    # must still come back with its generation.
    monkeypatch.setattr(svcmod, "MAX_BYTES", 3000)
    took = svc.take("edge")
    assert took["ok"], took
    assert took["data"]["claim"]["generation"]


def test_section_offset_requires_revision(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    assert svc.create_item("s1", "Sectioned", description="E" * 20000)["ok"]
    first = svc.show_item("s1", field="description", offset=0, budget=6000)
    assert first["ok"]
    cont = first["data"]["continuation"]
    assert cont["rev"]
    # Omitting the revision on a continuation page fails closed.
    assert not svc.show_item("s1", field="description", offset=cont["offset"])["ok"]


def test_error_envelopes_stay_bounded(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    huge = "z" * 100000
    failed = svc.create_item("ok-id", huge)
    assert not failed["ok"] and _size(failed) <= MAX_BYTES
    missing = svc.show_item("nope", field="description")
    assert not missing["ok"] and _size(missing) <= MAX_BYTES


def test_oversized_item_reports_alternate_read(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    big = "x" * 60000 + "日本語" * 1000
    assert svc.create_item("big", "Big task", description=big)["ok"]
    shown = svc.show_item("big")
    assert shown["ok"], shown
    assert _size(shown) <= MAX_BYTES
    assert "description_continuation" in shown["data"]
    first = svc.show_item("big", field="description", offset=0, budget=6000)
    assert first["ok"] and first["data"]["window"]
    assert "continuation" in first["data"]
    cont = first["data"]["continuation"]
    second = svc.show_item(
        "big", field="description", offset=cont["offset"], budget=cont["budget"], rev=cont["rev"])
    assert second["ok"]
    past_end = svc.show_item("big", field="description", offset=10**9)
    assert not past_end["ok"]
    missing = svc.show_item("big", field="nope")
    assert not missing["ok"]


def test_many_deps_and_multibyte_stay_bounded(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    for i in range(30):
        svc.create_item(f"d{i:02d}", f"dep task {i} héllo")
    svc.create_item("hub", "Hub task", needs=[f"d{i:02d}" for i in range(30)])
    shown = svc.show_item("hub")
    assert shown["ok"] and _size(shown) <= MAX_BYTES
    page = svc.list_items(limit=50)
    assert page["ok"] and _size(page) <= MAX_BYTES
    assert page["data"]["total"] == 31


def test_one_claim_per_worker(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    svc.create_item("a1", "Task A1")
    svc.create_item("a2", "Task A2")
    assert svc.take("a1")["ok"]
    second = svc.take("a2")
    assert not second["ok"] and second["code"] == "E_MULTIPLE_CLAIMS"


def test_offline_read_uses_cache_but_mutation_fails(tmp_path: Path) -> None:
    remote = tmp_path / "gone.git"
    subprocess.run(["git", "init", "--quiet", "--bare", str(remote)], check=True)
    ref = git_backend.StateRef(remote=str(remote), branch="todo-state")
    git_backend.bootstrap(ref)
    svc = TrackerService(ref=ref, cache_dir=tmp_path / "cache", worker="w1")
    assert svc.create_item("t1", "Task one")["ok"]
    assert svc.list_items()["ok"]  # populate the offline cache while online
    subprocess.run(["rm", "-rf", str(remote)], check=True)
    page = svc.list_items()
    assert page["ok"] and page["data"].get("stale") is True
    failed = svc.create_item("t2", "Task two")
    assert not failed["ok"] and failed["code"] == "E_OFFLINE"


def test_empty_page_with_remaining_items_is_unacceptable(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    for i in range(10):
        svc.create_item(f"p{i:02d}", f"Task {i}")
    offset = 0
    seen: list[str] = []
    cursor = None
    for _ in range(10):
        page = svc.list_items(limit=3, cursor=cursor) if cursor else svc.list_items(limit=3)
        assert page["ok"], page
        assert page["data"]["items"], "empty page with items remaining"
        seen.extend(item["id"] for item in page["data"]["items"])
        cursor = page["data"].get("next_cursor")
        if cursor is None:
            break
    assert len(seen) == 10
    assert offset == 0
