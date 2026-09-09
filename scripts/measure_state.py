"""Measure token budgets, latency, and Git growth for the JSON/Git tracker.

Produces the numbers behind docs/measurements.md. All fixtures are
disposable local bare remotes. Seeding writes one commit per scale
(seeding is not the publication path under test); publication latency is
measured separately on sampled single-operation publishes.

Usage:
    uv run python scripts/measure_state.py [--scale 100 10000]
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from todo_db import count_tokens, git_backend  # noqa: E402
from todo_db import store as S  # noqa: E402
from todo_db.git_backend import StateRef  # noqa: E402
from todo_db.mcp.instructions import INSTRUCTIONS  # noqa: E402
from todo_db.service import TrackerService  # noqa: E402


def _snapshots_tools_schema_bytes() -> int:
    snap = json.loads(Path("scripts/mcp_snapshots/tools.json").read_text())
    return len(json.dumps(snap).encode())


def _du_bytes(path: Path) -> int:
    total = 0
    for root, _, files in __import__("os").walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total


def seed(ref: StateRef, n: int, link_every: int = 0) -> float:
    """Write one commit with *n* items. Returns wall seconds."""
    tmp = Path(tempfile.mkdtemp(prefix="seed-"))
    started = time.perf_counter()
    try:
        subprocess.run(["git", "clone", "--quiet", ref.remote, str(tmp / "w")], check=True)
        work = tmp / "w"
        subprocess.run(["git", "-C", str(work), "config", "user.name", "seed"], check=True)
        subprocess.run(["git", "-C", str(work), "config", "user.email", "seed@localhost"], check=True)
        subprocess.run(["git", "-C", str(work), "fetch", "--quiet", "origin", ref.branch], check=True)
        tip = subprocess.run(
            ["git", "-C", str(work), "rev-parse", "FETCH_HEAD"],
            check=True, capture_output=True, text=True).stdout.strip()
        subprocess.run(["git", "-C", str(work), "checkout", "--quiet", tip], check=True)
        snap = S.load_snapshot(work)
        for i in range(n):
            needs = [f"t{i - link_every:05d}"] if link_every and i >= link_every else []
            S.op_create(
                snap, item_id=f"t{i:05d}", title=f"Seeded task {i}",
                priority=("critical" if i % 10 == 0 else "medium"),
                description=f"Description for task {i}." if i % 3 == 0 else "",
                needs=needs,
            )
        S.save_snapshot(work, snap)
        subprocess.run(["git", "-C", str(work), "add", "--", "index.json", "items"], check=True)
        subprocess.run(["git", "-C", str(work), "commit", "--quiet", "-m",
                        f"todo(seed): {n} items\n\nTodo-Op-Id: seed{n}\n"], check=True)
        subprocess.run(["git", "-C", str(work), "push", "--quiet", "origin", f"HEAD:{ref.branch}"],
                       check=True)
        return time.perf_counter() - started
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def publish_sample(ref: StateRef, cache: Path, worker: str, item_id: str, title: str) -> dict[str, float]:
    svc = TrackerService(ref=ref, cache_dir=cache, worker=worker)
    marks: dict[str, float] = {}
    t0 = time.perf_counter()
    assert svc.create_item(item_id, title)["ok"]
    marks["create"] = time.perf_counter() - t0
    t0 = time.perf_counter()
    took = svc.take(item_id)
    assert took["ok"], took
    marks["take"] = time.perf_counter() - t0
    gen = took["data"]["claim"]["generation"]
    t0 = time.perf_counter()
    assert svc.finish(item_id, gen)["ok"]
    marks["finish"] = time.perf_counter() - t0
    return marks


def measure_scale(n: int) -> dict:
    root = Path(tempfile.mkdtemp(prefix="measure-"))
    remote = root / "state.git"
    subprocess.run(["git", "init", "--quiet", "--bare", str(remote)], check=True)
    ref = StateRef(remote=str(remote), branch="todo-state")
    git_backend.bootstrap(ref)
    before = _du_bytes(remote)
    seed_s = seed(ref, n, link_every=7 if n >= 100 else 0)
    after_seed = _du_bytes(remote)
    cache = root / "cache"
    svc = TrackerService(ref=ref, cache_dir=cache, worker="measure")

    t0 = time.perf_counter()
    page = svc.list_items()
    list_s = time.perf_counter() - t0
    assert page["ok"], page
    list_blob = json.dumps(page)
    list_tokens, tokenizer = count_tokens(list_blob)

    t0 = time.perf_counter()
    shown = svc.show_item("t00001")
    show_s = time.perf_counter() - t0
    assert shown["ok"], shown
    show_tokens, _ = count_tokens(json.dumps(shown))

    pub = publish_sample(ref, cache, "measure", "probe-1", "Probe task")
    after_pub = _du_bytes(remote)

    # Take-context and ack tokens on a small dedicated fixture.
    svc2 = TrackerService(ref=ref, cache_dir=root / "cache2", worker="m2")
    svc2.create_item("m-take", "Take fixture", description="Work description here.")
    take_tokens, _ = count_tokens(json.dumps(svc2.take("m-take")))

    result = {
        "items": n,
        "seed_commit_s": round(seed_s, 2),
        "list_page_s": round(list_s, 3),
        "show_item_s": round(show_s, 3),
        "publish_create_s": round(pub["create"], 3),
        "publish_take_s": round(pub["take"], 3),
        "publish_finish_s": round(pub["finish"], 3),
        "list_5row_tokens": list_tokens,
        "show_1item_tokens": show_tokens,
        "take_context_tokens": take_tokens,
        "tokenizer": tokenizer,
        "git_bytes_before": before,
        "git_bytes_after_seed": after_seed,
        "git_bytes_after_3_publishes": after_pub,
        "git_bytes_per_seed_item": round((after_seed - before) / max(n, 1), 1),
    }
    shutil.rmtree(root, ignore_errors=True)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scale", nargs="+", type=int, default=[100, 10000])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    schema_tokens, tokenizer = count_tokens(
        Path("scripts/mcp_snapshots/tools.json").read_text() + INSTRUCTIONS)
    out = {
        "tokenizer": tokenizer,
        "startup_schemas_plus_instructions_tokens": schema_tokens,
        "scales": [measure_scale(n) for n in args.scale],
    }
    if args.json:
        print(json.dumps(out, indent=2, sort_keys=True))
    else:
        print(f"tokenizer: {tokenizer}")
        print(f"startup schemas+instructions: {schema_tokens} tokens")
        for scale in out["scales"]:
            print(f"--- {scale['items']} items ---")
            for key, value in scale.items():
                if key != "items":
                    print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
