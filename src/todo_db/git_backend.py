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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from .errors import E_CONFLICT, E_OFFLINE, E_STATE, E_UNKNOWN, TodoError
from .store import Snapshot, load_snapshot, save_snapshot, validate_snapshot

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
OP_ID_TRAILER = "Todo-Op-Id"

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


def _git(args: list[str], cwd: Path | None = None, timeout: int = GIT_TIMEOUT_S) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _git_ok(args: list[str], cwd: Path | None = None) -> str:
    proc = _git(args, cwd)
    if proc.returncode != 0:
        raise TodoError(f"git {' '.join(args)} failed: {proc.stderr.strip()}", code=E_STATE)
    return proc.stdout.strip()


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
    proc = _git(["ls-remote", ref.remote, ref.branch])
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
    # NOTE: callers fetch with an explicit refspec, which updates FETCH_HEAD
    # but not necessarily the tracking ref, so search from the given rev.
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


def _remember_rev(ns: Path, rev: str) -> None:
    import os as _os

    ns.mkdir(parents=True, exist_ok=True)
    # Unique sibling per writer: two processes sharing one cache namespace
    # must never unlink each other's pointer source.
    tmp = ns / f".last.{_os.getpid()}.{uuid4().hex}.tmp"
    tmp.write_text(rev + "\n", encoding="utf-8")
    tmp.replace(ns / "last")


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


def reconcile(ref: StateRef, op_id: str) -> ReconcileResult:
    """Look up an operation ID against the accepted branch history.

    Transport failure and true absence are distinct: absence reports the
    inspected tip so the caller can see what was checked; only an
    unreachable remote reports unknown.
    """
    _check_op_id(op_id)
    found = _reconcile_best_effort(ref, op_id)
    if found is None:
        return ReconcileResult(state="unknown", detail=f"remote {ref.remote!r} unreachable")
    return found


def _commit_message(op: str, summary: str, op_id: str, worker: str) -> str:
    subject = f"todo({op}): {summary}"[:200]
    return f"{subject}\n\n{OP_ID_TRAILER}: {op_id}\nTodo-Actor: {worker}\n"


def mutate(
    ref: StateRef,
    *,
    op: str,
    summary: str,
    worker: str,
    op_id: str | None = None,
    apply: Callable[[Snapshot], dict[str, Any]],
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> MutationOutcome:
    """Apply one logical operation and publish it. See module docstring.

    ``apply`` receives a freshly loaded snapshot at the accepted tip, checks
    its own preconditions, and mutates it in place. It must be free of
    external side effects: it runs again on every retry, and retries never
    rerun verification commands or side effects (there are none to rerun).
    """
    op_id = _check_op_id(op_id or new_op_id())
    tmp = Path(tempfile.mkdtemp(prefix="todo-state-"))
    try:
        clone = _git(["clone", "--quiet", "--origin", "origin", ref.remote, str(tmp / "w")])
        if clone.returncode != 0:
            return MutationOutcome(
                ok=False, outcome="offline", op_id=op_id,
                code=E_OFFLINE, error=f"remote {ref.remote!r} unreachable: {_brief(clone.stderr)}",
            )
        work = tmp / "w"
        _git(["config", "user.name", "todo-db"], work)
        _git(["config", "user.email", "todo-db@localhost"], work)

        attempts = 0
        while True:
            fetch = _git(["fetch", "--quiet", "origin", ref.branch], work)
            if fetch.returncode != 0:
                if "couldn't find remote ref" in fetch.stderr:
                    return MutationOutcome(
                        ok=False, outcome="error", op_id=op_id, code=E_STATE,
                        error=f"state branch {ref.branch!r} is missing; run bootstrap",
                    )
                return MutationOutcome(
                    ok=False, outcome="offline", op_id=op_id, code=E_OFFLINE,
                    error=f"fetch failed: {_brief(fetch.stderr)} (operation ID {op_id})",
                )
            tip = _git_ok(["rev-parse", "FETCH_HEAD"], work)

            # Idempotency: a previous attempt may have pushed with a lost
            # reply. Never apply twice under one operation ID.
            existing = _find_op_commit(work, "FETCH_HEAD", op_id)
            if existing:
                return MutationOutcome(ok=True, outcome="applied", op_id=op_id, sha=existing, ack={"duplicate": True})

            checkout = _git(["checkout", "--quiet", "--detach", tip], work)
            if checkout.returncode != 0:
                return MutationOutcome(
                    ok=False, outcome="error", op_id=op_id, code=E_STATE,
                    error=f"cannot check out {tip}: {_brief(checkout.stderr)}",
                )
            try:
                snapshot = load_snapshot(work)
            except TodoError as exc:
                return MutationOutcome(
                    ok=False, outcome="error", op_id=op_id,
                    code=exc.code or E_STATE, error=str(exc),
                )
            try:
                ack = apply(snapshot) or {}
            except TodoError as exc:
                # Precondition failure after re-evaluation: a conflict, not a
                # retryable transport error. Never overwrite the newer state.
                code = exc.code or E_STATE
                outcome = "conflict" if code in (E_CONFLICT, "E_CLAIM_STALE") else "error"
                return MutationOutcome(
                    ok=False, outcome=outcome, op_id=op_id, code=code, error=str(exc),
                )
            try:
                validate_snapshot(snapshot.index, snapshot.details)
                save_snapshot(work, snapshot)
            except TodoError as exc:
                return MutationOutcome(
                    ok=False, outcome="error", op_id=op_id,
                    code=exc.code or E_STATE, error=str(exc),
                )
            _git(["add", "--", INDEX_NAME, ITEMS_NAME], work)
            commit = _git(["commit", "--quiet", "-m", _commit_message(op, summary, op_id, worker)], work)
            if commit.returncode != 0:
                if "nothing to commit" in (commit.stdout + commit.stderr):
                    return MutationOutcome(
                        ok=False, outcome="error", op_id=op_id, code=E_STATE,
                        error="operation produced no state change",
                    )
                return MutationOutcome(
                    ok=False, outcome="error", op_id=op_id, code=E_STATE,
                    error=f"commit failed: {_brief(commit.stderr)}",
                )
            new_sha = _git_ok(["rev-parse", "HEAD"], work)
            # Sanity: the commit's parent must be exactly the snapshot used.
            parent = _git_ok(["rev-parse", "HEAD^"], work)
            if parent != tip:
                return MutationOutcome(
                    ok=False, outcome="error", op_id=op_id, code=E_STATE,
                    error="commit parent diverged from the fetched tip; refusing to push",
                )
            try:
                push = _git(["push", "--quiet", "origin", f"HEAD:{ref.branch}"], work)
            except subprocess.TimeoutExpired:
                push = None
            if push is None:
                # Ambiguous: the push may or may not have landed. Reconcile.
                rec = _reconcile(work, ref, op_id)
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
                    return MutationOutcome(ok=True, outcome="applied", op_id=op_id, sha=new_sha, ack=ack)
                # Push reported success but confirmation disagrees: reconcile
                # rather than claiming success or failure without evidence.
                rec = _reconcile(work, ref, op_id)
                if rec.state == "applied":
                    return MutationOutcome(ok=True, outcome="applied", op_id=op_id, sha=rec.sha, ack=ack)
                return MutationOutcome(
                    ok=False, outcome="unknown", op_id=op_id, code=E_UNKNOWN,
                    error="push reply was lost and the outcome cannot be determined; "
                    f"reconcile later with operation ID {op_id}",
                )
            kind = _classify_remote_failure(push.stderr)
            if kind == "conflict":
                attempts += 1
                if attempts > max_retries:
                    return MutationOutcome(
                        ok=False, outcome="conflict", op_id=op_id, code=E_CONFLICT,
                        error=f"competing updates did not settle within {max_retries} retries",
                    )
                _git(["reset", "--quiet", "--hard", "FETCH_HEAD"], work)
                continue
            if kind == "offline":
                # The push may have landed before the connection broke.
                # Reconcile in this same clone: applied wins, unreachable is
                # unknown, and a definitive absence re-enters the retry loop
                # for semantic re-evaluation instead of blindly repeating.
                rec = _reconcile(work, ref, op_id)
                if rec.state == "applied":
                    return MutationOutcome(ok=True, outcome="applied", op_id=op_id, sha=rec.sha, ack=ack)
                if rec.state == "unknown":
                    return MutationOutcome(
                        ok=False, outcome="unknown", op_id=op_id, code=E_UNKNOWN,
                        error="push failed ambiguously and the outcome cannot be determined; "
                        f"reconcile later with operation ID {op_id}",
                    )
                attempts += 1
                if attempts > max_retries:
                    return MutationOutcome(
                        ok=False, outcome="conflict", op_id=op_id, code=E_CONFLICT,
                        error=f"competing updates did not settle within {max_retries} retries",
                    )
                _git(["reset", "--quiet", "--hard", "FETCH_HEAD"], work)
                continue
            return MutationOutcome(
                ok=False, outcome="error", op_id=op_id, code=E_STATE,
                error=f"push rejected by the remote: {_brief(push.stderr)}",
            )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


INDEX_NAME = "index.json"
ITEMS_NAME = "items"


def _search(work: Path, op_id: str) -> ReconcileResult:
    tip = _git(["rev-parse", "FETCH_HEAD"], work)
    if tip.returncode != 0:
        return ReconcileResult(state="unknown", detail="fetch did not resolve a tip")
    rev = tip.stdout.strip()
    found = _find_op_commit(work, "FETCH_HEAD", op_id)
    if found:
        return ReconcileResult(state="applied", sha=found)
    return ReconcileResult(state="absent", sha=rev)


def _reconcile(work: Path, ref: StateRef, op_id: str) -> ReconcileResult:
    fetch = _git(["fetch", "--quiet", "origin", ref.branch], work)
    if fetch.returncode != 0:
        return ReconcileResult(state="unknown", detail=f"fetch failed: {_brief(fetch.stderr)}")
    return _search(work, op_id)


def _reconcile_best_effort(ref: StateRef, op_id: str) -> ReconcileResult | None:
    """None only when the remote cannot be reached at all."""
    tmp = Path(tempfile.mkdtemp(prefix="todo-reconcile-"))
    try:
        clone = _git(["clone", "--quiet", "--origin", "origin", ref.remote, str(tmp / "w")])
        if clone.returncode != 0:
            return None
        work = tmp / "w"
        fetch = _git(["fetch", "--quiet", "origin", ref.branch], work)
        if fetch.returncode != 0:
            return ReconcileResult(state="unknown", detail=f"fetch failed: {_brief(fetch.stderr)}")
        return _search(work, op_id)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _offline_cached(ns: Path, ref: StateRef) -> ReadOutcome:
    # Offline reads serve ONLY this state branch's last accepted revision,
    # never another project's cache and never an inferred ordering.
    rev = _last_rev(ns)
    if rev is None or not (ns / "revs" / rev / INDEX_NAME).is_file():
        raise TodoError(
            f"remote {ref.remote!r} unreachable and no cached revision "
            "for this state branch is available",
            code=E_OFFLINE,
        )
    return ReadOutcome(snapshot=load_snapshot(ns / "revs" / rev), rev=rev, stale=True)


def read(ref: StateRef, cache_dir: str | Path) -> ReadOutcome:
    """Load the accepted tip, falling back to a cached revision offline.

    The reported revision is always the commit actually loaded, resolved
    after the fetch — never an earlier observation. Mutations never use
    this path: they go through :func:`mutate`, which fails offline rather
    than succeeding locally.
    """
    ns = _ns_dir(cache_dir, ref)
    try:
        tip = ls_remote_tip(ref)
    except TodoError:
        return _offline_cached(ns, ref)
    if tip is None:
        raise TodoError(
            f"state branch {ref.branch!r} is missing; run bootstrap", code=E_STATE
        )
    tmp = Path(tempfile.mkdtemp(prefix="todo-read-"))
    try:
        clone = _git(["clone", "--quiet", "--origin", "origin", ref.remote, str(tmp / "w")])
        work = tmp / "w"
        fetch = _git(["fetch", "--quiet", "origin", ref.branch], work)
        checkout = _git(["checkout", "--quiet", "FETCH_HEAD"], work)
        if clone.returncode != 0 or fetch.returncode != 0 or checkout.returncode != 0:
            # The remote flaked mid-read; fall back to this branch's cache.
            return _offline_cached(ns, ref)
        actual = _git_ok(["rev-parse", "HEAD"], work)
        snapshot = load_snapshot(work)
        dest = ns / "revs" / actual
        if not (dest / INDEX_NAME).is_file():
            dest.mkdir(parents=True, exist_ok=True)
            save_snapshot(dest, snapshot)
        _remember_rev(ns, actual)
        return ReadOutcome(snapshot=snapshot, rev=actual, stale=False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def read_rev(ref: StateRef, rev: str) -> Snapshot:
    """Load the snapshot at a historical commit (recovery/restore reads)."""
    import json as _json

    tmp = Path(tempfile.mkdtemp(prefix="todo-read-rev-"))
    try:
        clone = _git(["clone", "--quiet", "--origin", "origin", ref.remote, str(tmp / "w")])
        if clone.returncode != 0:
            raise TodoError(f"remote {ref.remote!r} unreachable", code=E_OFFLINE)
        work = tmp / "w"
        exists = _git(["cat-file", "-e", f"{rev}^{{commit}}"], work)
        if exists.returncode != 0:
            raise TodoError(f"revision {rev!r} not found on {ref.remote!r}", code=E_STATE)
        raw_index = _git(["show", f"{rev}:{INDEX_NAME}"], work)
        if raw_index.returncode != 0:
            raise TodoError(f"revision {rev!r} has no {INDEX_NAME}", code=E_STATE)
        try:
            index = _json.loads(raw_index.stdout)
        except ValueError as exc:
            raise TodoError(f"revision {rev!r} has an invalid {INDEX_NAME}: {exc}", code=E_STATE) from exc
        details: dict[str, dict[str, Any]] = {}
        if isinstance(index.get("items"), dict):
            tree = _git(["ls-tree", "-r", "--name-only", rev, "--", ITEMS_NAME], work)
            if tree.returncode == 0:
                for line in tree.stdout.splitlines():
                    name = line.strip().rsplit("/", 1)[-1]
                    if not name.endswith(".json"):
                        continue
                    item_id = name[:-5]
                    if item_id not in index["items"]:
                        continue
                    raw = _git(["show", f"{rev}:{ITEMS_NAME}/{name}"], work)
                    if raw.returncode != 0:
                        raise TodoError(f"revision {rev!r} is missing detail for {item_id!r}", code=E_STATE)
                    try:
                        details[item_id] = _json.loads(raw.stdout)
                    except ValueError as exc:
                        raise TodoError(f"revision {rev!r} detail for {item_id!r} is invalid: {exc}", code=E_STATE) from exc
        validate_snapshot(index, details)
        return Snapshot(index=index, details=details)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _assert_on_branch(ref: StateRef, rev: str) -> None:
    """Refuse revisions that are not part of the state branch's history."""
    tmp = Path(tempfile.mkdtemp(prefix="todo-ancestry-"))
    try:
        clone = _git(["clone", "--quiet", "--origin", "origin", ref.remote, str(tmp / "w")])
        if clone.returncode != 0:
            raise TodoError(f"remote {ref.remote!r} unreachable", code=E_OFFLINE)
        work = tmp / "w"
        fetch = _git(["fetch", "--quiet", "origin", ref.branch], work)
        if fetch.returncode != 0:
            raise TodoError(f"cannot inspect state branch: {_brief(fetch.stderr)}", code=E_OFFLINE)
        ancestor = _git(["merge-base", "--is-ancestor", rev, "FETCH_HEAD"], work)
        if ancestor.returncode != 0:
            raise TodoError(
                f"revision {rev!r} is not part of {ref.branch!r} history; restore refuses it",
                code=E_STATE,
            )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def restore_rev(ref: StateRef, rev: str, worker: str) -> MutationOutcome:
    """Append-only restore: a new commit returning state to *rev*'s content."""
    import json as _json

    _assert_on_branch(ref, rev)
    wanted = read_rev(ref, rev)
    payload = _json.loads(_json.dumps({"index": wanted.index, "details": wanted.details}))

    def apply(snap: Snapshot) -> dict[str, Any]:
        snap.index = payload["index"]
        snap.details.clear()
        snap.details.update(payload["details"])
        return {"id": "restore", "rev": rev}

    return mutate(ref, op="restore", summary=f"restore state to {rev[:12]}", worker=worker, apply=apply)


def bootstrap(ref: StateRef, *, worker: str = "bootstrap") -> str:
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
        _git_ok(["commit", "--quiet", "-m", _commit_message("bootstrap", "initial empty state", new_op_id(), worker)], tmp)
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


def history(ref: StateRef, limit: int = 20) -> list[dict[str, str]]:
    """Recovery aid: recent state-branch commits (ordinary Git history)."""
    tmp = Path(tempfile.mkdtemp(prefix="todo-log-"))
    try:
        clone = _git(["clone", "--quiet", "--origin", "origin", ref.remote, str(tmp / "w")])
        if clone.returncode != 0:
            raise TodoError(f"remote {ref.remote!r} unreachable", code=E_OFFLINE)
        work = tmp / "w"
        _git(["fetch", "--quiet", "origin", ref.branch], work)
        out = _git(["log", "FETCH_HEAD", f"-n{limit}", "--format=%H%x00%s%x00%aN%x00%aI"], work)
        if out.returncode != 0:
            return []
        entries = []
        for line in out.stdout.strip().splitlines():
            parts = line.split("\x00")
            if len(parts) == 4:
                entries.append({"sha": parts[0], "subject": parts[1], "author": parts[2], "at": parts[3]})
        return entries
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
