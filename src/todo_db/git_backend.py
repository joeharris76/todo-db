"""Git publication protocol for the JSON task state.

The remote state branch is authoritative. For a mutation::

    1. Fetch its accepted tip and load that snapshot.
    2. Check the requested operation's preconditions and apply only that
       logical operation.
    3. Validate the resulting index/detail correspondence.
    4. Create a commit whose parent is exactly the snapshot used.
    5. Attempt an ordinary fast-forward push to that one state branch.
    6. Report success only after remote acceptance is confirmed.

A competing push means fetch, re-evaluate, and, where valid, reapply the
logical operation. Competing writers never auto-merge claim JSON and never
rebase an already-mutated index as a substitute for semantic validation:
the pure ``apply`` function runs again against the new tip, so a retry can
only succeed when the operation is still valid, and conflicting edits to
the same field fail as conflicts instead of silently overwriting.

Reties are bounded (:data:`DEFAULT_MAX_RETRIES`). Every mutation carries a
unique operation ID recorded as a ``Todo-Op-Id`` commit trailer, which is
the durable acceptance record: a successful push with a lost reply is
reconciled by searching the branch for the trailer, so the operation is
never applied twice. If the outcome cannot be determined, the result is an
explicit unknown/retryable outcome preserving the operation ID.

Offline reads may use a clearly identified cached revision. Offline
mutations never succeed locally. History stays append-only: force pushes
are never used. All writers must use this protocol; branch rewrites or
deletion invalidate its assumptions.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import tempfile
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator
from uuid import uuid4

from .errors import E_CONFLICT, E_OFFLINE, E_STATE, E_UNKNOWN, TodoError
from .git_store import Store, StoreError, run_git
from .store import Snapshot, load_snapshot, validate_snapshot

OP_ID_RE = re.compile(r"^[0-9a-f]{32}$")
MAX_STDERR_CHARS = 500


def _brief(stderr: str, limit: int = MAX_STDERR_CHARS) -> str:
    """Bound remote stderr before it reaches a user-facing message."""
    text = " ".join(stderr.strip().split())
    return text if len(text) <= limit else text[:limit] + "…"


def _check_op_id(op_id: str) -> str:
    if not isinstance(op_id, str) or not OP_ID_RE.fullmatch(op_id):
        raise TodoError(
            f"invalid operation ID {op_id!r}: expected 32 hex chars",
            code=E_STATE,
        )
    return op_id

STATE_BRANCH_FALLBACK = "todo-state"
DEFAULT_MAX_RETRIES = 5
GIT_TIMEOUT_S = 60
FIRST_FETCH_TIMEOUT_S = 300
OP_ID_TRAILER = "Todo-Op-Id"
SESSION_TRAILER = "Todo-Session"

CONFIG_DIRNAME = ".todo-db"
CONFIG_FILENAME = "config.json"


def load_state_config(path: str | Path) -> dict[str, Any]:
    """Load a ``.todo-db/config.json`` carrying ``state_remote``/``state_branch``."""
    import json as _json

    path = Path(path)
    try:
        payload = _json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TodoError(f"invalid repo config {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise TodoError(f"invalid repo config {path}: expected a JSON object")
    for key in ("state_remote", "state_branch"):
        if key in payload and (not isinstance(payload[key], str) or not payload[key].strip()):
            raise TodoError(f"invalid repo config {path}: {key!r} must be a non-empty string")
    return payload


def discover_config(start: str | Path | None = None) -> tuple[Path, dict[str, Any]] | None:
    """Walk up from *start* (default cwd) to a git root or home for the config."""
    current = Path(start).expanduser().resolve() if start else Path.cwd().resolve()
    probe = current
    git_root: Path | None = None
    while True:
        if (probe / ".git").exists():
            git_root = probe
            break
        if probe.parent == probe:
            break
        probe = probe.parent
    home = Path.home().resolve()
    for candidate in (current, *current.parents):
        path = candidate / CONFIG_DIRNAME / CONFIG_FILENAME
        if path.is_file():
            return path, load_state_config(path)
        if git_root is not None and candidate == git_root:
            break
        if candidate == home:
            break
    return None


def resolve_ref(
    *,
    remote: str | None = None,
    branch: str | None = None,
    config: str | None = None,
    repo_root: str | None = None,
) -> tuple[StateRef, Path | None]:
    """Resolve the authoritative state branch: flag > env > config file.

    Returns the ref plus the config path used, if any. Never touches the
    network, so resolving is safe anywhere including offline.
    """
    import os as _os

    remote = remote or _os.environ.get("TODO_DB_STATE_REMOTE")
    branch = branch or _os.environ.get("TODO_DB_STATE_BRANCH")
    config_ref = config or _os.environ.get("TODO_DB_CONFIG")
    config_path: Path | None = None
    if config_ref:
        config_path = Path(config_ref).expanduser().resolve()
        if not config_path.is_file():
            raise TodoError(f"--config/TODO_DB_CONFIG points to a missing file: {config_path}")
        payload = load_state_config(config_path)
        remote = remote or payload.get("state_remote")
        branch = branch or payload.get("state_branch")
    elif not remote:
        discovered = discover_config(repo_root)
        if discovered is not None:
            config_path, payload = discovered
            remote = remote or payload.get("state_remote")
            branch = branch or payload.get("state_branch")
    if not remote:
        hint = config_path or (
            (Path(repo_root).expanduser() if repo_root else Path.cwd()) / CONFIG_DIRNAME / CONFIG_FILENAME
        )
        raise TodoError(
            "no state remote: pass --state-remote, set TODO_DB_STATE_REMOTE, "
            f"or add state_remote to {hint} (scaffold one with `todo-db bootstrap --write-config`)"
        )
    return StateRef(remote=remote, branch=branch or STATE_BRANCH_FALLBACK), config_path


@dataclass(frozen=True)
class StateRef:
    """Where authoritative task state lives."""

    remote: str
    branch: str = STATE_BRANCH_FALLBACK


@dataclass
class MutationOutcome:
    ok: bool
    outcome: str  # "applied" | "conflict" | "offline" | "unknown" | "error"
    op_id: str
    sha: str | None = None
    ack: dict[str, Any] = field(default_factory=dict)
    code: str | None = None
    error: str | None = None


@dataclass
class ReadOutcome:
    snapshot: Snapshot
    rev: str
    stale: bool  # True when served from cache because the remote was unreachable


def new_op_id() -> str:
    return uuid4().hex


def _git(
    args: list[str], cwd: Path | None = None, timeout: int = GIT_TIMEOUT_S, **kwargs: Any,
) -> subprocess.CompletedProcess[Any]:
    # Inherited GIT_DIR, GIT_INDEX_FILE and identity variables are scrubbed
    # so a hook-launched server can never redirect state commands.
    return run_git(args, cwd, timeout, **kwargs)


def _git_ok(args: list[str], cwd: Path | None = None) -> str:
    proc = _git(args, cwd)
    if proc.returncode != 0:
        raise TodoError(f"git {' '.join(args)} failed: {proc.stderr.strip()}", code=E_STATE)
    return proc.stdout.strip()


def _branch_missing(stderr: str) -> bool:
    return "couldn't find remote ref" in stderr


def _missing_branch_error(ref: StateRef) -> TodoError:
    return TodoError(f"state branch {ref.branch!r} is missing; run bootstrap", code=E_STATE)


def _classify_remote_failure(stderr: str) -> str:
    text = stderr.lower()
    if "non-fast-forward" in text or "fetch first" in text or "failed to push some refs" in text:
        return "conflict"
    if (
        "could not resolve" in text
        or "could not read" in text
        or "connection refused" in text
        or "timed out" in text
        or "no such file or directory" in text
        or "not a git repository" in text
        or "does not exist" in text
        or "repository not found" in text
        or "permission denied" in text
        or "authentication failed" in text
        or "unable to access" in text
    ):
        return "offline"
    return "rejected"


def ls_remote_tip(ref: StateRef) -> str | None:
    """Return the accepted tip SHA, or None when the branch does not exist."""
    try:
        proc = _git(["ls-remote", ref.remote, ref.branch])
    except subprocess.TimeoutExpired as exc:
        raise TodoError(
            f"remote {ref.remote!r} did not answer within {GIT_TIMEOUT_S}s", code=E_OFFLINE,
        ) from exc
    if proc.returncode != 0:
        raise TodoError(
            f"remote {ref.remote!r} unreachable or not permitted: {_brief(proc.stderr)}",
            code=E_OFFLINE,
        )
    line = proc.stdout.strip()
    if not line:
        return None
    return line.split()[0]


def _find_op_commit(clone: Path, rev: str, op_id: str) -> str | None:
    # NOTE: callers fetch onto a private ref that is deleted afterwards, so
    # search from the given rev rather than any named ref.
    # --grep is substring matching, so candidates are verified against the
    # exact trailer line: a worker-controlled message containing
    # "Todo-Op-Id: <id>" anywhere else must never reconcile as that operation.
    _check_op_id(op_id)
    wanted = f"{OP_ID_TRAILER}: {op_id}"
    proc = _git(
        ["log", rev, "--fixed-strings", f"--grep={wanted}", "--format=%H"],
        clone,
    )
    if proc.returncode != 0:
        return None
    for sha in proc.stdout.strip().splitlines():
        body = _git(["show", "-s", "--format=%B", sha], clone)
        if body.returncode != 0:
            continue
        if any(line.strip() == wanted for line in body.stdout.splitlines()):
            return sha
    return None


def namespace(ref: StateRef) -> str:
    """Cache namespace for one state branch: SHA-256 of remote + branch."""
    digest = hashlib.sha256(f"{ref.remote}\0{ref.branch}".encode("utf-8")).hexdigest()
    return digest[:16]


def _ns_dir(cache_dir: str | Path, ref: StateRef) -> Path:
    return Path(cache_dir) / namespace(ref)


def default_cache_dir() -> Path:
    """``TODO_DB_CACHE_DIR``, else ``~/.cache/todo-db-state``."""
    import os as _os

    override = _os.environ.get("TODO_DB_CACHE_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".cache" / "todo-db-state"


def _remote_location(ref: StateRef) -> str:
    """The remote as the store must see it.

    The store lives in the cache directory, so a relative local path would
    resolve against the wrong base. URLs and scp-style remotes
    (``host:path``) pass through unchanged. Local paths resolve whether or
    not they exist, so an unreachable local remote still maps to its own
    store and offline reads find it.
    """
    raw = str(ref.remote)
    if "://" in raw or re.match(r"^[^/]*:", raw):
        return raw
    return str(Path(raw).expanduser().resolve())


def _store(ref: StateRef, cache_dir: str | Path | None) -> Store:
    """The object store for *ref*, keyed by its resolved location.

    Hashing the resolved location keeps two different repositories that
    share a relative spelling from sharing one store. For URL remotes it
    equals :func:`namespace`, so the store sits beside any legacy cache.
    """
    resolved = StateRef(remote=_remote_location(ref), branch=ref.branch)
    root = Path(cache_dir) if cache_dir is not None else default_cache_dir()
    # Late-bound so tests that replace _git also see store commands.
    return Store(_ns_dir(root, resolved), lambda *a, **k: _git(*a, **k), GIT_TIMEOUT_S)


def _open_store(store: Store) -> None:
    try:
        store.open()
    except (OSError, StoreError) as exc:
        raise TodoError(f"cannot open the local state store {store.git_dir}: {exc}", code=E_STATE) from exc


def _repair(store: Store) -> None:
    """Rebuild a damaged store; raise if it still cannot be opened."""
    store.repair(store.store_id())
    _open_store(store)


@contextmanager
def _store_errors() -> Iterator[None]:
    """Report a failing local store as E_STATE on paths that do not repair."""
    try:
        yield
    except StoreError as exc:
        raise TodoError(f"local state store failed: {exc}; the next read or write rebuilds it",
                        code=E_STATE) from exc


def _fetch_timeout(store: Store) -> int:
    # The first fetch for a namespace transfers the branch's whole history.
    return GIT_TIMEOUT_S if store.tip() else FIRST_FETCH_TIMEOUT_S


def _last_rev(ns: Path) -> str | None:
    try:
        return (ns / "last").read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


@dataclass(frozen=True)
class ReconcileResult:
    """Tri-state reconciliation: applied, definitively absent, or unknown."""

    state: str  # "applied" | "absent" | "unknown"
    sha: str | None = None  # applied commit, or inspected tip when absent
    detail: str = ""


def reconcile(ref: StateRef, op_id: str, *, cache_dir: str | Path | None = None) -> ReconcileResult:
    """Look up an operation ID against the accepted branch history.

    Transport failure and true absence are distinct: absence reports the
    inspected tip so the caller can see what was checked; only an
    unreachable remote reports unknown.
    """
    _check_op_id(op_id)
    found = _reconcile_best_effort(ref, op_id, cache_dir)
    if found is None:
        return ReconcileResult(state="unknown", detail=f"remote {ref.remote!r} unreachable")
    return found


def _commit_message(op: str, summary: str, op_id: str, worker: str, session: str | None = None) -> str:
    subject = f"todo({op}): {summary}"[:200]
    body = f"{OP_ID_TRAILER}: {op_id}\nTodo-Actor: {worker}\n"
    if session:
        # Trailer values are single lines by construction: the service
        # validates session IDs before they reach this point, and the
        # CLI validates its --session/TODO_DB_SESSION the same way.
        safe = " ".join(str(session).split())
        body += f"{SESSION_TRAILER}: {safe}\n"
    return f"{subject}\n\n{body}"


def mutate(
    ref: StateRef,
    *,
    op: str,
    summary: str,
    worker: str,
    op_id: str | None = None,
    apply: Callable[[Snapshot], dict[str, Any]],
    max_retries: int = DEFAULT_MAX_RETRIES,
    session: str | None = None,
    cache_dir: str | Path | None = None,
) -> MutationOutcome:
    """Apply one logical operation and publish it. See module docstring.

    ``apply`` receives a freshly loaded snapshot at the accepted tip, checks
    its own preconditions, and mutates it in place. It must be free of
    external side effects: it runs again on every retry, and retries never
    rerun verification commands or side effects (there are none to rerun).

    The commit is built with Git plumbing in the shared object store: only
    changed files are hashed and no checkout is written.
    """
    op_id = _check_op_id(op_id or new_op_id())
    store = _store(ref, cache_dir)
    url = _remote_location(ref)
    try:
        _open_store(store)
    except TodoError as exc:
        return MutationOutcome(ok=False, outcome="error", op_id=op_id, code=E_STATE, error=str(exc))
    repaired = False
    attempts = 0
    while True:
        try:
            with store.using():
                step = _mutate_once(store, ref, url, op, summary, worker, op_id, apply, session)
        except StoreError as exc:
            if repaired:
                return MutationOutcome(
                    ok=False, outcome="error", op_id=op_id, code=E_STATE,
                    error=f"local state store failed: {exc} (operation ID {op_id})",
                )
            repaired = True
            try:
                _repair(store)
            except (OSError, TodoError) as rexc:
                return MutationOutcome(ok=False, outcome="error", op_id=op_id, code=E_STATE, error=str(rexc))
            continue
        if step is not None:
            store.maybe_maintain()
            return step
        attempts += 1
        if attempts > max_retries:
            return MutationOutcome(
                ok=False, outcome="conflict", op_id=op_id, code=E_CONFLICT,
                error=f"competing updates did not settle within {max_retries} retries",
            )


def _mutate_once(
    store: Store,
    ref: StateRef,
    url: str,
    op: str,
    summary: str,
    worker: str,
    op_id: str,
    apply: Callable[[Snapshot], dict[str, Any]],
    session: str | None,
) -> MutationOutcome | None:
    """One fetch-apply-push attempt. None means a competing push won: retry."""
    with ExitStack() as stack:
        try:
            fetch, tip = stack.enter_context(store.incoming(url, ref.branch, _fetch_timeout(store)))
        except subprocess.TimeoutExpired:
            return MutationOutcome(
                ok=False, outcome="offline", op_id=op_id, code=E_OFFLINE,
                error=f"fetch timed out after {GIT_TIMEOUT_S}s (operation ID {op_id})",
            )
        if fetch.returncode != 0 or tip is None:
            if _branch_missing(fetch.stderr):
                return MutationOutcome(
                    ok=False, outcome="error", op_id=op_id, code=E_STATE,
                    error=str(_missing_branch_error(ref)),
                )
            return MutationOutcome(
                ok=False, outcome="offline", op_id=op_id, code=E_OFFLINE,
                error=f"fetch failed: {_brief(fetch.stderr)} (operation ID {op_id})",
            )
        store.advance_tip(tip)

        # Idempotency: a previous attempt may have pushed with a lost
        # reply. Never apply twice under one operation ID.
        existing = _find_op_commit(store.git_dir, tip, op_id)
        if existing:
            return MutationOutcome(ok=True, outcome="applied", op_id=op_id, sha=existing, ack={"duplicate": True})

        try:
            snapshot, files = store.load(tip)
        except TodoError as exc:
            return MutationOutcome(ok=False, outcome="error", op_id=op_id, code=exc.code or E_STATE, error=str(exc))
        try:
            ack = apply(snapshot) or {}
        except TodoError as exc:
            # Precondition failure after re-evaluation: a conflict, not a
            # retryable transport error. Never overwrite the newer state.
            code = exc.code or E_STATE
            outcome = "conflict" if code in (E_CONFLICT, "E_CLAIM_STALE") else "error"
            return MutationOutcome(ok=False, outcome=outcome, op_id=op_id, code=code, error=str(exc))
        try:
            validate_snapshot(snapshot.index, snapshot.details)
        except TodoError as exc:
            return MutationOutcome(ok=False, outcome="error", op_id=op_id, code=exc.code or E_STATE, error=str(exc))
        tree = store.write_tree(tip, snapshot, files)
        if tree == store.tree_of(tip):
            return MutationOutcome(
                ok=False, outcome="error", op_id=op_id, code=E_STATE,
                error="operation produced no state change",
            )
        new_sha = store.commit(tree, tip, _commit_message(op, summary, op_id, worker, session))
        try:
            push = store.push(url, new_sha, ref.branch)
        except subprocess.TimeoutExpired:
            push = None
        if push is None:
            # Ambiguous: the push may or may not have landed. Reconcile.
            rec = _reconcile(store, ref, op_id)
            if rec.state == "applied":
                return MutationOutcome(ok=True, outcome="applied", op_id=op_id, sha=rec.sha, ack=ack)
            return MutationOutcome(
                ok=False, outcome="unknown", op_id=op_id, code=E_UNKNOWN,
                error="push timed out with an undetermined outcome; "
                f"reconcile later with operation ID {op_id}",
            )
        if push.returncode == 0:
            try:
                confirmed = ls_remote_tip(ref)
            except TodoError:
                confirmed = "unreachable"
            if confirmed == new_sha:
                store.advance_tip(new_sha)
                return MutationOutcome(ok=True, outcome="applied", op_id=op_id, sha=new_sha, ack=ack)
            # Push reported success but confirmation disagrees: reconcile
            # rather than claiming success or failure without evidence.
            rec = _reconcile(store, ref, op_id)
            if rec.state == "applied":
                return MutationOutcome(ok=True, outcome="applied", op_id=op_id, sha=rec.sha, ack=ack)
            return MutationOutcome(
                ok=False, outcome="unknown", op_id=op_id, code=E_UNKNOWN,
                error="push reply was lost and the outcome cannot be determined; "
                f"reconcile later with operation ID {op_id}",
            )
        kind = _classify_remote_failure(push.stderr)
        if kind == "conflict":
            return None
        if kind == "offline":
            # The push may have landed before the connection broke.
            # Reconcile against the store: applied wins, unreachable is
            # unknown, and a definitive absence re-enters the retry loop
            # for semantic re-evaluation instead of blindly repeating.
            rec = _reconcile(store, ref, op_id)
            if rec.state == "applied":
                return MutationOutcome(ok=True, outcome="applied", op_id=op_id, sha=rec.sha, ack=ack)
            if rec.state == "unknown":
                return MutationOutcome(
                    ok=False, outcome="unknown", op_id=op_id, code=E_UNKNOWN,
                    error="push failed ambiguously and the outcome cannot be determined; "
                    f"reconcile later with operation ID {op_id}",
                )
            return None
        return MutationOutcome(
            ok=False, outcome="error", op_id=op_id, code=E_STATE,
            error=f"push rejected by the remote: {_brief(push.stderr)}",
        )


INDEX_NAME = "index.json"
ITEMS_NAME = "items"


def _search(store: Store, tip: str, op_id: str) -> ReconcileResult:
    store.advance_tip(tip)
    found = _find_op_commit(store.git_dir, tip, op_id)
    if found:
        return ReconcileResult(state="applied", sha=found)
    return ReconcileResult(state="absent", sha=tip)


def _reconcile(store: Store, ref: StateRef, op_id: str) -> ReconcileResult:
    # A timeout here leaves the outcome undetermined, never a raised error:
    # the caller must still hand back the operation ID.
    try:
        with store.incoming(_remote_location(ref), ref.branch) as (fetch, tip):
            if fetch.returncode != 0 or tip is None:
                return ReconcileResult(state="unknown", detail=f"fetch failed: {_brief(fetch.stderr)}")
            return _search(store, tip, op_id)
    except subprocess.TimeoutExpired:
        return ReconcileResult(state="unknown", detail=f"timed out after {GIT_TIMEOUT_S}s")


def _reconcile_best_effort(ref: StateRef, op_id: str, cache_dir: str | Path | None) -> ReconcileResult | None:
    """None only when the remote cannot be reached at all."""
    store = _store(ref, cache_dir)
    _open_store(store)
    with _store_errors(), store.using():
        try:
            with store.incoming(_remote_location(ref), ref.branch, _fetch_timeout(store)) as (fetch, tip):
                if fetch.returncode != 0 or tip is None:
                    if _branch_missing(fetch.stderr):
                        return ReconcileResult(state="unknown", detail=f"fetch failed: {_brief(fetch.stderr)}")
                    return None
                return _search(store, tip, op_id)
        except subprocess.TimeoutExpired:
            return ReconcileResult(state="unknown", detail=f"timed out after {GIT_TIMEOUT_S}s")


def _offline_cached(store: Store, ref: StateRef, cache_dir: str | Path) -> ReadOutcome:
    # Offline reads serve ONLY this state branch's last accepted revision,
    # never another project's cache and never an inferred ordering.
    try:
        if store.exists():
            with store.using():
                rev = store.tip()
                if rev is not None:
                    return ReadOutcome(snapshot=store.load(rev)[0], rev=rev, stale=True)
    except StoreError:
        pass
    # Caches written before the object store keep revs/<last> until
    # `todo-db cache prune` removes them; serve that until the store has a
    # tip, re-reading the pointer once in case it moved mid-read.
    legacy = _ns_dir(cache_dir, ref)
    for attempt in (1, 2):
        rev = _last_rev(legacy)
        if rev is None or not (legacy / "revs" / rev / INDEX_NAME).is_file():
            break
        try:
            return ReadOutcome(snapshot=load_snapshot(legacy / "revs" / rev), rev=rev, stale=True)
        except TodoError:
            if attempt == 2:
                raise
    raise TodoError(
        f"remote {ref.remote!r} unreachable and no cached revision "
        "for this state branch is available",
        code=E_OFFLINE,
    )


def read(ref: StateRef, cache_dir: str | Path) -> ReadOutcome:
    """Load the accepted tip, falling back to a cached revision offline.

    The reported revision is always the commit actually loaded, resolved
    after the fetch — never an earlier observation. Mutations never use
    this path: they go through :func:`mutate`, which fails offline rather
    than succeeding locally.

    A tip already in the object store is read without a fetch (a commit
    ID names its content, so it cannot be stale); otherwise only the
    objects the store lacks are fetched, for the state branch alone.
    """
    store = _store(ref, cache_dir)
    try:
        tip = ls_remote_tip(ref)
    except TodoError:
        return _offline_cached(store, ref, cache_dir)
    if tip is None:
        raise TodoError(
            f"state branch {ref.branch!r} is missing; run bootstrap", code=E_STATE
        )
    _open_store(store)
    for attempt in (1, 2):
        try:
            with store.using():
                outcome = _read_tip(store, ref, tip)
        except StoreError as exc:
            if attempt == 2:
                raise TodoError(f"local state store failed: {exc}", code=E_STATE) from exc
            _repair(store)
            continue
        if outcome is None:
            # The remote flaked mid-read; fall back to this branch's cache.
            return _offline_cached(store, ref, cache_dir)
        store.maybe_maintain()
        return outcome
    raise AssertionError("unreachable")


def _read_tip(store: Store, ref: StateRef, tip: str) -> ReadOutcome | None:
    if store.tip() == tip or store.has_commit(tip):
        snapshot = store.load(tip)[0]
        store.advance_tip(tip)
        return ReadOutcome(snapshot=snapshot, rev=tip, stale=False)
    try:
        with store.incoming(_remote_location(ref), ref.branch, _fetch_timeout(store)) as (fetch, actual):
            if fetch.returncode != 0 or actual is None:
                return None
            store.advance_tip(actual)
            return ReadOutcome(snapshot=store.load(actual)[0], rev=actual, stale=False)
    except subprocess.TimeoutExpired:
        return None


def _fetch_for_history(store: Store, ref: StateRef) -> str:
    """Fetch the branch into *store* and return its tip, or raise."""
    try:
        with store.incoming(_remote_location(ref), ref.branch, _fetch_timeout(store)) as (fetch, tip):
            if fetch.returncode != 0 or tip is None:
                if _branch_missing(fetch.stderr):
                    raise _missing_branch_error(ref)
                raise TodoError(f"remote {ref.remote!r} unreachable: {_brief(fetch.stderr)}", code=E_OFFLINE)
            store.advance_tip(tip)
            return tip
    except subprocess.TimeoutExpired as exc:
        raise TodoError(f"remote {ref.remote!r} did not answer within {GIT_TIMEOUT_S}s", code=E_OFFLINE) from exc


def read_rev(ref: StateRef, rev: str, *, cache_dir: str | Path | None = None) -> Snapshot:
    """Load the snapshot at a historical commit (recovery/restore reads)."""
    import json as _json

    store = _store(ref, cache_dir)
    _open_store(store)
    with _store_errors(), store.using():
        tip = _fetch_for_history(store, ref)
        if not store.has_commit(rev) or not store.is_ancestor(rev, tip):
            raise TodoError(f"revision {rev!r} not found on {ref.remote!r}", code=E_STATE)
        entries = store.entries(rev)
        index_entry = entries.get(INDEX_NAME)
        if index_entry is None or index_entry[1] != "blob":
            raise TodoError(f"revision {rev!r} has no {INDEX_NAME}", code=E_STATE)
        try:
            index = _json.loads(store.blob(index_entry[2]))
        except ValueError as exc:
            raise TodoError(f"revision {rev!r} has an invalid {INDEX_NAME}: {exc}", code=E_STATE) from exc
        details: dict[str, dict[str, Any]] = {}
        if isinstance(index, dict) and isinstance(index.get("items"), dict):
            wanted: dict[str, str] = {}
            for path, (_mode, kind, oid) in entries.items():
                if kind != "blob" or not path.startswith(f"{ITEMS_NAME}/"):
                    continue
                name = path.rsplit("/", 1)[-1]
                if name.endswith(".json") and name[:-5] in index["items"]:
                    wanted[name[:-5]] = oid
            data = store.blobs(set(wanted.values()))
            for item_id, oid in wanted.items():
                try:
                    details[item_id] = _json.loads(data[oid])
                except ValueError as exc:
                    raise TodoError(f"revision {rev!r} detail for {item_id!r} is invalid: {exc}", code=E_STATE) from exc
        validate_snapshot(index, details)
        return Snapshot(index=index, details=details)


def _assert_on_branch(ref: StateRef, rev: str, cache_dir: str | Path | None) -> None:
    """Refuse revisions that are not part of the state branch's history."""
    store = _store(ref, cache_dir)
    _open_store(store)
    with _store_errors(), store.using():
        tip = _fetch_for_history(store, ref)
        if not store.has_commit(rev) or not store.is_ancestor(rev, tip):
            raise TodoError(
                f"revision {rev!r} is not part of {ref.branch!r} history; restore refuses it",
                code=E_STATE,
            )


def restore_rev(
    ref: StateRef, rev: str, worker: str, session: str | None = None, *, cache_dir: str | Path | None = None,
) -> MutationOutcome:
    """Append-only restore: a new commit returning state to *rev*'s content."""
    import json as _json

    _assert_on_branch(ref, rev, cache_dir)
    wanted = read_rev(ref, rev, cache_dir=cache_dir)
    payload = _json.loads(_json.dumps({"index": wanted.index, "details": wanted.details}))

    def apply(snap: Snapshot) -> dict[str, Any]:
        snap.index = payload["index"]
        snap.details.clear()
        snap.details.update(payload["details"])
        return {"id": "restore", "rev": rev}

    return mutate(
        ref, op="restore", summary=f"restore state to {rev[:12]}",
        worker=worker, apply=apply, session=session, cache_dir=cache_dir,
    )


def bootstrap(ref: StateRef, *, worker: str = "bootstrap", session: str | None = None) -> str:
    """Create the state branch with an empty snapshot. Refuses to overwrite."""
    try:
        existing = ls_remote_tip(ref)
    except TodoError as exc:
        raise TodoError(f"cannot bootstrap: remote unreachable ({exc})", code=E_OFFLINE) from exc
    if existing is not None:
        raise TodoError(
            f"refusing to bootstrap: state branch {ref.branch!r} already exists at {existing}",
            code=E_CONFLICT,
        )
    from .store import Snapshot, empty_index, save_snapshot as _save

    tmp = Path(tempfile.mkdtemp(prefix="todo-bootstrap-"))
    try:
        _git_ok(["init", "-b", "main"], tmp)
        _git(["config", "user.name", "todo-db"], tmp)
        _git(["config", "user.email", "todo-db@localhost"], tmp)
        _save(tmp, Snapshot(index=empty_index(), details={}))
        _git_ok(["add", "--", INDEX_NAME, ITEMS_NAME], tmp)
        _git_ok(
            ["commit", "--quiet", "-m",
             _commit_message("bootstrap", "initial empty state", new_op_id(), worker, session)],
            tmp,
        )
        sha = _git_ok(["rev-parse", "HEAD"], tmp)
        push = _git(["push", "--quiet", ref.remote, f"HEAD:{ref.branch}"], tmp)
        if push.returncode != 0:
            # A concurrent bootstrap may have won the race; report collision
            # rather than overwriting.
            raise TodoError(
                f"bootstrap push failed (another writer may have created {ref.branch!r}): "
                f"{_brief(push.stderr)}",
                code=E_CONFLICT,
            )
        return sha
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _commit_trailers(work: Path, sha: str) -> dict[str, str]:
    """Read the exact trailer lines of one state commit.

    Attribution rides in ``Todo-Op-Id`` / ``Todo-Actor`` / ``Todo-Session``
    trailers. Only exact ``Key: value`` lines count — a worker-controlled
    subject mentioning such a string never qualifies — mirroring the
    strictness of :func:`_find_op_commit`. Missing trailers report as
    empty strings so pre-history commits keep working.
    """
    found = {OP_ID_TRAILER: "", "Todo-Actor": "", SESSION_TRAILER: ""}
    body = _git(["show", "-s", "--format=%B", sha], work)
    if body.returncode != 0:
        return {"op_id": "", "actor": "", "session": ""}
    # Split on "\n" only: Python's splitlines() also breaks on Unicode
    # separators (U+0085/U+2028/U+2029), which identities may no longer
    # contain but older commits might. First exact match wins, so an
    # injected later line can never override the real trailer.
    for line in body.stdout.split("\n"):
        for key in found:
            if found[key] == "" and line.startswith(f"{key}:"):
                found[key] = line.split(":", 1)[1].strip()
    return {"op_id": found[OP_ID_TRAILER], "actor": found["Todo-Actor"], "session": found[SESSION_TRAILER]}


def history(ref: StateRef, limit: int = 20, *, cache_dir: str | Path | None = None) -> list[dict[str, str]]:
    """Recovery aid: recent state-branch commits (ordinary Git history)."""
    store = _store(ref, cache_dir)
    _open_store(store)
    with _store_errors(), store.using():
        tip = _fetch_for_history(store, ref)
        out = store.git(["log", tip, f"-n{limit}", "--format=%H%x00%s%x00%aN%x00%aI"])
        if out.returncode != 0:
            return []
        entries = []
        for line in out.stdout.strip().splitlines():
            parts = line.split("\x00")
            if len(parts) == 4:
                entries.append({
                    "sha": parts[0], "subject": parts[1], "author": parts[2], "at": parts[3],
                    **_commit_trailers(store.git_dir, parts[0]),
                })
        return entries
