"""The persistent Git object store behind reads, writes and recovery."""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from todo_db import git_backend
from todo_db import git_store
from todo_db import store as S
from todo_db.errors import TodoError

WORDS = (
    "state branch item claim worker session needs ready blocked open done review "
    "plan test commit push fetch tree index detail cache store snapshot deferral "
    "batch member prepared evidence scope generation priority medium high low"
).split()


def _git(cwd: Path, *args: str, **kw) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, **kw).stdout.strip()


def _remote(tmp_path: Path) -> git_backend.StateRef:
    remote = tmp_path / "state.git"
    subprocess.run(["git", "init", "--quiet", "--bare", str(remote)], check=True)
    return git_backend.StateRef(remote=str(remote), branch="todo-state")


def _prose(rng: random.Random, words: int) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(words))


def _objects_bytes(store: git_store.Store) -> int:
    root = store.git_dir / "objects"
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())


Files = dict[str, "str | tuple[str, str]"]


def _worktree_commit(tmp_path: Path, files: Files, *, mode_exec: frozenset[str] = frozenset()) -> tuple[Path, str]:
    """Commit raw files on a fresh branch repo; ("link", target) makes a symlink."""
    work = tmp_path / "src"
    subprocess.run(["git", "init", "--quiet", "-b", "todo-state", str(work)], check=True)
    _git(work, "config", "user.name", "t")
    _git(work, "config", "user.email", "t@t")
    for rel, body in files.items():
        path = work / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(body, tuple):
            path.symlink_to(body[1])
        else:
            path.write_text(body, encoding="utf-8")
        if rel in mode_exec:
            path.chmod(0o755)
    _git(work, "add", "-A")
    _git(work, "commit", "--quiet", "-m", "fixture")
    return work, _git(work, "rev-parse", "HEAD")


def _fetched_store(tmp_path: Path, url: Path, branch: str = "todo-state") -> tuple[git_store.Store, str]:
    store = git_store.Store(tmp_path / "ns", git_store.run_git)
    store.open()
    with store.incoming(str(url), branch) as (proc, tip):
        assert proc.returncode == 0, proc.stderr
        store.advance_tip(tip)
    return store, tip


def _index(*ids: str) -> tuple[str, dict[str, str]]:
    snap = S.Snapshot(index=S.empty_index(), details={})
    for item_id in ids:
        S.op_create(snap, item_id=item_id, title=item_id)
    return S.serialize_state(snap.index), {i: S.serialize_state(snap.details[i]) for i in ids}


def _valid_files(*ids: str) -> Files:
    index, details = _index(*ids)
    return {"index.json": index, **{f"items/{i}.json": body for i, body in details.items()}}


def _parity_cases() -> dict[str, Files]:
    base = _valid_files("a", "b")
    return {
        "valid": base,
        "empty-items": {"index.json": _index()[0]},
        "missing-detail": {k: v for k, v in base.items() if k != "items/b.json"},
        "extra-detail": {**base, "items/zzz.json": json.dumps({"id": "zzz"})},
        "dot-file": {**base, "items/.hidden.json": "{}"},
        "nested-ignored": {**base, "items/sub/deep.json": "not json"},
        "non-json-ignored": {**base, "items/notes.txt": "hello"},
        "symlinked-index": {**base, "real.json": base["index.json"], "index.json": ("link", "real.json")},
        "symlinked-detail": {**base, "real.json": base["items/b.json"], "items/b.json": ("link", "../real.json")},
        "invalid-json": {**base, "items/a.json": "{not json"},
        "missing-index": {k: v for k, v in base.items() if k != "index.json"},
    }


@pytest.mark.parametrize("case", sorted(_parity_cases()))
def test_store_loader_matches_checkout_loader(tmp_path: Path, case: str) -> None:
    files = _parity_cases()[case]
    work, sha = _worktree_commit(tmp_path, files)
    store, tip = _fetched_store(tmp_path, work)
    assert tip == sha

    def outcome(load):
        try:
            snap = load()
            return ("ok", snap.index, snap.details)
        except TodoError as exc:
            return ("error", exc.code)

    assert outcome(lambda: store.load(sha)[0]) == outcome(lambda: S.load_snapshot(work))


def test_store_loader_refuses_symlinked_items_directory(tmp_path: Path) -> None:
    index, details = _index("a")
    work = tmp_path / "src"
    subprocess.run(["git", "init", "--quiet", "-b", "todo-state", str(work)], check=True)
    _git(work, "config", "user.name", "t")
    _git(work, "config", "user.email", "t@t")
    (work / "index.json").write_text(index)
    (work / "real").mkdir()
    (work / "real" / "a.json").write_text(details["a"])
    (work / "items").symlink_to("real")
    _git(work, "add", "-A")
    _git(work, "commit", "--quiet", "-m", "fixture")
    store, tip = _fetched_store(tmp_path, work)
    with pytest.raises(TodoError, match="symlinked items directory"):
        store.load(tip)
    with pytest.raises(TodoError, match="symlinked items directory"):
        S.load_snapshot(work)


def _porcelain_commit(tmp_path: Path, url: Path, apply, message: str) -> tuple[str, str]:
    """The pre-store write path: checkout, rewrite every file, git add, git commit -m."""
    work = tmp_path / "porcelain"
    subprocess.run(["git", "init", "--quiet", "-b", "main", str(work)], check=True)
    _git(work, "config", "user.name", "todo-db")
    _git(work, "config", "user.email", "todo-db@localhost")
    _git(work, "fetch", "--quiet", str(url), "todo-state")
    _git(work, "checkout", "--quiet", "--detach", "FETCH_HEAD")
    snap = S.load_snapshot(work)
    apply(snap)
    S.save_snapshot(work, snap)
    _git(work, "add", "--", "index.json", "items")
    _git(work, "commit", "--quiet", "-m", message)
    body = subprocess.run(["git", "cat-file", "commit", "HEAD"], cwd=work, check=True,
                          capture_output=True, text=True).stdout
    return _git(work, "rev-parse", "HEAD^{tree}"), body.split("\n\n", 1)[1]


def test_plumbing_commit_matches_porcelain_commit(tmp_path: Path) -> None:
    files = _valid_files("a", "b", "c")
    # A non-canonical blob and an executable bit: the porcelain path
    # normalizes both by rewriting every file, so the store must too.
    files["items/c.json"] = json.dumps(json.loads(files["items/c.json"]))
    work, sha = _worktree_commit(tmp_path, files, mode_exec={"items/a.json"})

    def apply(snap: S.Snapshot) -> None:
        S.op_update(snap, "a", title="renamed")
        del snap.index["items"]["b"]
        del snap.details["b"]
        S.op_create(snap, item_id="d", title="new one")

    message = git_backend._commit_message(
        "update", "line one  \n\n\nline two\t", "a" * 32, "worker", "session-1",
    )
    want_tree, want_body = _porcelain_commit(tmp_path, work, apply, message)

    store, tip = _fetched_store(tmp_path, work)
    snap, current = store.load(tip)
    apply(snap)
    tree = store.write_tree(tip, snap, current)
    commit = store.commit(tree, tip, message)
    body = store.git(["cat-file", "commit", commit]).stdout.split("\n\n", 1)[1]
    assert tree == want_tree
    assert body == want_body
    assert "Todo-Op-Id: " + "a" * 32 in body


def test_cleanup_message_matches_git_stripspace() -> None:
    samples = ["", "\n\n", "a", "a\n", "  a  \n\n\n\nb \t\n\n", "\nx\n\n\ny\n\n", "t\r\nu\r\n"]
    for sample in samples:
        want = subprocess.run(["git", "stripspace"], input=sample, capture_output=True, text=True, check=True).stdout
        assert git_store.cleanup_message(sample) == want, repr(sample)


def test_tip_never_moves_backwards_but_follows_a_rewrite(tmp_path: Path) -> None:
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    first = git_backend.mutate(ref, op="create", summary="a", worker="w",
                               apply=lambda s: (S.op_create(s, item_id="a", title="A"), {})[1])
    second = git_backend.mutate(ref, op="create", summary="b", worker="w",
                                apply=lambda s: (S.op_create(s, item_id="b", title="B"), {})[1])
    store = git_backend._store(ref, None)
    assert store.tip() == second.sha
    store.advance_tip(first.sha)
    assert store.tip() == second.sha
    # A rewritten branch (not a descendant) still replaces the tip.
    orphan = store._ok(["commit-tree", store.tree_of(first.sha)], input="rewrite\n",
                       env=git_store.COMMIT_IDENTITY).strip()
    store.advance_tip(orphan)
    assert store.tip() == orphan


def test_read_follows_a_force_pushed_branch(tmp_path: Path) -> None:
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    base = git_backend.ls_remote_tip(ref)
    git_backend.mutate(ref, op="create", summary="a", worker="w",
                       apply=lambda s: (S.op_create(s, item_id="a", title="A"), {})[1])
    cache = tmp_path / "cache"
    assert "a" in git_backend.read(ref, cache).snapshot.index["items"]
    newer = git_backend._store(ref, cache).tip()
    # A rewind looks exactly like a slow process reporting an older tip, so
    # the stored tip keeps the newer commit; the read still serves the remote.
    subprocess.run(["git", "-C", ref.remote, "update-ref", "refs/heads/todo-state", base], check=True)
    ro = git_backend.read(ref, cache)
    assert ro.rev == base and "a" not in ro.snapshot.index["items"]
    assert git_backend._store(ref, cache).tip() == newer
    # A divergent rewrite is followed: the remote is authoritative.
    work = tmp_path / "rewrite"
    subprocess.run(["git", "init", "--quiet", "-b", "main", str(work)], check=True)
    _git(work, "config", "user.name", "t")
    _git(work, "config", "user.email", "t@t")
    _git(work, "fetch", "--quiet", ref.remote, "todo-state")
    _git(work, "checkout", "--quiet", "--detach", "FETCH_HEAD")
    snap = S.load_snapshot(work)
    S.op_create(snap, item_id="b", title="B")
    S.save_snapshot(work, snap)
    _git(work, "add", "--", "index.json", "items")
    _git(work, "commit", "--quiet", "-m", "divergent")
    _git(work, "push", "--quiet", "--force", ref.remote, "HEAD:todo-state")
    ro = git_backend.read(ref, cache)
    assert "b" in ro.snapshot.index["items"] and "a" not in ro.snapshot.index["items"]
    assert git_backend._store(ref, cache).tip() == ro.rev


def test_size_per_interaction_is_within_twice_the_items_touched(tmp_path: Path) -> None:
    """Retained store growth per one-item commit, after maintenance, is <= 2x that item."""
    rng = random.Random(7)
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    cache = tmp_path / "cache"
    ids = [f"item-{n:03d}" for n in range(300)]

    def seed(snap: S.Snapshot) -> dict:
        for item_id in ids:
            S.op_create(snap, item_id=item_id, title=_prose(rng, 8)[:200], description=_prose(rng, 500))
        return {}

    assert git_backend.mutate(ref, op="seed", summary="seed", worker="w", apply=seed, cache_dir=cache).ok
    git_backend.read(ref, cache)
    store = git_backend._store(ref, cache)
    store.maintain()
    before = _objects_bytes(store)

    touched = 0
    for n in range(50):
        item_id = ids[rng.randrange(len(ids))]
        sentence = _prose(rng, 12)

        def edit(snap: S.Snapshot, item_id=item_id, sentence=sentence) -> dict:
            detail = snap.details[item_id]
            S.op_update(snap, item_id, description=detail.get("description", "") + " " + sentence)
            return {}

        out = git_backend.mutate(ref, op="update", summary=f"edit {n}", worker="w", apply=edit, cache_dir=cache)
        assert out.ok, out.error
        ro = git_backend.read(ref, cache)
        touched += len(S.serialize_state(ro.snapshot.details[item_id]).encode())
        assert store.slack() <= git_store.SLACK_CAP_BYTES
    store.maintain()
    growth = _objects_bytes(store) - before
    print(f"store growth {growth} B for {touched} B of items touched ({growth / touched:.2f}x)")
    assert growth <= 2 * touched, (growth, touched)


def test_maintenance_starts_once_slack_passes_the_cap(tmp_path: Path, monkeypatch) -> None:
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    cache = tmp_path / "cache"
    git_backend.read(ref, cache)
    store = git_backend._store(ref, cache)
    calls: list[int] = []
    monkeypatch.setattr(git_store.Store, "maintain", lambda self: calls.append(1) or True)
    monkeypatch.setattr(git_store, "SLACK_CAP_BYTES", 10**12)
    store.maybe_maintain()
    assert calls == []
    monkeypatch.setattr(git_store, "SLACK_CAP_BYTES", -1)
    store.maybe_maintain()
    assert calls == [1]


def test_maintenance_is_skipped_while_another_process_runs_it(tmp_path: Path) -> None:
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    cache = tmp_path / "cache"
    git_backend.read(ref, cache)
    store = git_backend._store(ref, cache)
    import fcntl

    fd = os.open(store.ns / git_store.MAINTENANCE_LOCK_NAME, os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        assert store.maintain() is False
    finally:
        os.close(fd)
    assert store.maintain() is True


def test_damaged_store_is_rebuilt_on_read(tmp_path: Path, monkeypatch) -> None:
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    git_backend.mutate(ref, op="create", summary="a", worker="w",
                       apply=lambda s: (S.op_create(s, item_id="a", title="A"), {})[1])
    cache = tmp_path / "cache"
    git_backend.read(ref, cache)
    store = git_backend._store(ref, cache)
    old_id = store.store_id()
    for pack in (store.git_dir / "objects").rglob("*"):
        if pack.is_file() and pack.parent.name != "info":
            pack.unlink()
    ro = git_backend.read(ref, cache)
    # Missing objects are simply fetched again.
    assert "a" in ro.snapshot.index["items"] and ro.stale is False

    # A store that fails locally is moved aside and rebuilt once.
    real_load = git_store.Store.load
    calls = {"n": 0}

    def failing_once(self, rev):
        calls["n"] += 1
        if calls["n"] == 1:
            raise git_store.StoreError("simulated damage")
        return real_load(self, rev)

    monkeypatch.setattr(git_store.Store, "load", failing_once)
    ro = git_backend.read(ref, cache)
    assert "a" in ro.snapshot.index["items"] and calls["n"] == 2
    assert store.store_id() != old_id


def test_offline_read_serves_the_store_tip(tmp_path: Path) -> None:
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    out = git_backend.mutate(ref, op="create", summary="a", worker="w",
                             apply=lambda s: (S.op_create(s, item_id="a", title="A"), {})[1])
    cache = tmp_path / "cache"
    git_backend.read(ref, cache)
    git_backend._store(ref, cache).maintain()
    subprocess.run(["rm", "-rf", ref.remote], check=True)
    ro = git_backend.read(ref, cache)
    assert ro.stale is True and ro.rev == out.sha and "a" in ro.snapshot.index["items"]


def test_offline_read_falls_back_to_a_legacy_cached_revision(tmp_path: Path, monkeypatch) -> None:
    ref = git_backend.StateRef(remote="https://example.invalid/state.git", branch="todo-state")
    cache = tmp_path / "cache"
    legacy = git_backend._ns_dir(cache, ref)
    snap = S.Snapshot(index=S.empty_index(), details={})
    S.op_create(snap, item_id="legacy", title="Legacy")
    S.save_snapshot(legacy / "revs" / ("c" * 40), snap)
    (legacy / "last").write_text("c" * 40 + "\n")

    def offline(r):
        raise TodoError("simulated outage", code="E_OFFLINE")

    monkeypatch.setattr(git_backend, "ls_remote_tip", offline)
    ro = git_backend.read(ref, cache)
    assert ro.stale is True and ro.rev == "c" * 40 and "legacy" in ro.snapshot.index["items"]


def test_reads_never_write_snapshot_files(tmp_path: Path) -> None:
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    cache = tmp_path / "cache"
    git_backend.read(ref, cache)
    assert not list(cache.rglob("revs")) and not list(cache.rglob("last"))


def test_inherited_git_environment_and_global_hooks_are_ignored(tmp_path: Path, monkeypatch) -> None:
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    for name in ("reference-transaction", "pre-push", "post-commit"):
        hook = hooks / name
        # Fail only inside the cache: with a local-path remote the same
        # global hooks also run in the remote's receive-pack.
        hook.write_text('#!/bin/sh\ncase "$(pwd)" in *default-cache*) exit 1;; esac\nexit 0\n')
        hook.chmod(0o755)
    global_config = tmp_path / "gitconfig"
    global_config.write_text(f"[core]\n\thooksPath = {hooks}\n[commit]\n\tgpgSign = true\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "elsewhere-index"))
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Someone Else")
    out = git_backend.mutate(ref, op="create", summary="a", worker="w",
                             apply=lambda s: (S.op_create(s, item_id="a", title="A"), {})[1])
    assert out.ok, out.error
    author = subprocess.run(["git", "-C", ref.remote, "log", "-1", "--format=%an <%ae>", "todo-state"],
                            env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
                            capture_output=True, text=True, check=True).stdout.strip()
    assert author == "todo-db <todo-db@localhost>"


def test_local_relative_remotes_get_their_own_store(tmp_path: Path, monkeypatch) -> None:
    for name in ("one", "two"):
        (tmp_path / name).mkdir()
        subprocess.run(["git", "init", "--quiet", "--bare", str(tmp_path / name / "state.git")], check=True)
    rel = git_backend.StateRef(remote="state.git", branch="todo-state")
    monkeypatch.chdir(tmp_path / "one")
    one = git_backend._store(rel, tmp_path / "cache").ns
    monkeypatch.chdir(tmp_path / "two")
    two = git_backend._store(rel, tmp_path / "cache").ns
    assert one != two


WORKER = textwrap.dedent("""
    import json, sys
    from pathlib import Path
    from todo_db import git_backend, store as S
    remote, cache, name, count = sys.argv[1], Path(sys.argv[2]), sys.argv[3], int(sys.argv[4])
    ref = git_backend.StateRef(remote=remote, branch="todo-state")
    errors, created = [], []
    for n in range(count):
        item_id = f"{name}-{n}"
        try:
            out = git_backend.mutate(
                ref, op="create", summary=item_id, worker=name, cache_dir=cache,
                apply=lambda s: (S.op_create(s, item_id=item_id, title=item_id), {})[1],
            )
            if out.ok:
                created.append(item_id)
            elif out.code != "E_CONFLICT":
                # Retry exhaustion under heavy contention is a legitimate,
                # reported outcome; anything else is a failure.
                errors.append(out.error)
            git_backend.read(ref, cache)
        except BaseException as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
    print(json.dumps({"errors": errors, "created": created}))
""")

MAINTAINER = textwrap.dedent("""
    import sys, time
    from pathlib import Path
    from todo_db import git_store
    ns, stop = Path(sys.argv[1]), Path(sys.argv[2])
    runs = 0
    while not stop.exists():
        if git_store.Store(ns, git_store.run_git).maintain():
            runs += 1
        time.sleep(0.05)
    print(runs)
""")


def test_concurrent_processes_share_one_store_safely(tmp_path: Path) -> None:
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    cache = tmp_path / "cache"
    git_backend.read(ref, cache)
    store = git_backend._store(ref, cache)
    stop = tmp_path / "stop"
    env = {**os.environ, "TODO_DB_STORE_MAINTENANCE": "off"}
    maintainer = subprocess.Popen([sys.executable, "-c", MAINTAINER, str(store.ns), str(stop)],
                                  stdout=subprocess.PIPE, text=True, env=env)
    workers = [
        subprocess.Popen([sys.executable, "-c", WORKER, ref.remote, str(cache), f"w{n}", "3"],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        for n in range(8)
    ]
    results = [w.communicate(timeout=600) for w in workers]
    stop.touch()
    runs = int(maintainer.communicate(timeout=120)[0].strip() or 0)
    created: set[str] = set()
    for out, err in results:
        report = json.loads(out)
        assert report["errors"] == [], err
        assert "cannot lock ref" not in err and "Traceback" not in err
        created |= set(report["created"])
    assert runs >= 1
    ro = git_backend.read(ref, cache)
    assert len(created) >= 16 and created <= set(ro.snapshot.index["items"])
    assert store.tip() == git_backend.ls_remote_tip(ref)
    assert not list((store.git_dir / "refs" / "todo-db" / "in").glob("*"))
    subprocess.run(["rm", "-rf", ref.remote], check=True)
    offline = git_backend.read(ref, cache)
    assert offline.stale is True and offline.rev == ro.rev


def test_cache_prune_removes_only_obsolete_entries(tmp_path: Path) -> None:
    ref = _remote(tmp_path)
    git_backend.bootstrap(ref)
    cache = tmp_path / "cache"
    git_backend.read(ref, cache)
    live = git_backend._store(ref, cache).ns
    for legacy in (live, cache / ("1" * 16)):
        (legacy / "revs" / ("a" * 40)).mkdir(parents=True)
        (legacy / "last").write_text("a" * 40 + "\n")
    (live / ".last.1.x.tmp").write_text("partial")
    (cache / ("b" * 40)).mkdir()
    (cache / ".trash-old").mkdir()
    stale = cache / ("2" * 16)
    (stale / "revs").mkdir(parents=True)
    old = 1_000_000_000
    for path in (stale / "revs", stale):
        os.utime(path, (old, old))
    (cache / "not-a-namespace").mkdir()

    dry = git_store.prune_cache(cache, dry_run=True)
    assert (live / "revs").exists() and (cache / ("b" * 40)).exists()
    removed = {Path(p).relative_to(cache).as_posix() for p in dry["removed"]}
    assert removed == {
        "b" * 40, ".trash-old", "2" * 16,
        f"{live.name}/revs", f"{live.name}/last", f"{live.name}/.last.1.x.tmp",
    }

    report = git_store.prune_cache(cache)
    assert {Path(p).relative_to(cache).as_posix() for p in report["removed"]} == removed
    assert report["maintained"] == [str(live)]
    # The namespace without a store keeps its legacy revision: its only offline copy.
    assert (cache / ("1" * 16) / "revs").exists() and (cache / "not-a-namespace").exists()
    assert sorted(p.name for p in cache.iterdir()) == sorted([live.name, "1" * 16, "not-a-namespace"])
    assert git_backend.read(ref, cache).stale is False
