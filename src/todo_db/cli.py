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


def _cache(args: argparse.Namespace) -> Path:
    if args.cache_dir:
        return Path(args.cache_dir).expanduser()
    override = os.environ.get("TODO_DB_CACHE_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".cache" / "todo-db-state"


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
        sha = git_backend.bootstrap(ref, worker=_worker(args))
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
                result = git_backend.reconcile(ref, args.op_id)
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
            outcome = git_backend.restore_rev(ref, args.restore_rev, _worker(args))
            if not outcome.ok:
                return _fail(outcome.error or "restore failed")
            _emit({"restored_to": args.restore_rev, "rev": outcome.sha})
            return EXIT_OK
        _emit({"history": git_backend.history(ref, limit=args.limit)})
        return EXIT_OK
    except TodoDBError as exc:
        return _fail(str(exc))


def cmd_list(args: argparse.Namespace) -> int:
    try:
        ref, _ = _ref_from(args)
        svc = TrackerService(ref=ref, cache_dir=_cache(args), worker="cli")
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
        svc = TrackerService(ref=ref, cache_dir=_cache(args), worker="cli")
        env = svc.show_item(args.id, field=args.field, offset=args.offset, budget=args.budget)
    except TodoDBError as exc:
        return _fail(str(exc))
    _emit(env)
    return EXIT_OK if env.get("ok") else EXIT_ERROR


def _add_target_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", help="path to a .todo-db/config.json (overrides discovery)")
    parser.add_argument("--state-remote", help="Git URL or path of the state remote")
    parser.add_argument("--state-branch", help="state branch (default: todo-state)")
    parser.add_argument("--cache-dir", help="local snapshot cache")
    parser.add_argument("--repo-root", help="project root for config discovery (default: cwd)")
    parser.add_argument("--actor", help="worker identity for mutations (default: TODO_DB_ACTOR or 'human')")


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

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except TodoDBError as exc:
        return _fail(str(exc))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
