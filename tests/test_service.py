"""Service-level proving cases: budgets, paging, sections, lifecycle."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from todo_db import git_backend, service as Svc
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
    second = svc.show_item("big", field="description", offset=cont["offset"], budget=cont["budget"])
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
