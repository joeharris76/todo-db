"""Persistent Git object store behind reads, writes and recovery.

Each state branch gets one bare repository under the cache directory
(``<cache>/<namespace>/store.git``). Fetches are incremental, so a new tip
costs only the objects it adds, and snapshots are read straight from Git
objects instead of being unpacked into files. Git's packed deltas keep the
steady-state cost of a commit well under the bytes of the items it
touches; maintenance (:meth:`Store.maintain`) restores that packing once
unpacked objects exceed :data:`SLACK_CAP_BYTES`.

Many processes share one store. The design relies only on what Git
already makes safe for concurrent use: objects are written atomically and
are immutable, fetches land on a per-call ref under ``refs/todo-db/in/``
(no configured remote, so nothing races on remote-tracking refs), and the
one shared ref, :data:`TIP_REF`, moves only by compare-and-swap. A shared
``flock`` guards ordinary use so that a rebuild of a damaged store
(:meth:`Store.repair`) never pulls a repository out from under a reader.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator
from uuid import uuid4

from .errors import E_STATE, TodoError
from .store import INDEX_FILENAME, ITEMS_DIRNAME, Snapshot, assemble_snapshot, serialize_state

try:  # POSIX only; without it the store runs unlocked.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms
    fcntl = None  # type: ignore[assignment]

STORE_DIRNAME = "store.git"
TIP_REF = "refs/todo-db/tip"
INCOMING_PREFIX = "refs/todo-db/in/"
STORE_ID_FILE = "todo-db-id"
LOCK_NAME = "store.lock"
MAINTENANCE_LOCK_NAME = "maintenance.lock"
MAINTENANCE_STAMP_NAME = "maintenance.stamp"
SLACK_CAP_BYTES = 8 * 1024 * 1024
MAINTENANCE_INTERVAL_S = 600
PRUNE_EXPIRE = "1.hour.ago"
STALE_INCOMING_S = 3600
MAINTENANCE_ENV = "TODO_DB_STORE_MAINTENANCE"  # background (default) | inline | off

COMMIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "todo-db",
    "GIT_AUTHOR_EMAIL": "todo-db@localhost",
    "GIT_COMMITTER_NAME": "todo-db",
    "GIT_COMMITTER_EMAIL": "todo-db@localhost",
}

# Store-local settings win over the user's global config: no automatic gc
# racing ours, fetched packs kept as packs, and no hooks or signing.
STORE_CONFIG = (
    ("gc.auto", "0"),
    ("maintenance.auto", "false"),
    ("transfer.unpackLimit", "1"),
    ("fetch.writeCommitGraph", "false"),
    ("core.hooksPath", os.devnull),
    ("core.logAllRefUpdates", "false"),
    ("commit.gpgSign", "false"),
)

# Inherited variables that would redirect a command to another repository,
# index or identity. Transport and credential settings pass through.
SCRUBBED_ENV = frozenset({
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_INDEX_VERSION",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR",
    "GIT_NAMESPACE", "GIT_PREFIX", "GIT_QUARANTINE_PATH", "GIT_SHALLOW_FILE",
    "GIT_GRAFT_FILE", "GIT_REPLACE_REF_BASE",
    "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE",
    "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_COMMITTER_DATE",
})

SYMLINK_MODE = "120000"
FILE_MODE = "100644"

Runner = Callable[..., "subprocess.CompletedProcess[Any]"]


def git_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in SCRUBBED_ENV}
    if extra:
        env.update(extra)
    return env


def run_git(
    args: list[str],
    cwd: Path | None = None,
    timeout: int = 60,
    *,
    input: str | bytes | None = None,
    env: dict[str, str] | None = None,
    text: bool = True,
) -> subprocess.CompletedProcess[Any]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        input=input,
        capture_output=True,
        text=text,
        timeout=timeout,
        check=False,
        env=git_env(env),
    )


def cleanup_message(message: str) -> str:
    """Normalize a commit message the way ``git commit -m`` does.

    That is ``--cleanup=whitespace``: strip trailing whitespace from each
    line, drop leading and trailing blank lines, collapse runs of blank
    lines, and end with exactly one newline. ``commit-tree`` stores its
    input verbatim, so plumbing-built commits apply this first to keep
    history identical to the porcelain path.
    """
    out: list[str] = []
    pending_blank = False
    for line in message.split("\n"):
        line = line.rstrip(" \t\r\v\f")
        if not line:
            pending_blank = bool(out)
            continue
        if pending_blank:
            out.append("")
            pending_blank = False
        out.append(line)
    return "\n".join(out) + "\n" if out else ""


class StoreError(Exception):
    """A local store command failed; the store may be damaged."""


def _brief(text: str, limit: int = 300) -> str:
    text = " ".join(str(text).strip().split())
    return text if len(text) <= limit else text[:limit] + "…"


Entry = tuple[str, str, str]  # (mode, type, object id)


class Store:
    """One state branch's object store. See the module docstring."""

    def __init__(self, ns: Path, run: Runner, timeout: int = 60) -> None:
        self.ns = Path(ns)
        self.git_dir = self.ns / STORE_DIRNAME
        self._run = run
        self.timeout = timeout

    # -- plumbing --

    def git(
        self,
        args: list[str],
        *,
        timeout: int | None = None,
        input: str | bytes | None = None,
        env: dict[str, str] | None = None,
        text: bool = True,
    ) -> subprocess.CompletedProcess[Any]:
        kwargs: dict[str, Any] = {}
        if input is not None:
            kwargs["input"] = input
        if env:
            kwargs["env"] = env
        if not text:
            kwargs["text"] = False
        return self._run(args, self.git_dir, timeout or self.timeout, **kwargs)

    def _ok(self, args: list[str], **kwargs: Any) -> Any:
        proc = self.git(args, **kwargs)
        if proc.returncode != 0:
            stderr = proc.stderr if isinstance(proc.stderr, str) else proc.stderr.decode("utf-8", "replace")
            raise StoreError(f"git {args[0]} failed in {self.git_dir}: {_brief(stderr)}")
        return proc.stdout

    # -- lifecycle --

    def exists(self) -> bool:
        return (self.git_dir / "HEAD").is_file()

    def open(self) -> None:
        """Create the store if needed; safe when processes race to do it."""
        if self.exists():
            return
        self.ns.mkdir(parents=True, exist_ok=True)
        staging = self.ns / f".init-{uuid4().hex}"
        # Until the store's own config exists, keep the user's global hooks
        # (for example a reference-transaction hook) out of init itself.
        no_hooks = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": os.devnull}
        try:
            init = self._run(["init", "--quiet", "--bare", str(staging)], self.ns, self.timeout, env=no_hooks)
            if init.returncode != 0:
                raise StoreError(f"git init failed in {self.ns}: {_brief(init.stderr)}")
            # One append instead of a `git config` call per key: store
            # creation sits on the first read's latency.
            with open(staging / "config", "a", encoding="utf-8") as handle:
                for key, value in STORE_CONFIG:
                    section, name = key.split(".")
                    handle.write(f"[{section}]\n\t{name} = {value}\n")
            (staging / STORE_ID_FILE).write_text(uuid4().hex + "\n", encoding="utf-8")
            try:
                os.rename(staging, self.git_dir)
            except OSError:
                if not self.exists():
                    raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def store_id(self) -> str:
        try:
            return (self.git_dir / STORE_ID_FILE).read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    @contextmanager
    def _flock(self, name: str, mode: int, blocking: bool = True) -> Iterator[bool]:
        if fcntl is None:
            yield True
            return
        self.ns.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.ns / name, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            try:
                fcntl.flock(fd, mode | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError:
                yield False
                return
            yield True
        finally:
            os.close(fd)

    @contextmanager
    def using(self) -> Iterator[None]:
        """Hold the shared lock for ordinary reads and writes."""
        with self._flock(LOCK_NAME, fcntl.LOCK_SH if fcntl else 0):
            # The lock file's mtime records last use, for `cache prune`.
            try:
                os.utime(self.ns / LOCK_NAME)
            except OSError:
                pass
            yield

    def repair(self, failed_id: str) -> None:
        """Move a damaged store aside so the next :meth:`open` rebuilds it.

        Only the store that failed is moved: if another process already
        rebuilt it (a different store ID), this does nothing.
        """
        with self._flock(LOCK_NAME, fcntl.LOCK_EX if fcntl else 0):
            if not self.exists() or self.store_id() != failed_id:
                return
            trash = self.ns / f".trash-{uuid4().hex}"
            os.rename(self.git_dir, trash)
        shutil.rmtree(trash, ignore_errors=True)

    # -- refs --

    def resolve(self, rev: str) -> str | None:
        proc = self.git(["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"])
        return proc.stdout.strip() if proc.returncode == 0 else None

    def has_commit(self, rev: str) -> bool:
        return self.git(["cat-file", "-e", f"{rev}^{{commit}}"]).returncode == 0

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        return self.git(["merge-base", "--is-ancestor", ancestor, descendant]).returncode == 0

    def tip(self) -> str | None:
        # The store never packs refs, so the loose ref file is the ref; read
        # it directly to save a subprocess on every read.
        try:
            value = (self.git_dir / TIP_REF).read_text(encoding="ascii").strip()
        except OSError:
            return None
        return value if value and all(c in "0123456789abcdef" for c in value) else self.resolve(TIP_REF)

    def advance_tip(self, new: str) -> None:
        """Point :data:`TIP_REF` at *new* unless it already holds a descendant.

        The branch is append-only, so a newer tip descends from an older
        one and the ref never moves backwards when processes race. A tip
        that does not descend (a rewritten remote branch) still replaces
        it: the remote is authoritative. Best effort: a lost race retries.
        """
        for _ in range(5):
            current = self.tip()
            if current == new or (current and self.is_ancestor(new, current)):
                return
            old = current or "0" * len(new)
            if self.git(["update-ref", TIP_REF, new, old]).returncode == 0:
                return

    @contextmanager
    def incoming(
        self, url: str, branch: str, timeout: int | None = None,
    ) -> Iterator[tuple[subprocess.CompletedProcess[Any], str | None]]:
        """Fetch the branch onto a private ref; yield (process, fetched tip).

        Fetching by URL with an explicit refspec updates nothing but that
        ref, and every local ref is advertised as a "have", so only new
        objects transfer. The ref is deleted on exit; callers keep what
        they need reachable through :meth:`advance_tip`.
        """
        ref = INCOMING_PREFIX + uuid4().hex
        try:
            proc = self.git(
                ["fetch", "--quiet", "--no-tags", url, f"+refs/heads/{branch}:{ref}"],
                timeout=timeout or self.timeout,
            )
            yield proc, (self.resolve(ref) if proc.returncode == 0 else None)
        finally:
            self.git(["update-ref", "-d", ref])

    # -- reading --

    def entries(self, rev: str) -> dict[str, Entry]:
        raw = self._ok(["ls-tree", "-r", "-t", "-z", "--full-tree", rev], text=False)
        out: dict[str, Entry] = {}
        for record in raw.split(b"\0"):
            if not record:
                continue
            meta, _, path = record.partition(b"\t")
            mode, kind, oid = meta.decode("ascii").split()
            out[path.decode("utf-8", "surrogateescape")] = (mode, kind, oid)
        return out

    def blobs(self, oids: set[str]) -> dict[str, bytes]:
        if not oids:
            return {}
        ordered = sorted(oids)
        raw = self._ok(["cat-file", "--batch"], input=("\n".join(ordered) + "\n").encode("ascii"), text=False)
        out: dict[str, bytes] = {}
        pos = 0
        for oid in ordered:
            end = raw.index(b"\n", pos)
            header = raw[pos:end].decode("ascii").split()
            if len(header) != 3:
                raise StoreError(f"object {oid} is missing from {self.git_dir}")
            size = int(header[2])
            out[oid] = raw[end + 1 : end + 1 + size]
            pos = end + 1 + size + 1
        return out

    def blob(self, oid: str) -> bytes:
        return self.blobs({oid})[oid]

    def load(self, rev: str) -> tuple[Snapshot, dict[str, tuple[str, str, bytes]]]:
        """Load *rev* exactly as :func:`store.load_snapshot` loads a checkout.

        Returns the snapshot plus the state files it was built from, as
        ``{path: (mode, object id, bytes)}``, so a writer can tell which
        files its change actually touches.
        """
        entries = self.entries(rev)
        where = f"revision {rev[:12]}"
        items_entry = entries.get(ITEMS_DIRNAME)
        items_linked = items_entry is not None and items_entry[0] == SYMLINK_MODE

        def is_state_file(path: str) -> bool:
            if path == INDEX_FILENAME:
                return True
            head, _, name = path.partition("/")
            return head == ITEMS_DIRNAME and "/" not in name and name.endswith(".json")

        wanted = {
            path: entry for path, entry in entries.items()
            if is_state_file(path) and entry[1] == "blob" and entry[0] != SYMLINK_MODE
        }
        data = self.blobs({oid for _, _, oid in wanted.values()})

        def read_file(rel: str, label: str) -> Any:
            if rel.startswith(f"{ITEMS_DIRNAME}/") and items_linked:
                raise TodoError("refusing to follow symlinked items directory", code=E_STATE)
            entry = entries.get(rel)
            if entry is None:
                raise TodoError(f"missing {label} in {where}: is this a state checkout?", code=E_STATE)
            if entry[0] == SYMLINK_MODE:
                raise TodoError(f"refusing to follow symlink at {label}", code=E_STATE)
            if rel not in wanted:
                raise TodoError(f"{label} in {where} is not a regular file", code=E_STATE)
            try:
                return json.loads(data[entry[2]].decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise TodoError(f"invalid {label}: {exc}", code=E_STATE) from exc

        def detail_names() -> list[str]:
            if items_linked:
                raise TodoError("refusing to follow symlinked items directory", code=E_STATE)
            return [path.split("/", 1)[1] for path in entries if path != INDEX_FILENAME and is_state_file(path)]

        snapshot = assemble_snapshot(read_file, detail_names)
        files = {path: (mode, oid, data[oid]) for path, (mode, _, oid) in wanted.items()}
        return snapshot, files

    # -- writing --

    def write_tree(self, parent: str, snapshot: Snapshot, files: dict[str, tuple[str, str, bytes]]) -> str:
        """Write the tree for *snapshot* on top of *parent*.

        Only state files whose bytes or mode differ are hashed, which gives
        the same tree as rewriting every file in a checkout and running
        ``git add -- index.json items``: canonical bytes, mode 100644, and
        detail files that left the snapshot removed.
        """
        wanted: dict[str, bytes] = {INDEX_FILENAME: serialize_state(snapshot.index).encode("utf-8")}
        for item_id, detail in snapshot.details.items():
            wanted[f"{ITEMS_DIRNAME}/{item_id}.json"] = serialize_state(detail).encode("utf-8")
        changed = [
            path for path, body in wanted.items()
            if path not in files or files[path][2] != body or files[path][0] != FILE_MODE
        ]
        removed = [path for path in files if path not in wanted]
        lines: list[str] = []
        with tempfile.TemporaryDirectory(prefix="todo-tree-") as tmp:
            if changed:
                paths = []
                for n, path in enumerate(changed):
                    target = Path(tmp) / f"{n}.blob"
                    target.write_bytes(wanted[path])
                    paths.append(str(target))
                oids = self._ok(
                    ["hash-object", "-w", "--no-filters", "--stdin-paths"], input="\n".join(paths) + "\n",
                ).split()
                if len(oids) != len(changed):
                    raise StoreError("hash-object returned the wrong number of objects")
                lines += [f"{FILE_MODE} {oid}\t{path}" for oid, path in zip(oids, changed)]
            zero = "0" * len(parent)
            lines += [f"0 {zero}\t{path}" for path in removed]
            env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
            self._ok(["read-tree", parent], env=env)
            if lines:
                self._ok(["update-index", "--index-info"], input="\n".join(lines) + "\n", env=env)
            return self._ok(["write-tree"], env=env).strip()

    def tree_of(self, rev: str) -> str:
        return self._ok(["rev-parse", f"{rev}^{{tree}}"]).strip()

    def commit(self, tree: str, parent: str, message: str) -> str:
        return self._ok(
            ["commit-tree", tree, "-p", parent], input=cleanup_message(message), env=COMMIT_IDENTITY,
        ).strip()

    def push(self, url: str, sha: str, branch: str) -> subprocess.CompletedProcess[Any]:
        return self.git(["push", "--quiet", url, f"{sha}:refs/heads/{branch}"])

    # -- maintenance --

    def slack(self) -> int:
        """Bytes the store holds beyond its largest pack."""
        stats: dict[str, int] = {}
        for line in self._ok(["count-objects", "-v"]).splitlines():
            key, _, value = line.partition(":")
            if value.strip().isdigit():
                stats[key.strip()] = int(value.strip())
        packs = [p.stat().st_size for p in (self.git_dir / "objects" / "pack").glob("*.pack")]
        total = (stats.get("size", 0) + stats.get("size-pack", 0)) * 1024
        return max(0, total - max(packs, default=0))

    def maybe_maintain(self) -> None:
        """Start maintenance when slack passes the cap. Never raises."""
        mode = os.environ.get(MAINTENANCE_ENV, "background")
        if mode == "off":
            return
        try:
            stamp = self.ns / MAINTENANCE_STAMP_NAME
            if stamp.exists() and time.time() - stamp.stat().st_mtime < MAINTENANCE_INTERVAL_S:
                return
            if self.slack() <= SLACK_CAP_BYTES:
                return
            if mode == "inline":
                self.maintain()
                return
            subprocess.Popen(
                [sys.executable, "-c", MAINTAIN_ENTRY, "maintain", str(self.ns)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, close_fds=True, env=git_env(),
            )
        except (OSError, StoreError, subprocess.SubprocessError):
            return

    def maintain(self) -> bool:
        """Repack, prune and drop leaked fetch refs. False when already running.

        ``repack -A`` keeps unreachable objects as loose objects stamped
        with their pack's time, and ``prune`` removes only those older than
        :data:`PRUNE_EXPIRE`. Objects another process wrote moments ago and
        has not referenced yet (a fetch about to set its ref, a commit about
        to be pushed) therefore survive. Packs created after the repack
        starts are never touched.
        """
        if not self.exists():
            return False
        with self._flock(MAINTENANCE_LOCK_NAME, fcntl.LOCK_EX if fcntl else 0, blocking=False) as got:
            if not got:
                return False
            (self.ns / MAINTENANCE_STAMP_NAME).touch()
            cutoff = time.time() - STALE_INCOMING_S
            incoming = self.git_dir / INCOMING_PREFIX.rstrip("/")
            for ref in incoming.glob("*") if incoming.is_dir() else []:
                try:
                    if ref.stat().st_mtime < cutoff:
                        self.git(["update-ref", "-d", INCOMING_PREFIX + ref.name])
                except OSError:
                    continue
            self._ok(["repack", "-A", "-d", "-l", "-q"], timeout=max(self.timeout, 600))
            self._ok(["prune", f"--expire={PRUNE_EXPIRE}"], timeout=max(self.timeout, 600))
            return True


LEGACY_NAMES = ("revs", "last")
DEFAULT_PRUNE_DAYS = 30.0


def _is_hex(name: str, length: int) -> bool:
    return len(name) == length and all(c in "0123456789abcdef" for c in name)


def _last_used(ns: Path) -> float:
    stamps = [ns / LOCK_NAME, ns / "last", ns / "revs", ns / STORE_DIRNAME]
    return max((p.stat().st_mtime for p in stamps if p.exists()), default=ns.stat().st_mtime)


def prune_cache(
    cache_dir: Path,
    *,
    older_than_days: float = DEFAULT_PRUNE_DAYS,
    dry_run: bool = False,
    run: Runner = run_git,
    now: float | None = None,
) -> dict[str, Any]:
    """Remove what the object store made obsolete, then maintain each store.

    Removed: snapshot folders from before namespacing (40-hex names),
    leftover trash, namespaces unused for *older_than_days*, and, in a
    namespace whose store already has a tip, the legacy ``revs/``,
    ``last`` and ``.last.*.tmp`` files. A namespace with no store tip keeps
    its legacy revision, which is still its only offline copy. Everything
    is renamed into one trash folder first, so a reader never sees a
    half-deleted entry, and then deleted.
    """
    cache_dir = Path(cache_dir)
    report: dict[str, Any] = {"cache_dir": str(cache_dir), "dry_run": dry_run, "removed": [], "maintained": []}
    if not cache_dir.is_dir():
        return report
    cutoff = (now if now is not None else time.time()) - older_than_days * 86400
    victims: list[Path] = []
    stores: list[Store] = []
    for entry in sorted(cache_dir.iterdir()):
        if not entry.is_dir() or entry.is_symlink():
            continue
        if entry.name.startswith(".trash-") or _is_hex(entry.name, 40):
            victims.append(entry)
            continue
        if not _is_hex(entry.name, 16):
            continue
        if _last_used(entry) < cutoff:
            victims.append(entry)
            continue
        store = Store(entry, run)
        has_tip = store.tip() is not None
        for child in entry.iterdir():
            legacy = child.name in LEGACY_NAMES or (child.name.startswith(".last.") and child.name.endswith(".tmp"))
            if child.name.startswith((".trash-", ".init-")) or (legacy and has_tip):
                victims.append(child)
        if store.exists():
            stores.append(store)
    report["removed"] = [str(v) for v in victims]
    if dry_run:
        report["maintained"] = [str(s.ns) for s in stores]
        return report
    trash = cache_dir / f".trash-{uuid4().hex}"
    trash.mkdir()
    for n, victim in enumerate(victims):
        try:
            os.rename(victim, trash / f"{n}-{victim.name}")
        except OSError:
            continue
    shutil.rmtree(trash, ignore_errors=True)
    for store in stores:
        try:
            if store.maintain():
                report["maintained"].append(str(store.ns))
        except (OSError, StoreError):
            continue
    return report


def _main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "maintain":
        try:
            Store(Path(argv[1]), run_git).maintain()
        except (OSError, StoreError, subprocess.SubprocessError):
            return 1
        return 0
    return 2


# Detached maintenance runs through -c: `python -m` would re-import this
# module, which the package has already imported.
MAINTAIN_ENTRY = "import sys; from todo_db.git_store import _main; sys.exit(_main(sys.argv[1:]))"
