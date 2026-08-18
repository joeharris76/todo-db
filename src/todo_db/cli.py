"""Command-line boundary for the standalone tracker."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

from .audit import canonical_json
from .backends import auth_remediation, connect, hosted_error
from .database import SCHEMA_VERSION, TodoDatabase
from .database import TOOL_VERSION
from .errors import HostedAuthError, TodoDBError, TodoError
from .findings import (
    DISPOSITIONS,
    FINDING_KINDS,
    GATE_TEXT,
    LINK_KINDS,
    FindingsTracker,
    count_unsynced_drafts,
    create_draft,
    default_drafts_dir,
    unsynced_drafts,
)
from .models import CredentialMode, DatabaseConfig, ProjectIdentity
from .tracker import PRIORITIES, TodoTracker, _parse_anti_pattern


FINDING_OFFLINE_SUBCOMMANDS = frozenset({"create", "candidates"})
FINDING_MUTATING_SUBCOMMANDS = frozenset({"sync", "dismiss", "triage", "link", "promote"})

CONFIG_DIRNAME = ".todo-db"
CONFIG_FILENAME = "config.json"
DEFAULT_DB_RELATIVE = f"{CONFIG_DIRNAME}/standalone.sqlite"
DEFAULT_WRAPPER_RELATIVE = "_project/scripts/todo"
SCAFFOLD_GITIGNORE = "*.sqlite*\nreplica.db*\n*.lock\n!config.json\n"

IDENTITY_SOURCES_HINT = (
    "supply --project-id/--repository, set TODO_DB_PROJECT_ID/TODO_DB_REPOSITORY, "
    f"or run from a repo with a discovered {CONFIG_DIRNAME}/{CONFIG_FILENAME} "
    "(scaffold one with `todo-db init-project`)"
)

EXIT_CODES_EPILOG = """\
exit codes:
  0  success (doctor: every check passed; warnings allowed)
  1  findings reported (check-scope violations, lint findings, verify --run failures)
  2  generic error: fix the reported cause and retry
  4  hosted authentication failure: refresh the auth token, e.g.
     export TODO_DB_AUTH_TOKEN=$(turso db tokens create <db>), or run 'turso auth login'"""


def _load_repo_config(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TodoError(f"invalid repo config {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise TodoError(f"invalid repo config {path}: expected a JSON object")
    for key in ("project_id", "repository", "db"):
        if key in payload and (not isinstance(payload[key], str) or not payload[key].strip()):
            raise TodoError(f"invalid repo config {path}: {key!r} must be a non-empty string")
    return payload


def _git_root(path: Path) -> Path | None:
    probe = path.resolve()
    while True:
        if (probe / ".git").exists():
            return probe
        if probe.parent == probe:
            return None
        probe = probe.parent


def _discover_repo_config() -> tuple[Path, dict[str, Any]] | None:
    """Find `.todo-db/config.json` like git discovery: TODO_DB_CONFIG, else walk up from cwd."""

    override = os.environ.get("TODO_DB_CONFIG")
    if override:
        path = Path(override).expanduser()
        if not path.is_file():
            raise TodoError(f"TODO_DB_CONFIG points to a missing file: {path}")
        return path, _load_repo_config(path)
    current = Path.cwd().resolve()
    git_root = _git_root(current)
    home = Path.home().resolve()
    for candidate in (current, *current.parents):
        path = candidate / CONFIG_DIRNAME / CONFIG_FILENAME
        if path.is_file():
            return path, _load_repo_config(path)
        if git_root is not None and candidate == git_root:
            break
        if candidate == home:
            break
    return None


def _config_root(config_path: Path) -> Path:
    """The repo root a discovered config belongs to (config lives in `<root>/.todo-db/`)."""

    return config_path.parent.parent


def _resolve_identity(
    args: argparse.Namespace, discovered: tuple[Path, dict[str, Any]] | None
) -> ProjectIdentity | None:
    """Resolve identity per field: explicit flag > env > discovered config > nothing."""

    payload = discovered[1] if discovered else {}
    project_id = getattr(args, "project_id", None) or os.environ.get("TODO_DB_PROJECT_ID") or payload.get("project_id")
    repository = getattr(args, "repository", None) or os.environ.get("TODO_DB_REPOSITORY") or payload.get("repository")
    if project_id and repository:
        return ProjectIdentity(project_id=project_id, repository=repository)
    if project_id or repository:
        missing = "--repository" if project_id else "--project-id"
        raise TodoError(f"partial project identity: {missing} is also required ({IDENTITY_SOURCES_HINT})")
    return None


def _resolve_db(explicit: str | None, discovered: tuple[Path, dict[str, Any]] | None) -> str:
    """Resolve the database target: explicit flag > env > discovered config > local default."""

    if explicit:
        return explicit
    from_env = os.environ.get("TODO_DB_PATH") or os.environ.get("TODO_DB_URL")
    if from_env:
        return from_env
    if discovered is not None:
        config_path, payload = discovered
        target = payload.get("db")
        if target is None:
            return str(_config_root(config_path) / DEFAULT_DB_RELATIVE)
        if "://" in target or Path(target).is_absolute():
            return target
        return str(_config_root(config_path) / target)
    return str(Path.cwd() / DEFAULT_DB_RELATIVE)


def _identity_args(parser: argparse.ArgumentParser, *, required: bool = False) -> None:
    parser.add_argument("--project-id", default=argparse.SUPPRESS, required=required)
    parser.add_argument("--repository", default=argparse.SUPPRESS, required=required)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="todo-db",
        description="Project-isolated database-backed TODO tracker",
        epilog=EXIT_CODES_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"todo-db {TOOL_VERSION}")
    parser.add_argument(
        "--db",
        default=None,
        help="local SQLite path or secure libsql/https URL "
        f"(default: TODO_DB_PATH/TODO_DB_URL, then a discovered {CONFIG_DIRNAME}/{CONFIG_FILENAME},"
        f" then ./{DEFAULT_DB_RELATIVE})",
    )
    parser.add_argument("--replica", type=Path, help="local embedded-replica path for hosted read-write mode")
    parser.add_argument("--actor", help="audit actor identity")
    parser.add_argument("--project-id", default=argparse.SUPPRESS)
    parser.add_argument("--repository", default=argparse.SUPPRESS)
    parser.add_argument("--fields", help="comma-separated list of fields to project for list, ready, and show")
    parser.add_argument("--limit", type=int, default=None, help="maximum number of items to return")
    parser.add_argument(
        "--max-bytes", type=int, default=None, help="maximum bytes for command output with truncation marker"
    )

    compact_parser = argparse.ArgumentParser(add_help=False)
    compact_parser.add_argument(
        "--fields",
        default=argparse.SUPPRESS,
        help="comma-separated list of fields to project for list, ready, and show",
    )
    compact_parser.add_argument(
        "--limit", type=int, default=argparse.SUPPRESS, help="maximum number of items to return"
    )
    compact_parser.add_argument(
        "--max-bytes",
        type=int,
        default=argparse.SUPPRESS,
        help="maximum bytes for command output with truncation marker",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="create or validate the schema")
    _identity_args(init)

    init_project = sub.add_parser(
        "init-project",
        help=f"init the database and scaffold {CONFIG_DIRNAME}/{CONFIG_FILENAME}, a scoped .gitignore,"
        " and optionally a wrapper script",
    )
    _identity_args(init_project)
    init_project.add_argument(
        "--db",
        dest="db",
        default=argparse.SUPPRESS,
        help=f"local path (default {DEFAULT_DB_RELATIVE}) or libsql:// URL, recorded in the config file",
    )
    init_project.add_argument(
        "--wrapper",
        nargs="?",
        const=DEFAULT_WRAPPER_RELATIVE,
        default=None,
        metavar="PATH",
        help=f"also write an executable wrapper script (default location {DEFAULT_WRAPPER_RELATIVE})",
    )
    init_project.add_argument("--force", action="store_true", help="overwrite an existing config/wrapper")

    doctor = sub.add_parser("doctor", help="read-only preflight: config, identity, database, auth, drafts dir")
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument(
        "--rw", action="store_true", help="also probe hosted replica open+sync (writes local replica files)"
    )
    _identity_args(doctor)

    export = sub.add_parser("export", help="write a lossless JSON export")
    _identity_args(export)
    export.add_argument("--output", "--out", dest="output", type=Path, required=True)

    restore = sub.add_parser("restore", help="replace tracker state from a verified JSON export")
    _identity_args(restore)
    restore.add_argument("--input", type=Path, required=True)
    restore.add_argument("--replace", action="store_true", help="confirm replacement of current tracker state")

    restore_legacy = sub.add_parser(
        "restore-legacy", help="replace tracker state from a BenchBox legacy-schema snapshot"
    )
    _identity_args(restore_legacy)
    restore_legacy.add_argument("--input", type=Path, required=True)
    restore_legacy.add_argument("--replace", action="store_true", help="confirm replacement of current tracker state")

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

    update = sub.add_parser(
        "update",
        help="amend item metadata, work, dependencies, guardrails, or verifications (fully audited)",
    )
    update.add_argument("id")
    update.add_argument("--title")
    update.add_argument("--description")
    update.add_argument("--priority", choices=PRIORITIES)
    update.add_argument("--worktree")
    update.add_argument("--approach")
    update.add_argument("--category")
    update.add_argument("--add-work", action="append", default=[], metavar="WID:SUMMARY[:needs=w1,w2]")
    update.add_argument("--edit-work", action="append", default=[], metavar="WID:NEW-SUMMARY")
    update.add_argument("--add-work-need", action="append", default=[], metavar="WID:NEEDS_WID")
    update.add_argument("--drop-work-need", action="append", default=[], metavar="WID:NEEDS_WID")
    update.add_argument("--add-verify", action="append", default=[], metavar="DESC[::COMMAND[::EXPECTED]]")
    update.add_argument("--drop-verify", action="append", default=[], type=int, metavar="SEQ")
    update.add_argument("--add-only-modify", action="append", default=[], metavar="GLOB")
    update.add_argument("--drop-only-modify", action="append", default=[], metavar="GLOB")
    update.add_argument("--add-do-not-modify", action="append", default=[], metavar="GLOB")
    update.add_argument("--drop-do-not-modify", action="append", default=[], metavar="GLOB")
    update.add_argument("--add-needs", action="append", default=[], metavar="ITEM")
    update.add_argument("--drop-needs", action="append", default=[], metavar="ITEM")
    update.add_argument("--add-preserve", action="append", default=[], metavar="BEHAVIOR")
    update.add_argument("--drop-preserve", action="append", default=[], metavar="BEHAVIOR")
    update.add_argument(
        "--add-anti-pattern",
        action="append",
        default=[],
        metavar="DONT -- because WHY -- INSTEAD",
    )
    update.add_argument("--drop-anti-pattern", action="append", default=[], metavar="DONT")
    update.add_argument(
        "--add-prior-art",
        action="append",
        default=[],
        metavar="PATH::CONCEPT::reuse|extend|supersede",
    )
    update.add_argument("--drop-prior-art", action="append", default=[], metavar="PATH::CONCEPT")
    update.add_argument(
        "--reason",
        help="required for any edit to a done/dropped item, scope change, or a drop of verify/needs/preserve/anti-pattern/prior-art/work-need",
    )
    _identity_args(update)

    show = sub.add_parser("show", help="show one item", parents=[compact_parser])
    show.add_argument("id")
    show.add_argument("--json", action="store_true")
    _identity_args(show)

    for name, help_text in (
        ("claim", "claim an item"),
        ("release", "release a claim"),
        ("deps", "show dependencies"),
        ("unblock", "clear a block"),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("id")
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

    complete = sub.add_parser("complete", help="complete an item after running its verification ladder")
    complete.add_argument("id")
    complete.add_argument("--pr", type=int)
    complete.add_argument(
        "--override-verification",
        metavar="REASON",
        help="skip the verification ladder and record this override reason in the completion audit event",
    )
    _identity_args(complete)

    for name, option in (("drop", "reason"), ("block", "reason")):
        command = sub.add_parser(name, help=f"{name} an item")
        command.add_argument("id")
        command.add_argument(f"--{option}", required=True)
        _identity_args(command)

    listing = sub.add_parser("list", help="list items", parents=[compact_parser])
    listing.add_argument("--state", choices=("planning", "active", "done", "dropped"))
    listing.add_argument("--worktree")
    listing.add_argument("--priority", choices=PRIORITIES)
    listing.add_argument("--json", action="store_true")
    _identity_args(listing)

    ready = sub.add_parser("ready", help="ready items", parents=[compact_parser])
    ready.add_argument("--json", action="store_true")
    _identity_args(ready)

    stats = sub.add_parser("stats", help="stats")
    stats.add_argument("--json", action="store_true")
    _identity_args(stats)

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

    finding = sub.add_parser("finding", help="findings domain: capture, sync, triage, promote")
    finding_sub = finding.add_subparsers(dest="finding_command", required=True)

    fcreate = finding_sub.add_parser("create", help="write a draft finding (draft file only, never the DB)")
    fcreate.add_argument("--title", required=True)
    fcreate.add_argument("--finding-kind", required=True, choices=sorted(FINDING_KINDS))
    fcreate.add_argument("--review-context", required=True)
    fcreate.add_argument("--gate", help="class-not-instance attestation; must be 'class-not-instance'")
    fcreate.add_argument("--fixed-by", help="landed fix ref (required when --finding-kind bug-class)")
    fcreate.add_argument("--slug", help="kebab-slug override (default: derived from --title)")
    fcreate.add_argument("--finding", help="## Finding body text")
    fcreate.add_argument("--why", help="## Why this matters body text")
    fcreate.add_argument("--next-steps", dest="next_steps", help="## Suggested next steps body text")
    fcreate.add_argument("--observed-sha", help="provenance SHA (not a lookup key)")
    fcreate.add_argument("--drafts-dir", type=Path)
    _identity_args(fcreate)

    fcandidates = finding_sub.add_parser("candidates", help="list unsynced drafts (local glob, zero-credential)")
    fcandidates.add_argument("--drafts-dir", type=Path)
    _identity_args(fcandidates)

    flist = finding_sub.add_parser("list", help="list findings")
    flist.add_argument("--disposition", choices=DISPOSITIONS)
    flist.add_argument("--rank", choices=("urgency", "breadth", "confidence"), help="opt-in post-triage ranking")
    flist.add_argument("--json", action="store_true")
    _identity_args(flist)

    fshow = finding_sub.add_parser("show", help="show one finding")
    fshow.add_argument("id")
    fshow.add_argument("--json", action="store_true")
    _identity_args(fshow)

    fsync = finding_sub.add_parser("sync", help="land drafts into the tracker (the credentialed landing step)")
    fsync.add_argument("--drafts-dir", type=Path)
    _identity_args(fsync)

    fdismiss = finding_sub.add_parser("dismiss", help="dismiss a finding with a reason")
    fdismiss.add_argument("id")
    fdismiss.add_argument("--reason", required=True)
    _identity_args(fdismiss)

    ftriage = finding_sub.add_parser("triage", help="set judgement fields and/or move disposition")
    ftriage.add_argument("id")
    ftriage.add_argument("--urgency")
    ftriage.add_argument("--breadth")
    ftriage.add_argument("--confidence")
    ftriage.add_argument("--reconsider-after", dest="reconsider_after")
    ftriage.add_argument("--disposition", choices=("actionable", "actioned"))
    ftriage.add_argument("--reason")
    _identity_args(ftriage)

    flink = finding_sub.add_parser("link", help="link a finding to an item or another finding")
    flink.add_argument("id")
    flink.add_argument("--kind", required=True, choices=[kind for kind in LINK_KINDS if kind != "promoted-to"])
    flink.add_argument("--to-item", dest="to_item")
    flink.add_argument("--to-finding", dest="to_finding")
    flink.add_argument("--note")
    _identity_args(flink)

    fpromote = finding_sub.add_parser("promote", help="promote a finding to a planning item (atomic)")
    fpromote.add_argument("id")
    fpromote.add_argument("--to-item", dest="to_item", required=True)
    fpromote.add_argument("--title")
    fpromote.add_argument("--priority", choices=PRIORITIES, default="medium")
    fpromote.add_argument("--worktree")
    fpromote.add_argument("--description")
    _identity_args(fpromote)
    return parser


def _config(args: argparse.Namespace, mode: CredentialMode, identity: ProjectIdentity | None) -> DatabaseConfig:
    return DatabaseConfig(
        path=args.db,
        identity=identity,
        credential_mode=mode,
        replica_path=args.replica,
    )


def _mode_for(args: argparse.Namespace) -> CredentialMode:
    if args.command == "finding":
        return (
            CredentialMode.READ_WRITE
            if args.finding_command in FINDING_MUTATING_SUBCOMMANDS
            else CredentialMode.READ_ONLY
        )
    return (
        CredentialMode.READ_WRITE
        if args.command
        in {
            "init",
            "init-project",
            "import-yaml",
            "restore",
            "restore-legacy",
            "create",
            "update",
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


def _parse_work_edits(specs: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for spec in specs:
        wid, sep, summary = spec.partition(":")
        if not sep or not wid or not summary:
            raise TodoError(f"--edit-work expects WID:NEW-SUMMARY, got {spec!r}")
        if wid in result:
            raise TodoError(f"--edit-work names {wid!r} more than once")
        result[wid] = summary
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


def _parse_wid_pairs(specs: list[str], flag: str) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for spec in specs:
        wid, sep, needs_wid = spec.partition(":")
        if not sep or not wid or not needs_wid or ":" in needs_wid:
            raise TodoError(f"{flag} expects WID:NEEDS_WID, got {spec!r}")
        result.append((wid, needs_wid))
    return result


def _parse_anti_pattern_specs(specs: list[str]) -> list[tuple[str, str, str]]:
    return [_parse_anti_pattern(spec) for spec in specs]


def _parse_prior_art_adds(specs: list[str]) -> list[tuple[str, str, str]]:
    result: list[tuple[str, str, str]] = []
    for spec in specs:
        fields = spec.split("::")
        if len(fields) != 3 or not all(part.strip() for part in fields):
            raise TodoError(f"--add-prior-art expects PATH::CONCEPT::reuse|extend|supersede, got {spec!r}")
        result.append((fields[0].strip(), fields[1].strip(), fields[2].strip()))
    return result


def _parse_prior_art_drops(specs: list[str]) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for spec in specs:
        fields = spec.split("::")
        if len(fields) != 2 or not all(part.strip() for part in fields):
            raise TodoError(f"--drop-prior-art expects PATH::CONCEPT, got {spec!r}")
        result.append((fields[0].strip(), fields[1].strip()))
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


VALID_ITEM_FIELDS = frozenset(
    {
        "id",
        "title",
        "worktree",
        "priority",
        "state",
        "blocked_reason",
        "category",
        "description",
        "approach",
        "claimed_by",
        "claimed_at",
        "claimed_session",
        "claim_token",
        "claimed_branch",
        "claimed_worktree",
        "git_baseline",
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
        "ready_units",
        "blocked_units",
    }
)


def _project_fields(item: dict[str, Any], fields_str: str | None) -> dict[str, Any]:
    if not fields_str:
        return item
    fields = [f.strip() for f in fields_str.split(",") if f.strip()]
    if not fields:
        return item
    unknown = [f for f in fields if f not in VALID_ITEM_FIELDS]
    if unknown:
        raise TodoError(f"unknown field(s): {', '.join(unknown)}")
    return {k: item[k] for k in fields if k in item}


def _output(text: str, args: argparse.Namespace) -> None:
    max_bytes = getattr(args, "max_bytes", None)
    if max_bytes is not None:
        if max_bytes < 20:
            raise TodoError(f"--max-bytes must be at least 20, got {max_bytes}")
        encoded = text.encode("utf-8")
        target_bytes = max_bytes - 1
        if len(encoded) > target_bytes:
            low = 0
            high = len(encoded)
            best_cut = 0
            best_marker = ""
            while low <= high:
                mid = (low + high) // 2
                chunk = encoded[:mid].decode("utf-8", errors="ignore")
                omitted = len(encoded) - len(chunk.encode("utf-8"))
                marker = f"\n... [truncated: {omitted} bytes omitted]"
                candidate_len = len(chunk.encode("utf-8")) + len(marker.encode("utf-8"))
                if candidate_len <= target_bytes:
                    best_cut = mid
                    best_marker = marker
                    low = mid + 1
                else:
                    high = mid - 1

            if not best_marker:
                fallback = "\n... [truncated]"
                fallback_len = len(fallback.encode("utf-8"))
                if target_bytes >= fallback_len:
                    cut = target_bytes - fallback_len
                    truncated_text = encoded[:cut].decode("utf-8", errors="ignore")
                    text = f"{truncated_text}{fallback}"
                else:
                    text = fallback.strip()[:target_bytes]
            else:
                truncated_text = encoded[:best_cut].decode("utf-8", errors="ignore")
                text = f"{truncated_text}{best_marker}"
    print(text)


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


def _drafts_dir(args: argparse.Namespace, project_id: str | None) -> Path:
    supplied = getattr(args, "drafts_dir", None)
    if supplied:
        return Path(supplied).expanduser()
    if project_id is None and not os.environ.get("TODO_DB_FINDING_DRAFTS_DIR"):
        raise TodoError(f"the default drafts dir is project-scoped; pass --drafts-dir or {IDENTITY_SOURCES_HINT}")
    return default_drafts_dir(project_id or "")


def _finding_offline(args: argparse.Namespace, project_id: str | None) -> int:
    """Zero-credential capture: never opens a database connection."""

    drafts_dir = _drafts_dir(args, project_id)
    if args.finding_command == "create":
        print(GATE_TEXT, file=sys.stderr)
        path = create_draft(
            title=args.title,
            finding_kind=args.finding_kind,
            review_context=args.review_context,
            gate=args.gate,
            drafts_dir=drafts_dir,
            fixed_by=args.fixed_by,
            slug=args.slug,
            finding=args.finding,
            why=args.why,
            next_steps=args.next_steps,
            observed_sha=args.observed_sha,
        )
        print(f"Recorded: {path}")
        print("Draft only; run `todo-db finding sync` (a separate, credentialed step) to land it.")
        return 0
    drafts = unsynced_drafts(drafts_dir)
    for path in drafts:
        print(path.name)
    print(f"{len(drafts)} unsynced draft(s) in {drafts_dir}")
    return 0


def _print_finding(finding: dict[str, Any]) -> None:
    print(f"== {finding['id']} [{finding['disposition']}] {finding['title']}")
    print(f"kind={finding['finding_kind']} date={finding['date']} review={finding['review_context']}")
    if finding.get("disposition_reason"):
        print(f"reason: {finding['disposition_reason']}")
    for label in ("urgency", "breadth", "confidence", "reconsider_after", "observed_sha"):
        if finding.get(label):
            print(f"{label}: {finding[label]}")
    print("-- Finding")
    print(finding["finding_text"])
    print("-- Why this matters")
    print(finding["why_matters"])
    print("-- Suggested next steps")
    print(finding["next_steps"])
    for evidence in finding.get("evidence", []):
        location = ""
        if evidence.get("line_start"):
            location = f":{evidence['line_start']}"
            if evidence.get("line_end"):
                location += f"-{evidence['line_end']}"
        print(f"evidence: {evidence['path']}{location} {evidence.get('pattern') or ''}".rstrip())
    for link in finding.get("links", []):
        target = link.get("target_item") or link.get("target_finding") or "(dangling)"
        print(f"link: {link['kind']} -> {target}")


def _wrapper_script(project_id: str, wrapper_rel_path: str = DEFAULT_WRAPPER_RELATIVE) -> str:
    parts = Path(wrapper_rel_path).parent.parts
    upward = "/".join(".." for _ in parts) if parts else "."
    return f"""#!/usr/bin/env bash
#
# {project_id} TODO tracker entry point. Routes every subcommand to the
# canonical `todo-db` CLI. Project identity and database location come from
# the committed .todo-db/config.json discovered by the CLI; explicit flags and
# TODO_DB_* environment variables still take precedence over the config file.
#
# Tool resolution order:
#   1. TODO_DB_TOOL       explicit path to a todo-db checkout (uv run --project)
#   2. `todo-db` on PATH  an installed todo-db package
#   3. sibling checkout   <repo>/../todo-db
#
# On exit 4 (hosted auth failure) against a libsql:// database, a fresh token
# is minted with the turso CLI and the command retried once. The token is
# exported, never echoed. When remediation is impossible an ALERT block is
# printed to stderr and exit 4 propagates.
#
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${{BASH_SOURCE[0]}}")/{upward}" && pwd)"
export TODO_DB_CONFIG="${{TODO_DB_CONFIG:-$REPO_ROOT/.todo-db/config.json}}"

run_todo_db() {{
  if [ -n "${{TODO_DB_TOOL:-}}" ]; then
    uv run --project "$TODO_DB_TOOL" todo-db "$@"
    return
  fi
  if command -v todo-db >/dev/null 2>&1; then
    todo-db "$@"
    return
  fi
  TODO_DB_TOOL="$REPO_ROOT/../todo-db"
  if [ -d "$TODO_DB_TOOL" ]; then
    uv run --project "$TODO_DB_TOOL" todo-db "$@"
    return
  fi
  echo "todo: todo-db not found; install it or set TODO_DB_TOOL to a checkout (tried PATH and '$TODO_DB_TOOL')" >&2
  return 2
}}

print_auth_alert() {{
  cat >&2 <<'ALERT'
==================== TODO-DB AUTH ALERT ====================
Tracker writes are BLOCKED: hosted database authentication
failed and automatic token re-mint did not succeed.
Fix it now, one of:
  1. turso auth login
  2. export TODO_DB_AUTH_TOKEN="$(turso db tokens create <database-name>)"
Do not continue batch work until resolved: tracker items are
not being updated.
============================================================
ALERT
}}

status=0
run_todo_db "$@" || status=$?
if [ "$status" -ne 4 ]; then
  exit "$status"
fi

target="${{TODO_DB_URL:-}}"
if [ -z "$target" ] && [ -f "$TODO_DB_CONFIG" ]; then
  target="$(sed -n 's/.*"db"[[:space:]]*:[[:space:]]*"\\([^"]*\\)".*/\\1/p' "$TODO_DB_CONFIG" | head -n 1)" || target=""
fi
case "$target" in
  libsql://*) ;;
  *)
    print_auth_alert
    exit 4
    ;;
esac
if ! command -v turso >/dev/null 2>&1 || ! turso auth whoami >/dev/null 2>&1; then
  print_auth_alert
  exit 4
fi

host="${{target#libsql://}}"
host="${{host%%/*}}"
host="${{host%%\\?*}}"
db_name="$(turso db list 2>/dev/null | awk -v host="$host" 'index($0, host) {{print $1; exit}}')" || db_name=""
if [ -z "$db_name" ]; then
  print_auth_alert
  exit 4
fi
fresh_token="$(turso db tokens create "$db_name" 2>/dev/null)" || fresh_token=""
if [ -z "$fresh_token" ]; then
  print_auth_alert
  exit 4
fi
export TODO_DB_AUTH_TOKEN="$fresh_token"
echo "todo: hosted auth failure; minted a fresh token for '$db_name', retrying once" >&2

status=0
run_todo_db "$@" || status=$?
if [ "$status" -eq 4 ]; then
  print_auth_alert
  exit 4
fi
exit "$status"
"""


def _warn_if_git_ignored(path: Path, root: Path) -> None:
    result = subprocess.run(
        ["git", "check-ignore", "-q", str(path)], cwd=root, capture_output=True, text=True, check=False
    )
    if result.returncode == 0:
        print(
            f"warning: {path} is ignored by git; {CONFIG_FILENAME} is meant to be committed"
            f" -- adjust the repository .gitignore (a bare `{CONFIG_DIRNAME}/` rule hides it)",
            file=sys.stderr,
        )


def _init_project(args: argparse.Namespace, identity: ProjectIdentity, raw_db: str | None) -> int:
    """Run `init` and scaffold the repo: committed config, scoped .gitignore, optional wrapper."""

    root = Path.cwd()
    config_dir = root / CONFIG_DIRNAME
    config_path = config_dir / CONFIG_FILENAME
    gitignore_path = config_dir / ".gitignore"
    wrapper_path = (root / args.wrapper) if args.wrapper else None
    collisions = [path for path in (config_path, wrapper_path) if path is not None and path.exists()]
    if gitignore_path.exists() and gitignore_path.read_text(encoding="utf-8") != SCAFFOLD_GITIGNORE:
        collisions.append(gitignore_path)
    if collisions and not args.force:
        raise TodoError(
            "refusing to overwrite existing scaffolding: "
            + ", ".join(str(path) for path in sorted(set(collisions)))
            + "; pass --force to overwrite"
        )

    db_value = raw_db or DEFAULT_DB_RELATIVE
    if "://" in db_value or Path(db_value).is_absolute():
        db_target = db_value
    else:
        db_target = str(root / db_value)
    database_config = DatabaseConfig(
        path=db_target,
        identity=identity,
        credential_mode=CredentialMode.READ_WRITE,
        replica_path=args.replica,
    )
    with TodoDatabase.open(database_config) as database:
        print(f"schema v{database.schema_version} ready for {database.project_identity.project_id}")

    config_dir.mkdir(parents=True, exist_ok=True)
    payload = {"project_id": identity.project_id, "repository": identity.repository, "db": db_value}
    config_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {config_path}")
    gitignore_path.write_text(SCAFFOLD_GITIGNORE, encoding="utf-8")
    print(f"wrote {gitignore_path}")
    if wrapper_path is not None:
        wrapper_path.parent.mkdir(parents=True, exist_ok=True)
        wrapper_path.write_text(_wrapper_script(identity.project_id, wrapper_rel_path=args.wrapper), encoding="utf-8")
        wrapper_path.chmod(wrapper_path.stat().st_mode | 0o111)
        print(f"wrote {wrapper_path} (executable)")
    _warn_if_git_ignored(config_path, root)
    return 0


_FOREIGN_TRACKER_TABLES = frozenset({"items", "work_units", "item_deps", "meta"})


def _nearest_existing(path: Path) -> Path:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return probe


DoctorCheck = tuple[str, str, str | None]


def _doctor_local_probe(path: Path) -> tuple[DoctorCheck, tuple[str, str] | None]:
    if not path.exists():
        ancestor = _nearest_existing(path.parent)
        if os.access(ancestor, os.W_OK):
            return ("PASS", f"{path} does not exist yet; parent is creatable", "run `todo-db init` to create it"), None
        return ("FAIL", f"{path} does not exist and {ancestor} is not writable", "choose a writable --db path"), None
    try:
        connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return ("FAIL", f"cannot open {path} read-only: {exc}", "check file permissions"), None
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "schema_migrations" not in tables:
            if tables & _FOREIGN_TRACKER_TABLES:
                return ("FAIL", f"{path} contains a different tracker schema", "use a dedicated todo-db path"), None
            return ("WARN", f"{path} exists but has no todo-db schema", "run `todo-db init` to initialize it"), None
        version = int(connection.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] or 0)
        bound = None
        if "project_identity" in tables:
            row = connection.execute(
                "SELECT project_id, repository FROM project_identity WHERE singleton = 1"
            ).fetchone()
            bound = (row[0], row[1]) if row else None
        if version < SCHEMA_VERSION:
            detail = f"{path} schema v{version} behind packaged v{SCHEMA_VERSION}"
            return ("WARN", detail, "behind -- run init to migrate"), bound
        if version > SCHEMA_VERSION:
            return (
                "FAIL",
                f"{path} schema v{version} is ahead of packaged v{SCHEMA_VERSION}",
                "upgrade todo-db",
            ), bound
        return ("PASS", f"{path} schema v{version}", None), bound
    except sqlite3.Error as exc:
        return ("FAIL", f"cannot inspect {path}: {exc}", None), None
    finally:
        connection.close()


def _doctor_hosted_probe(target: str) -> tuple[DoctorCheck, tuple[str, str] | None, bool]:
    """Read-only SELECT probe against the primary. Returns (check, bound identity, auth-classified)."""

    if target.lower().startswith("http://"):
        return ("FAIL", "plaintext http:// is refused for the hosted backend", "use https:// or libsql://"), None, False
    ro_token = os.environ.get("TODO_DB_RO_AUTH_TOKEN", "")
    token = ro_token or os.environ.get("TODO_DB_AUTH_TOKEN", "")
    variable = "TODO_DB_RO_AUTH_TOKEN" if ro_token else "TODO_DB_AUTH_TOKEN"
    if not token:
        detail = "no TODO_DB_RO_AUTH_TOKEN or TODO_DB_AUTH_TOKEN in the environment"
        return ("FAIL", detail, auth_remediation()), None, True
    config = DatabaseConfig(path=target, credential_mode=CredentialMode.READ_ONLY, auth_token=token)
    try:
        connection = connect(config)
    except HostedAuthError as exc:
        return ("FAIL", str(exc), None), None, True
    except (TodoDBError, OSError, ValueError) as exc:
        return ("FAIL", str(exc), None), None, False
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "schema_migrations" not in tables:
            return ("WARN", f"{target} reachable but has no todo-db schema", "run `todo-db init`"), None, False
        version = int(connection.execute("SELECT max(version) AS v FROM schema_migrations").fetchone()["v"] or 0)
        bound = None
        if "project_identity" in tables:
            row = connection.execute(
                "SELECT project_id, repository FROM project_identity WHERE singleton = 1"
            ).fetchone()
            bound = (row["project_id"], row["repository"]) if row else None
        if version < SCHEMA_VERSION:
            detail = f"{target} schema v{version} behind packaged v{SCHEMA_VERSION}"
            return ("WARN", detail, "behind -- run init to migrate"), bound, False
        return ("PASS", f"read-only probe ok: {target} schema v{version}", None), bound, False
    except (sqlite3.Error, ValueError) as exc:
        classified = hosted_error(exc, url=target, token=token, context="read-only probe", token_variable=variable)
        return ("FAIL", str(classified), None), None, isinstance(classified, HostedAuthError)
    finally:
        connection.close()


def _doctor(args: argparse.Namespace) -> int:
    checks: list[dict[str, str]] = []
    auth_failure = False

    def add(name: str, status: str, detail: str, remediation: str | None = None) -> None:
        check = {"name": name, "status": status, "detail": detail}
        if remediation:
            check["remediation"] = remediation
        checks.append(check)

    discovered = None
    config_error: str | None = None
    try:
        discovered = _discover_repo_config()
    except TodoError as exc:
        config_error = str(exc)
    if config_error is not None:
        add(
            "config", "FAIL", config_error, "fix or remove the config; scaffold a fresh one with `todo-db init-project`"
        )
    elif discovered is not None:
        add("config", "PASS", f"discovered {discovered[0]}")
    else:
        add("config", "PASS", f"no {CONFIG_DIRNAME}/{CONFIG_FILENAME} discovered; flags/env/defaults apply")

    identity = None
    identity_error: str | None = None
    try:
        identity = _resolve_identity(args, discovered)
    except TodoError as exc:
        identity_error = str(exc)
    if getattr(args, "project_id", None):
        source = "flags"
    elif os.environ.get("TODO_DB_PROJECT_ID"):
        source = "environment"
    else:
        source = "discovered config"

    target = _resolve_db(getattr(args, "db", None), discovered)
    hosted = DatabaseConfig(path=target).is_hosted
    if hosted:
        db_check, bound, db_auth = _doctor_hosted_probe(target)
    else:
        (db_check, bound), db_auth = _doctor_local_probe(Path(target)), False
    auth_failure = auth_failure or db_auth

    if identity_error is not None:
        add("identity", "FAIL", identity_error, IDENTITY_SOURCES_HINT)
    elif identity is not None:
        add("identity", "PASS", f"{identity.project_id} @ {identity.repository} (source: {source})")
    elif bound is not None:
        add("identity", "PASS", f"no caller-supplied identity; database is bound to {bound[0]} @ {bound[1]}")
    elif db_check[0] == "FAIL":
        add("identity", "WARN", "no identity from flags/env/config and the database probe failed to read a bound one")
    else:
        add(
            "identity",
            "FAIL",
            "no identity from flags/env/config and the database is not bound to one",
            IDENTITY_SOURCES_HINT,
        )
    add("database", *db_check)

    if hosted:
        if shutil.which("turso") is None:
            add(
                "turso-cli", "WARN", "turso CLI not found", "automatic token re-mint unavailable; install the turso CLI"
            )
        else:
            whoami = subprocess.run(["turso", "auth", "whoami"], capture_output=True, text=True, check=False)
            if whoami.returncode != 0:
                add(
                    "turso-cli",
                    "WARN",
                    "turso CLI is logged out",
                    "automatic token re-mint unavailable: run 'turso auth login'",
                )
            else:
                add("turso-cli", "PASS", f"turso auth whoami: {whoami.stdout.strip() or 'ok'}")
        if args.rw:
            rw_config = DatabaseConfig(
                path=target, identity=identity, credential_mode=CredentialMode.READ_WRITE, replica_path=args.replica
            )
            try:
                connect(rw_config).close()
                add("hosted-rw", "PASS", "hosted direct connection succeeded")
            except HostedAuthError as exc:
                auth_failure = True
                add("hosted-rw", "FAIL", str(exc))
            except (TodoDBError, OSError, ValueError, sqlite3.Error) as exc:
                add("hosted-rw", "FAIL", str(exc))

    project_hint = identity.project_id if identity is not None else (bound[0] if bound else None)
    if os.environ.get("TODO_DB_FINDING_DRAFTS_DIR") or project_hint:
        drafts = default_drafts_dir(project_hint or "")
        ancestor = _nearest_existing(drafts)
        if os.access(ancestor, os.W_OK):
            detail = f"{drafts} writable" if drafts.exists() else f"{drafts} creatable (nearest parent {ancestor})"
            add("finding-drafts", "PASS", detail)
        else:
            add(
                "finding-drafts",
                "FAIL",
                f"{drafts}: nearest existing parent {ancestor} is not writable",
                "fix permissions or set TODO_DB_FINDING_DRAFTS_DIR",
            )
    else:
        add(
            "finding-drafts",
            "WARN",
            "project identity unresolved; the default drafts dir is project-scoped",
            "resolve the identity or set TODO_DB_FINDING_DRAFTS_DIR",
        )

    statuses = {check["status"] for check in checks}
    exit_code = 4 if auth_failure else (2 if "FAIL" in statuses else 0)
    if args.json:
        print(json.dumps({"checks": checks, "exit": exit_code}, indent=2, sort_keys=True))
    else:
        for check in checks:
            print(f"{check['status']:4s} {check['name']}: {check['detail']}")
            if check.get("remediation"):
                print(f"     remediation: {check['remediation']}")
    return exit_code


def _run_finding(database: TodoDatabase, args: argparse.Namespace, project_id: str) -> int:
    service = FindingsTracker(database, actor=args.actor)
    command = args.finding_command
    if command == "list":
        listed = service.list_findings(disposition=args.disposition, rank=args.rank)
        if args.json:
            print(json.dumps(listed, indent=2, sort_keys=True))
        else:
            for row in listed:
                print(f"{row['id']:45s} {row['disposition']:11s} {row['finding_kind']:14s} {row['title']}")
    elif command == "show":
        finding = service.get_finding(args.id)
        if args.json:
            print(json.dumps(finding, indent=2, sort_keys=True))
        else:
            _print_finding(finding)
    elif command == "sync":
        result = service.sync_drafts(_drafts_dir(args, project_id))
        print(
            f"synced {len(result['synced'])}, skipped {len(result['skipped'])} (already landed),"
            f" pruned {result['pruned']} old .synced draft(s)"
        )
    elif command == "dismiss":
        service.dismiss(args.id, args.reason)
        print(f"{args.id} dismissed")
    elif command == "triage":
        service.triage(
            args.id,
            urgency=args.urgency,
            breadth=args.breadth,
            confidence=args.confidence,
            reconsider_after=args.reconsider_after,
            disposition=args.disposition,
            reason=args.reason,
        )
        print(f"{args.id} triaged")
    elif command == "link":
        service.link(args.id, kind=args.kind, target_item=args.to_item, target_finding=args.to_finding, note=args.note)
        print(f"linked {args.id} [{args.kind}]")
    elif command == "promote":
        service.promote(
            args.id,
            args.to_item,
            title=args.title,
            priority=args.priority,
            worktree=args.worktree,
            description=args.description,
        )
        print(f"finding {args.id} promoted to {args.to_item}")
    return 0


def _changed_files(base: str | None) -> list[str]:
    from .agent import GitScopeEngine

    return GitScopeEngine().changed_files(base)


def _main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        raw_db = getattr(args, "db", None)
        if args.command == "init-project":
            # init-project writes the config file, so it deliberately resolves
            # identity from flags/env only and never from a discovered config.
            identity = _resolve_identity(args, None)
            if identity is None:
                raise TodoError(
                    "init-project requires an explicit project identity: "
                    "pass --project-id/--repository or set TODO_DB_PROJECT_ID/TODO_DB_REPOSITORY"
                )
            return _init_project(args, identity, raw_db)
        if args.command == "doctor":
            return _doctor(args)
        discovered = _discover_repo_config()
        identity = _resolve_identity(args, discovered)
        args.db = _resolve_db(raw_db, discovered)
        if args.command == "init" and identity is None:
            raise TodoError(f"init requires a project identity and no longer assumes one: {IDENTITY_SOURCES_HINT}")
        if (
            args.command not in {"init", "init-project", "doctor"}
            and discovered is None
            and raw_db is None
            and identity is None
        ):
            raise TodoError(
                "no project boundary discovered: E_NO_PROJECT (run from a repository with .todo-db/config.json or supply --project-id/--repository)"
            )
        project_id = identity.project_id if identity is not None else None
        if args.command == "finding" and args.finding_command in FINDING_OFFLINE_SUBCOMMANDS:
            return _finding_offline(args, project_id)
        mode = _mode_for(args)
        with TodoDatabase.open(_config(args, mode, identity)) as database:
            if project_id is None:
                project_id = database.project_identity.project_id
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
            elif command == "restore-legacy":
                if not args.replace:
                    raise TodoError("restore-legacy replaces current state; pass --replace to confirm")
                database.restore_legacy(json.loads(args.input.read_text(encoding="utf-8")))
                print(f"restored legacy snapshot from {args.input}")
            elif command == "audit":
                print(json.dumps(database.verify_audit(), sort_keys=True))
            elif command == "import-yaml":
                if args.replace:
                    if not _config(args, mode, identity).is_hosted or args.dry_run:
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
            elif command == "update":
                detail = tracker.update_item(
                    args.id,
                    title=args.title,
                    description=args.description,
                    priority=args.priority,
                    worktree=args.worktree,
                    approach=args.approach,
                    category=args.category,
                    add_work=_parse_work(args.add_work),
                    edit_work=_parse_work_edits(args.edit_work),
                    add_verify=_parse_verify(args.add_verify),
                    drop_verify=args.drop_verify,
                    add_scope=[("only_modify", value) for value in args.add_only_modify]
                    + [("do_not_modify", value) for value in args.add_do_not_modify],
                    drop_scope=[("only_modify", value) for value in args.drop_only_modify]
                    + [("do_not_modify", value) for value in args.drop_do_not_modify],
                    add_deps=args.add_needs,
                    drop_deps=args.drop_needs,
                    add_preserves=args.add_preserve,
                    drop_preserves=args.drop_preserve,
                    add_anti_patterns=_parse_anti_pattern_specs(args.add_anti_pattern),
                    drop_anti_patterns=args.drop_anti_pattern,
                    add_prior_art=_parse_prior_art_adds(args.add_prior_art),
                    drop_prior_art=_parse_prior_art_drops(args.drop_prior_art),
                    add_work_needs=_parse_wid_pairs(args.add_work_need, "--add-work-need"),
                    drop_work_needs=_parse_wid_pairs(args.drop_work_need, "--drop-work-need"),
                    reason=args.reason,
                )
                print(f"updated {args.id} ({', '.join(sorted(key for key in detail if key != 'reason'))})")
            elif command == "show":
                if args.fields:
                    item = tracker.get_item(args.id)
                    projected = _project_fields(item, args.fields)
                    if args.json:
                        _output(json.dumps(projected, indent=2, sort_keys=True), args)
                    else:
                        fields_list = [f.strip() for f in args.fields.split(",") if f.strip()]
                        _output(" ".join(str(projected.get(f, "")) for f in fields_list), args)
                elif args.json:
                    _output(json.dumps(tracker.get_item(args.id), indent=2, sort_keys=True), args)
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
                tracker.complete(args.id, args.pr, verification_override_reason=args.override_verification)
                print(f"{args.id} done")
            elif command == "drop":
                tracker.drop(args.id, args.reason)
                print(f"{args.id} dropped")
            elif command == "block":
                tracker.block(args.id, args.reason)
                print(f"{args.id} blocked")
            elif command == "list":
                items = tracker.list_items(state=args.state, worktree=args.worktree, priority=args.priority)
                if getattr(args, "limit", None) is not None:
                    if args.limit < 0:
                        raise TodoError(f"--limit must be non-negative, got {args.limit}")
                    items = items[: args.limit]
                if args.fields:
                    items = [_project_fields(item, args.fields) for item in items]
                if args.json:
                    if getattr(args, "max_bytes", None) is not None:
                        max_b = args.max_bytes
                        if max_b < 20:
                            raise TodoError(f"--max-bytes must be at least 20, got {max_b}")
                        out_items = items
                        emitted = False
                        while out_items:
                            payload = json.dumps(out_items, indent=2, sort_keys=True)
                            if len(payload.encode("utf-8")) + 1 <= max_b:
                                print(payload)
                                emitted = True
                                break
                            out_items = out_items[:-1]
                        if not emitted:
                            print("[]")
                    else:
                        print(json.dumps(items, indent=2, sort_keys=True))
                else:
                    if args.fields:
                        fields_list = [f.strip() for f in args.fields.split(",") if f.strip()]
                        _output("\n".join(" ".join(str(item.get(f, "")) for f in fields_list) for item in items), args)
                    else:
                        _output(
                            "\n".join(
                                f"{item['id']} {item['state']} {item['priority']} {item['worktree']}" for item in items
                            ),
                            args,
                        )
            elif command == "ready":
                items = tracker.ready_items()
                if getattr(args, "limit", None) is not None:
                    if args.limit < 0:
                        raise TodoError(f"--limit must be non-negative, got {args.limit}")
                    items = items[: args.limit]
                if args.fields:
                    items = [_project_fields(item, args.fields) for item in items]
                if args.json:
                    if getattr(args, "max_bytes", None) is not None:
                        max_b = args.max_bytes
                        if max_b < 20:
                            raise TodoError(f"--max-bytes must be at least 20, got {max_b}")
                        out_items = items
                        emitted = False
                        while out_items:
                            payload = json.dumps(out_items, indent=2, sort_keys=True)
                            if len(payload.encode("utf-8")) + 1 <= max_b:
                                print(payload)
                                emitted = True
                                break
                            out_items = out_items[:-1]
                        if not emitted:
                            print("[]")
                    else:
                        print(json.dumps(items, indent=2, sort_keys=True))
                else:
                    if args.fields:
                        fields_list = [f.strip() for f in args.fields.split(",") if f.strip()]
                        _output("\n".join(" ".join(str(item.get(f, "")) for f in fields_list) for item in items), args)
                    else:
                        _output(
                            "\n".join(f"{item['id']} {item['priority']} {item['worktree']}" for item in items), args
                        )
                        open_findings = FindingsTracker(database, actor=args.actor).open_count()
                        drafts = count_unsynced_drafts(_drafts_dir(args, project_id))
                        if open_findings or drafts:
                            _output(
                                f"{open_findings} open finding(s), {drafts} unsynced draft(s) -- todo-db finding candidates",
                                args,
                            )
            elif command == "stats":
                stats = {
                    **tracker.stats(),
                    **FindingsTracker(database, actor=args.actor).stats(),
                    "unsynced_drafts": count_unsynced_drafts(_drafts_dir(args, project_id)),
                }
                print(json.dumps(stats, indent=2, sort_keys=True))
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
                if args.all:
                    snapshots = tracker.load_item_snapshots()
                    configs = tracker.get_all_configs()
                    findings = {item_id: tracker.lint_item(snap, configs) for item_id, snap in snapshots.items()}
                    ids = list(snapshots.keys())
                else:
                    findings = {args.id: tracker.lint(args.id)}
                    ids = [args.id]
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
            elif command == "finding":
                return _run_finding(database, args, project_id)
            else:  # pragma: no cover - argparse constrains command values
                parser.error(f"unsupported command: {command}")
        return 0
    except HostedAuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4
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
