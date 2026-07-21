"""Command-line boundary for the standalone tracker."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

from .audit import canonical_json
from .database import TodoDatabase
from .database import TOOL_VERSION
from .errors import TodoDBError, TodoError
from .models import CredentialMode, DatabaseConfig, ProjectIdentity
from .tracker import PRIORITIES, TodoTracker


def _default_db() -> str:
    return (
        os.environ.get("TODO_DB_PATH")
        or os.environ.get("TODO_DB_URL")
        or str(Path.cwd() / ".todo-db" / "standalone.sqlite")
    )


def _identity_args(parser: argparse.ArgumentParser, *, required: bool = False) -> None:
    parser.add_argument("--project-id", default=argparse.SUPPRESS, required=required)
    parser.add_argument("--repository", default=argparse.SUPPRESS, required=required)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="todo-db", description="Project-isolated database-backed TODO tracker")
    parser.add_argument("--version", action="version", version=f"todo-db {TOOL_VERSION}")
    parser.add_argument("--db", default=_default_db(), help="local SQLite path or secure libsql/https URL")
    parser.add_argument("--replica", type=Path, help="local embedded-replica path for hosted read-write mode")
    parser.add_argument("--actor", help="audit actor identity")
    parser.add_argument("--project-id", default=os.environ.get("TODO_DB_PROJECT_ID", "todo-db-standalone"))
    parser.add_argument("--repository", default=os.environ.get("TODO_DB_REPOSITORY", "todo-db"))
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="create or validate the schema")
    _identity_args(init)

    export = sub.add_parser("export", help="write a lossless JSON export")
    _identity_args(export)
    export.add_argument("--output", "--out", dest="output", type=Path, required=True)

    restore = sub.add_parser("restore", help="replace tracker state from a verified JSON export")
    _identity_args(restore)
    restore.add_argument("--input", type=Path, required=True)
    restore.add_argument("--replace", action="store_true", help="confirm replacement of current tracker state")

    audit = sub.add_parser("audit", help="audit integrity operations")
    audit_sub = audit.add_subparsers(dest="audit_command", required=True)
    verify_audit = audit_sub.add_parser("verify", help="verify the database event hash chain")
    _identity_args(verify_audit)

    imported = sub.add_parser("import-yaml", help="import explicitly selected legacy YAML trees")
    imported.add_argument("--todo-dir", type=Path, required=True)
    imported.add_argument("--done-dir", type=Path)
    imported.add_argument("--skip-done", action="store_true")
    imported.add_argument("--dry-run", action="store_true")
    imported.add_argument("--verbose", action="store_true")
    imported.add_argument("--replace", action="store_true")
    _identity_args(imported)

    create = sub.add_parser("create", help="create an item from flags or JSON")
    create.add_argument("id", nargs="?")
    create.add_argument("--from", dest="from_source", metavar="- | FILE")
    create.add_argument("--title")
    create.add_argument("--worktree")
    create.add_argument("--priority", choices=PRIORITIES)
    create.add_argument("--description")
    create.add_argument("--category")
    create.add_argument("--approach")
    create.add_argument("--work", action="append", default=[], metavar="WID:SUMMARY[:needs=w1,w2]")
    create.add_argument("--needs", action="append", default=[])
    create.add_argument("--only-modify", action="append", default=[])
    create.add_argument("--do-not-modify", action="append", default=[])
    create.add_argument("--preserve", action="append", default=[])
    create.add_argument("--verify", action="append", default=[], metavar="DESC[::COMMAND[::EXPECTED]]")
    _identity_args(create)

    for name, help_text in (
        ("show", "show one item"),
        ("claim", "claim an item"),
        ("release", "release a claim"),
        ("deps", "show dependencies"),
        ("unblock", "clear a block"),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("id")
        if name == "show":
            command.add_argument("--json", action="store_true")
        _identity_args(command)

    start = sub.add_parser("start", help="start a work unit")
    start.add_argument("id")
    start.add_argument("wid")
    _identity_args(start)

    done = sub.add_parser("done", help="mark a work unit done")
    done.add_argument("id")
    done.add_argument("wid")
    done.add_argument("--evidence", required=True)
    _identity_args(done)

    defer = sub.add_parser("defer", help="record follow-up work")
    defer.add_argument("id")
    defer.add_argument("--summary", required=True)
    defer.add_argument("--reason", required=True)
    _identity_args(defer)

    promote = sub.add_parser("promote", help="promote a deferral to a new item")
    promote.add_argument("deferral_id", type=int)
    promote.add_argument("--to-item", required=True)
    promote.add_argument("--title")
    promote.add_argument("--priority", choices=PRIORITIES, default="medium")
    promote.add_argument("--worktree")
    promote.add_argument("--description")
    _identity_args(promote)

    dismiss = sub.add_parser("dismiss", help="dismiss a deferral")
    dismiss.add_argument("deferral_id", type=int)
    dismiss.add_argument("--reason", required=True)
    _identity_args(dismiss)

    for name, option in (("complete", "pr"), ("drop", "reason"), ("block", "reason")):
        command = sub.add_parser(name, help=f"{name} an item")
        command.add_argument("id")
        command.add_argument(f"--{option}", required=(option != "pr"), type=int if option == "pr" else str)
        _identity_args(command)

    listing = sub.add_parser("list", help="list items")
    listing.add_argument("--state", choices=("planning", "active", "done", "dropped"))
    listing.add_argument("--worktree")
    listing.add_argument("--priority", choices=PRIORITIES)
    listing.add_argument("--json", action="store_true")
    _identity_args(listing)

    for name in ("ready", "stats"):
        command = sub.add_parser(name, help=name)
        command.add_argument("--json", action="store_true")
        _identity_args(command)

    scope = sub.add_parser("check-scope", help="check changed files against an item's scope")
    scope.add_argument("id")
    scope.add_argument("files", nargs="*")
    scope.add_argument("--base")
    _identity_args(scope)

    verification = sub.add_parser("verify", help="run a recorded verification")
    verification.add_argument("id")
    verification.add_argument("--run", type=int)
    _identity_args(verification)

    lint = sub.add_parser("lint", help="lint item planning quality")
    lint.add_argument("id", nargs="?")
    lint.add_argument("--all", action="store_true")
    _identity_args(lint)

    sweep = sub.add_parser("sweep-stale", help="release expired claims")
    sweep.add_argument("--ttl-hours", type=float, default=24.0)
    _identity_args(sweep)

    migrate = sub.add_parser("migrate", help="apply pending migrations")
    _identity_args(migrate)

    config = sub.add_parser("config", help="get or set tracker configuration")
    config.add_argument("key", nargs="?")
    config.add_argument("value", nargs="?")
    _identity_args(config)
    return parser


def _config(args: argparse.Namespace, mode: CredentialMode) -> DatabaseConfig:
    return DatabaseConfig(
        path=args.db,
        identity=ProjectIdentity(project_id=args.project_id, repository=args.repository),
        credential_mode=mode,
        replica_path=args.replica,
    )


def _mode_for(args: argparse.Namespace) -> CredentialMode:
    return (
        CredentialMode.READ_WRITE
        if args.command
        in {
            "init",
            "import-yaml",
            "restore",
            "create",
            "claim",
            "release",
            "start",
            "done",
            "defer",
            "promote",
            "dismiss",
            "complete",
            "drop",
            "block",
            "unblock",
            "verify",
            "sweep-stale",
            "migrate",
            "config",
        }
        else CredentialMode.READ_ONLY
    )


def _parse_work(specs: list[str]) -> list[dict[str, Any]]:
    result = []
    for spec in specs:
        fields = spec.split(":", 2)
        if len(fields) < 2:
            raise TodoError(f"--work expects WID:SUMMARY[:needs=...], got {spec!r}")
        needs = []
        if len(fields) == 3 and fields[2].startswith("needs="):
            needs = [value for value in fields[2][len("needs=") :].split(",") if value]
        result.append({"id": fields[0], "summary": fields[1], "needs": needs})
    return result


def _parse_verify(specs: list[str]) -> list[dict[str, str]]:
    result = []
    for spec in specs:
        fields = spec.split("::")
        entry: dict[str, str] = {"description": fields[0]}
        if len(fields) > 1:
            entry["command"] = fields[1]
        if len(fields) > 2:
            entry["expected"] = fields[2]
        result.append(entry)
    return result


def _load_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.from_source:
        raw = sys.stdin.read() if args.from_source == "-" else Path(args.from_source).read_text(encoding="utf-8")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TodoError(f"--from payload is not valid JSON: {exc}") from exc
        if args.id and not payload.get("id"):
            payload["id"] = args.id
        allowed = {
            "id",
            "item_id",
            "title",
            "worktree",
            "priority",
            "description",
            "category",
            "approach",
            "state",
            "blocked_reason",
            "created_at",
            "completed_at",
            "completed_pr",
            "work",
            "deps",
            "scope",
            "verifications",
            "preserves",
            "anti_patterns",
            "prior_art",
            "deferrals",
        }
        return {key: value for key, value in payload.items() if key in allowed}
    required = ("id", "title", "worktree", "priority", "description")
    missing = [field for field in required if not getattr(args, field)]
    if missing:
        raise TodoError(f"create requires {', '.join(missing)} (or use --from - with a JSON payload)")
    return {
        "item_id": args.id,
        "title": args.title,
        "worktree": args.worktree,
        "priority": args.priority,
        "description": args.description,
        "category": args.category,
        "approach": args.approach,
        "work": _parse_work(args.work),
        "deps": args.needs,
        "scope": [("only_modify", value) for value in args.only_modify]
        + [("do_not_modify", value) for value in args.do_not_modify],
        "verifications": _parse_verify(args.verify),
        "preserves": args.preserve,
    }


def _print_work_order(order: dict[str, Any]) -> None:
    print(f"== {order['id']} [{order['priority']}] {order['title']}")
    print(f"state={order['state']} worktree={order['worktree']} claimed_by={order['claimed_by'] or '-'}")
    if order["blocked_reason"]:
        print(f"BLOCKED: {order['blocked_reason']}")
    for rule in order["scope"]:
        print(f"scope {rule['kind']}: {rule['path_glob']}")
    for unit in order.get("ready_units", []):
        print(f"ready {unit['wid']} [{unit['status']}] {unit['summary']}")
    for unit in order.get("blocked_units", []):
        print(f"blocked {unit['wid']} on {','.join(unit['unmet'])}: {unit['summary']}")
    for definition in order["deferrals"]:
        if definition["resolution"] == "open":
            print(f"open deferral #{definition['id']}: {definition['summary']}")


def _changed_files(base: str | None) -> list[str]:
    command = ["git", "diff", "--name-only", f"{base}...HEAD" if base else "HEAD"]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise TodoError(f"git diff failed: {result.stderr.strip()}")
    files = set(result.stdout.splitlines())
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"], capture_output=True, text=True, check=False
    )
    if untracked.returncode == 0:
        files.update(untracked.stdout.splitlines())
    return sorted(file for file in files if file)


def _main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        mode = _mode_for(args)
        with TodoDatabase.open(_config(args, mode)) as database:
            tracker = TodoTracker(database, actor=args.actor)
            command = args.command
            if command in {"init", "migrate"}:
                print(f"schema v{database.schema_version} ready for {database.project_identity.project_id}")
            elif command == "export":
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(canonical_json(database.export()) + "\n", encoding="utf-8")
                print(f"export written to {args.output}")
            elif command == "restore":
                if not args.replace:
                    raise TodoError("restore replaces current state; pass --replace to confirm")
                database.restore(json.loads(args.input.read_text(encoding="utf-8")))
                print(f"restored export from {args.input}")
            elif command == "audit":
                print(json.dumps(database.verify_audit(), sort_keys=True))
            elif command == "import-yaml":
                if args.replace:
                    if not _config(args, mode).is_hosted or args.dry_run:
                        raise TodoError("--replace only applies to a live import into the hosted backend")
                    tracker.clear_items()
                report = tracker.import_yaml_tree(
                    args.todo_dir, None if args.skip_done else args.done_dir, dry_run=args.dry_run
                )
                print(json.dumps(report, sort_keys=True))
                if args.verbose:
                    for line in report["warnings"] + report["skipped"]:
                        print(line, file=sys.stderr)
            elif command == "create":
                payload = _load_payload(args)
                item_id = tracker.create_item(**payload)
                print(f"created {item_id}")
            elif command == "show":
                item = tracker.get_item(args.id)
                if args.json:
                    print(json.dumps(item, indent=2, sort_keys=True))
                else:
                    _print_work_order(tracker.work_order(args.id))
            elif command == "claim":
                _print_work_order(tracker.claim(args.id))
            elif command == "release":
                tracker.release(args.id)
                print(f"released {args.id}")
            elif command == "deps":
                item = tracker.get_item(args.id)
                print("\n".join(f"{args.id} needs {dep}" for dep in item["deps"]) or f"{args.id} has no dependencies")
            elif command == "unblock":
                tracker.unblock(args.id)
                print(f"{args.id} unblocked")
            elif command == "start":
                tracker.start_unit(args.id, args.wid)
                print(f"{args.id}:{args.wid} in_progress")
            elif command == "done":
                tracker.done_unit(args.id, args.wid, args.evidence)
                print(f"{args.id}:{args.wid} done")
            elif command == "defer":
                print(f"deferral #{tracker.defer(args.id, args.summary, args.reason)} recorded")
            elif command == "promote":
                tracker.promote_deferral(
                    args.deferral_id,
                    args.to_item,
                    title=args.title,
                    priority=args.priority,
                    worktree=args.worktree,
                    description=args.description,
                )
                print(f"deferral #{args.deferral_id} promoted to {args.to_item}")
            elif command == "dismiss":
                tracker.dismiss_deferral(args.deferral_id, args.reason)
                print(f"deferral #{args.deferral_id} dismissed")
            elif command == "complete":
                tracker.complete(args.id, args.pr)
                print(f"{args.id} done")
            elif command == "drop":
                tracker.drop(args.id, args.reason)
                print(f"{args.id} dropped")
            elif command == "block":
                tracker.block(args.id, args.reason)
                print(f"{args.id} blocked")
            elif command == "list":
                items = tracker.list_items(state=args.state, worktree=args.worktree, priority=args.priority)
                print(
                    json.dumps(items, indent=2, sort_keys=True)
                    if args.json
                    else "\n".join(
                        f"{item['id']} {item['state']} {item['priority']} {item['worktree']}" for item in items
                    )
                )
            elif command == "ready":
                items = tracker.ready_items()
                print(
                    json.dumps(items, indent=2, sort_keys=True)
                    if args.json
                    else "\n".join(f"{item['id']} {item['priority']} {item['worktree']}" for item in items)
                )
            elif command == "stats":
                print(json.dumps(tracker.stats(), indent=2, sort_keys=True))
            elif command == "check-scope":
                files = args.files or _changed_files(args.base)
                violations = tracker.check_scope(args.id, files)
                for violation in violations:
                    print(violation)
                if violations:
                    return 1
                print(f"scope OK ({len(files)} changed file(s))")
                return 0
            elif command == "verify":
                if args.run is None:
                    print(json.dumps(tracker.get_item(args.id)["verifications"], indent=2, sort_keys=True))
                else:
                    result, output = tracker.run_verification(args.id, args.run)
                    print(f"seq {args.run}: {result}")
                    tail = "\n".join(output.splitlines()[-10:])
                    if tail:
                        print(tail)
                    return 0 if result == "pass" else 1
            elif command == "lint":
                if args.id is None and not args.all:
                    raise TodoError("lint requires an item id or --all")
                ids = [item["id"] for item in tracker.list_items()] if args.all else [args.id]
                findings = {item_id: tracker.lint(item_id) for item_id in ids}
                total = sum(len(values) for values in findings.values())
                for item_id, values in findings.items():
                    for finding in values:
                        print(f"{item_id}: {finding}")
                print(f"{total} finding(s) across {len(ids)} item(s)")
                return 1 if total else 0
            elif command == "sweep-stale":
                print("\n".join(tracker.sweep_stale(args.ttl_hours)))
            elif command == "config":
                if args.key is None:
                    print(
                        json.dumps(
                            {
                                key: tracker.get_config(key)
                                for key in ("lint.require_scope_rules", "lint.require_w0_revalidation")
                            },
                            sort_keys=True,
                        )
                    )
                elif args.value is None:
                    print(tracker.get_config(args.key))
                else:
                    tracker.set_config(args.key, args.value)
                    print(f"{args.key}={args.value}")
            else:  # pragma: no cover - argparse constrains command values
                parser.error(f"unsupported command: {command}")
        return 0
    except (TodoDBError, TodoError, OSError, ValueError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
