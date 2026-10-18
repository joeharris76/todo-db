"""Minimal human/CI command line for the JSON/Git TODO tracker.

Bootstrap, validation, migration, and recovery live here. Agent planning
verbs (create/take/finish/...) are MCP-only by design: there is no
duplicate agent CLI. ``list``/``show`` are read-only recovery aids.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import git_backend
from . import store as S
from .errors import TodoDBError
from .migrate import migrate_file
from .service import TrackerService

EXIT_OK = 0
EXIT_ERROR = 2


def _emit(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return EXIT_ERROR


def _ref_from(args: argparse.Namespace) -> tuple[git_backend.StateRef, Path | None]:
    try:
        return git_backend.resolve_ref(
            remote=args.state_remote,
            branch=args.state_branch,
            config=args.config,
            repo_root=args.repo_root,
        )
    except TodoDBError as exc:
        raise exc


def _worker(args: argparse.Namespace) -> str:
    return args.actor or os.environ.get("TODO_DB_ACTOR") or "human"


def _session_id(args: argparse.Namespace) -> str:
    """Resolve the CLI session: flag, environment, or an ephemeral ID.

    Mutations from one CLI invocation belong to one session. An explicit
    ``--session`` / ``TODO_DB_SESSION`` value lets a caller resume it;
    otherwise a fresh per-invocation ID leaves no unattributed gaps in
    session history.
    """
    explicit = getattr(args, "session", None) or os.environ.get("TODO_DB_SESSION")
    if explicit and str(explicit).strip():
        # Fail closed: a forged session would land in commit trailers.
        return S.validate_session_id(str(explicit).strip())
    return uuid4().hex


def _cache(args: argparse.Namespace) -> Path:
    if getattr(args, "cache_dir", None):
        return Path(args.cache_dir).expanduser()
    return git_backend.default_cache_dir()


def _preflight_write_config(args: argparse.Namespace, ref: git_backend.StateRef) -> Path | None:
    """Decide the config write before any remote mutation, so a config
    refusal can never strand a freshly created branch."""
    if not args.write_config:
        return None
    root = Path(args.repo_root).expanduser().resolve() if args.repo_root else Path.cwd().resolve()
    parent = root / git_backend.CONFIG_DIRNAME
    # A non-directory at the config location (or an unwritable tree) must
    # refuse here, before the branch is created — never strand state.
    if parent.exists() and not parent.is_dir():
        raise TodoDBError(f"cannot write config: {parent} exists and is not a directory")
    path = parent / git_backend.CONFIG_FILENAME
    payload = {"state_remote": ref.remote, "state_branch": ref.branch}
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            existing = None
        if existing != payload:
            raise TodoDBError(f"refusing to overwrite differing config at {path}")
        return None
    return path


def cmd_bootstrap(args: argparse.Namespace) -> int:
    try:
        ref, _ = _ref_from(args)
        config_path = _preflight_write_config(args, ref)
    except TodoDBError as exc:
        # Bootstrap may be the very first command: allow explicit remote
        # without a config file even when discovery finds nothing.
        return _fail(str(exc))
    try:
        sha = git_backend.bootstrap(ref, worker=_worker(args), session=_session_id(args))
    except TodoDBError as exc:
        return _fail(str(exc))
    if config_path is not None:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps({"state_remote": ref.remote, "state_branch": ref.branch}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    _emit({"rev": sha, "remote": ref.remote, "branch": ref.branch})
    return EXIT_OK


def cmd_validate(args: argparse.Namespace) -> int:
    try:
        ref, _ = _ref_from(args)
        outcome = git_backend.read(ref, _cache(args))
    except TodoDBError as exc:
        return _fail(str(exc))
    now = datetime.now(timezone.utc)
    by_status: dict[str, int] = {}
    live = 0
    for entry in outcome.snapshot.index["items"].values():
        by_status[entry["status"]] = by_status.get(entry["status"], 0) + 1
        if S.claim_is_live(entry.get("claim"), now):
            live += 1
    _emit({
        "rev": outcome.rev,
        "stale": outcome.stale,
        "items": len(outcome.snapshot.index["items"]),
        "by_status": by_status,
        "live_claims": live,
    })
    # Authoritative validation must inspect authoritative state. A stale
    # cache passes only behind an explicit flag, so a CI gate cannot go
    # green on a cached revision while the remote is down.
    if (git_backend._ns_dir(_cache(args), ref) / "revs").is_dir():
        print("note: a legacy snapshot cache remains; run `todo-db cache prune` to reclaim it", file=sys.stderr)
    failure = git_backend._store(ref, _cache(args)).ns / "maintenance.error"
    if failure.is_file():
        print(f"note: cache maintenance failed: {failure.read_text(encoding='utf-8').strip()}", file=sys.stderr)
    if outcome.stale and not args.allow_stale:
        print("error: served a stale cached revision; remote unreachable (retry, or pass --allow-stale)", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK


def cmd_migrate(args: argparse.Namespace) -> int:
    try:
        ref, _ = _ref_from(args)
        report = migrate_file(
            args.from_export,
            ref,
            worker=_worker(args),
            dry_run=args.dry_run,
            backup_dir=args.backup_dir,
            session=_session_id(args),
            cache_dir=_cache(args),
        )
    except TodoDBError as exc:
        return _fail(str(exc))
    _emit(report)
    return EXIT_OK


def cmd_recover(args: argparse.Namespace) -> int:
    try:
        ref, _ = _ref_from(args)
    except TodoDBError as exc:
        return _fail(str(exc))
    try:
        if args.op_id:
            try:
                result = git_backend.reconcile(ref, args.op_id, cache_dir=_cache(args))
            except TodoDBError as exc:
                return _fail(str(exc))
            if result.state == "applied":
                _emit({"op_id": args.op_id, "rev": result.sha, "applied": True})
                return EXIT_OK
            if result.state == "absent":
                _emit({
                    "op_id": args.op_id,
                    "applied": False,
                    "checked_rev": result.sha,
                    "note": "not present at the inspected tip; re-apply only deliberately",
                })
                return EXIT_OK
            return _fail(f"reconciliation unknown: {result.detail}; retry later with --op-id {args.op_id}")
        if args.restore_rev:
            outcome = git_backend.restore_rev(
                ref, args.restore_rev, _worker(args), session=_session_id(args), cache_dir=_cache(args),
            )
            if not outcome.ok:
                return _fail(outcome.error or "restore failed")
            _emit({"restored_to": args.restore_rev, "rev": outcome.sha})
            return EXIT_OK
        _emit({"history": git_backend.history(ref, limit=args.limit, cache_dir=_cache(args))})
        return EXIT_OK
    except TodoDBError as exc:
        return _fail(str(exc))


def _read_session_id(args: argparse.Namespace) -> str | None:
    """Best-effort session for read-only commands.

    Reads never mutate, so a broken session configuration must not block
    them: fall back to unattributed rather than failing. Mutation paths
    keep the fail-closed :func:`_session_id`.
    """
    try:
        return _session_id(args)
    except TodoDBError:
        return None


def cmd_list(args: argparse.Namespace) -> int:
    try:
        ref, _ = _ref_from(args)
        svc = TrackerService(
            ref=ref, cache_dir=_cache(args), worker="cli",
            session_id=_read_session_id(args), client_name="cli",
        )
        env = svc.list_items(
            status=args.status, priority=args.priority, text=args.text,
            ready_only=args.ready, limit=args.limit, cursor=args.cursor,
        )
    except TodoDBError as exc:
        return _fail(str(exc))
    _emit(env)
    return EXIT_OK if env.get("ok") else EXIT_ERROR


def cmd_show(args: argparse.Namespace) -> int:
    try:
        ref, _ = _ref_from(args)
        svc = TrackerService(
            ref=ref, cache_dir=_cache(args), worker="cli",
            session_id=_read_session_id(args), client_name="cli",
        )
        env = svc.show_item(args.id, field=args.field, offset=args.offset, budget=args.budget)
    except TodoDBError as exc:
        return _fail(str(exc))
    _emit(env)
    return EXIT_OK if env.get("ok") else EXIT_ERROR


def cmd_cache_prune(args: argparse.Namespace) -> int:
    from .git_store import prune_cache

    cache = _cache(args)
    aliases: dict[str, str] = {}
    try:
        ref, _ = _ref_from(args)
    except TodoDBError:
        ref = None
    if ref is not None:
        # A local remote's store is keyed by its resolved path, so its legacy
        # snapshots may sit under a namespace that no store will ever claim.
        legacy, current = git_backend.namespace(ref), git_backend._store(ref, cache).ns.name
        if legacy != current:
            aliases[legacy] = current
    report = prune_cache(cache, older_than_days=args.older_than, dry_run=args.dry_run, aliases=aliases)
    _emit(report)
    return EXIT_OK


def _add_target_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", help="path to a .todo-db/config.json (overrides discovery)")
    parser.add_argument("--state-remote", help="Git URL or path of the state remote")
    parser.add_argument("--state-branch", help="state branch (default: todo-state)")
    parser.add_argument("--cache-dir", help="local state cache (default: TODO_DB_CACHE_DIR or ~/.cache/todo-db-state)")
    parser.add_argument("--repo-root", help="project root for config discovery (default: cwd)")
    parser.add_argument("--actor", help="worker identity for mutations (default: TODO_DB_ACTOR or 'human')")
    parser.add_argument("--session", help="session identity for mutations (default: TODO_DB_SESSION or a fresh ID)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="todo-db",
        description="Minimal human/CI CLI for the JSON/Git TODO tracker.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    bootstrap = sub.add_parser("bootstrap", help="create the state branch with empty state")
    _add_target_options(bootstrap)
    bootstrap.add_argument("--write-config", action="store_true", help="write .todo-db/config.json in the repo root")
    bootstrap.set_defaults(func=cmd_bootstrap)

    validate = sub.add_parser("validate", help="validate the accepted tip and report counts")
    _add_target_options(validate)
    validate.add_argument(
        "--allow-stale",
        action="store_true",
        help="accept a cached revision when the remote is unreachable (default: fail)",
    )
    validate.set_defaults(func=cmd_validate)

    migrate = sub.add_parser("migrate", help="migrate a v2 lossless export envelope onto a fresh state branch")
    _add_target_options(migrate)
    migrate.add_argument("--from-export", required=True, help="path to the export JSON file (read-only)")
    migrate.add_argument("--dry-run", action="store_true", help="report the mapping without publishing")
    migrate.add_argument("--backup-dir", help="directory receiving a backup copy of the input")
    migrate.set_defaults(func=cmd_migrate)

    recover = sub.add_parser("recover", help="reconcile an operation ID, restore a revision, or show history")
    _add_target_options(recover)
    group = recover.add_mutually_exclusive_group()
    group.add_argument("--op-id", help="look up the accepted commit for an operation ID")
    group.add_argument("--restore-rev", help="append-only restore of a prior revision (new commit)")
    recover.add_argument("--limit", type=int, default=20, help="history entries (default: 20)")
    recover.set_defaults(func=cmd_recover)

    lst = sub.add_parser("list", help="list tasks as brief rows (read-only)")
    _add_target_options(lst)
    lst.add_argument("--status", default=None)
    lst.add_argument("--priority", default=None)
    lst.add_argument("--text", default=None)
    lst.add_argument("--ready", action="store_true")
    lst.add_argument("--limit", type=int, default=5)
    lst.add_argument("--cursor", default=None)
    lst.set_defaults(func=cmd_list)

    show = sub.add_parser("show", help="show one task (read-only)")
    _add_target_options(show)
    show.add_argument("id")
    show.add_argument("--field", default=None)
    show.add_argument("--offset", type=int, default=0)
    show.add_argument("--budget", type=int, default=6000)
    show.set_defaults(func=cmd_show)

    cache = sub.add_parser("cache", help="manage the local state cache")
    cache_sub = cache.add_subparsers(dest="cache_command", required=True)
    prune = cache_sub.add_parser(
        "prune",
        help="remove legacy snapshot folders and unused namespaces, then repack each store",
    )
    prune.add_argument("--cache-dir", help="local state cache (default: TODO_DB_CACHE_DIR or ~/.cache/todo-db-state)")
    prune.add_argument("--config", help="also clear this project's legacy cache (path to .todo-db/config.json)")
    prune.add_argument("--state-remote", help="also clear this state remote's legacy cache")
    prune.add_argument("--state-branch", help="state branch for --state-remote (default: todo-state)")
    prune.add_argument("--repo-root", help="project root for config discovery (default: cwd)")
    prune.add_argument(
        "--older-than", type=float, default=30.0, metavar="DAYS",
        help="remove namespaces unused for this many days (default: 30, minimum: 1)",
    )
    prune.add_argument("--dry-run", action="store_true", help="list what would be removed without removing it")
    prune.set_defaults(func=cmd_cache_prune)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except TodoDBError as exc:
        return _fail(str(exc))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
