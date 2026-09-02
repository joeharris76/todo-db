"""Command-line boundary for the standalone tracker."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .audit import canonical_json
from .backends import ResolvedCredential, connect, hosted_error, resolve_credential
from .database import SCHEMA_VERSION, TodoDatabase
from .database import TOOL_VERSION
from .errors import HostedAuthError, TodoDBError, TodoError
from .findings import (
    FindingsTracker,
    default_drafts_dir,
)
from .models import CredentialMode, DatabaseConfig, ProjectIdentity
from .tracker import TodoTracker

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
  2  generic error, or legacy-safe auth failure before the v2 contract is negotiated
  4  hosted authentication failure under TODO_DB_AUTH_CONTRACT=v2: set a valid bounded
     TODO_DB_AUTH_TOKEN (or TODO_DB_RO_AUTH_TOKEN for reads), or provision one into
     your secret store and point TODO_DB_CREDENTIAL_COMMAND at it, then retry
     (docs/operations/hosted-credentials.md, Provision once)"""


def _auth_contract_v2() -> bool:
    return os.environ.get("TODO_DB_AUTH_CONTRACT") == "v2"


def _legacy_auth_warning() -> str:
    return (
        "v2 auth exit contract not negotiated; returning legacy-safe exit 2 so an older wrapper cannot "
        "mint credentials (run through a v2 wrapper or set TODO_DB_AUTH_CONTRACT=v2 for direct automation)"
    )


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
    parser.add_argument("--actor", help="audit actor identity")
    parser.add_argument("--project-id", default=argparse.SUPPRESS)
    parser.add_argument("--repository", default=argparse.SUPPRESS)
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

    refresh_wrapper = sub.add_parser(
        "refresh-wrapper", help="safely replace a recognized generated wrapper without changing project config"
    )
    refresh_wrapper.add_argument(
        "--wrapper",
        default=None,
        metavar="PATH",
        help=f"wrapper path relative to the project root (default: config value or {DEFAULT_WRAPPER_RELATIVE})",
    )

    doctor = sub.add_parser("doctor", help="read-only preflight: config, identity, database, auth, drafts dir")
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument("--rw", action="store_true", help="also probe the hosted read-write connection")
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

    complete = sub.add_parser("complete", help="complete an item after running its verification ladder")
    complete.add_argument("id")
    complete.add_argument("--pr", type=int)
    complete.add_argument(
        "--override-verification",
        metavar="REASON",
        help="skip the verification ladder and record this override reason in the completion audit event",
    )
    _identity_args(complete)

    sweep = sub.add_parser("sweep-stale", help="release expired claims")
    sweep.add_argument("--ttl-hours", type=float, default=24.0)
    _identity_args(sweep)

    migrate = sub.add_parser("migrate", help="apply pending migrations")
    _identity_args(migrate)

    config = sub.add_parser("config", help="get or set tracker configuration")
    config.add_argument("key", nargs="?")
    config.add_argument("value", nargs="?")
    _identity_args(config)

    finding = sub.add_parser("finding", help="findings domain: sync drafts into tracker")
    finding_sub = finding.add_subparsers(dest="finding_command", required=True)
    fsync = finding_sub.add_parser("sync", help="land drafts into the tracker (the credentialed landing step)")
    fsync.add_argument("--drafts-dir", type=Path)
    _identity_args(fsync)

    return parser


def _config(args: argparse.Namespace, mode: CredentialMode, identity: ProjectIdentity | None) -> DatabaseConfig:
    return DatabaseConfig(
        path=args.db,
        identity=identity,
        credential_mode=mode,
    )


def _mode_for(args: argparse.Namespace) -> CredentialMode:
    if args.command == "finding":
        return CredentialMode.READ_WRITE
    return (
        CredentialMode.READ_WRITE
        if args.command
        in {
            "init",
            "init-project",
            "import-yaml",
            "restore",
            "restore-legacy",
            "complete",
            "sweep-stale",
            "migrate",
            "config",
        }
        else CredentialMode.READ_ONLY
    )


def _drafts_dir(args: argparse.Namespace, project_id: str | None) -> Path:
    supplied = getattr(args, "drafts_dir", None)
    if supplied:
        return Path(supplied).expanduser()
    if project_id is None and not os.environ.get("TODO_DB_FINDING_DRAFTS_DIR"):
        raise TodoError(f"the default drafts dir is project-scoped; pass --drafts-dir or {IDENTITY_SOURCES_HINT}")
    return default_drafts_dir(project_id or "")


WRAPPER_VERSION_MARKER = "# todo-db-wrapper: v2"
_GENERATED_WRAPPER_SIGNATURE = "TODO tracker entry point. Routes every subcommand to the"


def _normalized_wrapper_relative(value: str) -> Path:
    candidate = Path(os.path.normpath(value))
    if candidate.is_absolute():
        raise TodoError("wrapper path must be relative to the project root")
    if candidate == Path(".") or not candidate.name or candidate.parts[0] == "..":
        raise TodoError("wrapper path must name a file inside the project root")
    return candidate


def _wrapper_path_in_root(root: Path, relative: Path) -> Path:
    root = root.resolve()
    candidate = root
    for part in relative.parts:
        candidate /= part
        if candidate.is_symlink():
            raise TodoError(f"wrapper path traverses a symlink: {candidate}")
    try:
        candidate.resolve().relative_to(root)
    except ValueError:
        raise TodoError("wrapper path escapes the project root") from None
    return candidate


def _wrapper_script(project_id: str, wrapper_rel_path: str = DEFAULT_WRAPPER_RELATIVE) -> str:
    normalized = _normalized_wrapper_relative(wrapper_rel_path)
    parts = normalized.parent.parts
    upward = "/".join(".." for _ in parts) if parts else "."
    return f"""#!/usr/bin/env bash
{WRAPPER_VERSION_MARKER}
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
# Authentication is data-plane only. This wrapper never calls the Turso CLI,
# mints or stores tokens, or retries a failed command.
#
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${{BASH_SOURCE[0]}}")/{upward}" && pwd)"
export TODO_DB_CONFIG="${{TODO_DB_CONFIG:-$REPO_ROOT/.todo-db/config.json}}"
export TODO_DB_AUTH_CONTRACT=v2

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

status=0
run_todo_db "$@" || status=$?
if [ "$status" -eq 4 ]; then
  echo "todo: hosted authentication failed; set TODO_DB_AUTH_TOKEN or point TODO_DB_CREDENTIAL_COMMAND at your secret store, then retry (see docs/operations/hosted-credentials.md, Provision once; automatic token minting is disabled)" >&2
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

    root = Path.cwd().resolve()
    config_dir = root / CONFIG_DIRNAME
    config_path = config_dir / CONFIG_FILENAME
    gitignore_path = config_dir / ".gitignore"
    wrapper_relative = _normalized_wrapper_relative(args.wrapper) if args.wrapper else None
    wrapper_path = _wrapper_path_in_root(root, wrapper_relative) if wrapper_relative is not None else None
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
    )
    with TodoDatabase.open(database_config) as database:
        print(f"schema v{database.schema_version} ready for {database.project_identity.project_id}")

    config_dir.mkdir(parents=True, exist_ok=True)
    payload = {"project_id": identity.project_id, "repository": identity.repository, "db": db_value}
    if wrapper_relative is not None:
        payload["wrapper"] = wrapper_relative.as_posix()
    config_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {config_path}")
    gitignore_path.write_text(SCAFFOLD_GITIGNORE, encoding="utf-8")
    print(f"wrote {gitignore_path}")
    if wrapper_path is not None and wrapper_relative is not None:
        wrapper_path.parent.mkdir(parents=True, exist_ok=True)
        wrapper_path.write_text(
            _wrapper_script(identity.project_id, wrapper_rel_path=wrapper_relative.as_posix()), encoding="utf-8"
        )
        wrapper_path.chmod(wrapper_path.stat().st_mode | 0o111)
        print(f"wrote {wrapper_path} (executable)")
    _warn_if_git_ignored(config_path, root)
    return 0


def _refresh_wrapper(args: argparse.Namespace) -> int:
    """Replace only a recognized generated wrapper; leave every other scaffold file untouched."""

    discovered = _discover_repo_config()
    if discovered is None:
        raise TodoError(f"refresh-wrapper requires a discovered {CONFIG_DIRNAME}/{CONFIG_FILENAME}")
    config_path, payload = discovered
    root = _config_root(config_path).resolve()
    configured = str(args.wrapper or payload.get("wrapper") or DEFAULT_WRAPPER_RELATIVE)
    candidate = _normalized_wrapper_relative(configured)
    relative = candidate.as_posix()
    wrapper_path = _wrapper_path_in_root(root, candidate)
    if not wrapper_path.is_file():
        raise TodoError(f"no existing wrapper to refresh: {wrapper_path}")
    current = wrapper_path.read_text(encoding="utf-8")
    if _GENERATED_WRAPPER_SIGNATURE not in current:
        raise TodoError(f"refusing to replace unrecognized wrapper: {wrapper_path}")

    project_id = str(payload.get("project_id") or "").strip()
    if not project_id:
        raise TodoError(f"{config_path} has no project_id for the generated wrapper")
    replacement = _wrapper_script(project_id, wrapper_rel_path=relative)
    if current == replacement:
        print(f"wrapper already current: {wrapper_path}")
        return 0

    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{wrapper_path.name}.", dir=wrapper_path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(replacement)
        temporary.chmod(wrapper_path.stat().st_mode | 0o111)
        os.replace(temporary, wrapper_path)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"refreshed wrapper {wrapper_path}")
    return 0


def _doctor_wrapper_check(discovered: tuple[Path, dict[str, Any]] | None) -> DoctorCheck | None:
    if discovered is None:
        return None
    config_path, payload = discovered
    root = _config_root(config_path).resolve()
    configured = str(payload.get("wrapper") or DEFAULT_WRAPPER_RELATIVE)
    try:
        candidate = _normalized_wrapper_relative(configured)
        wrapper_path = _wrapper_path_in_root(root, candidate)
    except TodoError as exc:
        return ("FAIL", f"unsafe wrapper path in {config_path}: {configured}", str(exc))
    if not wrapper_path.exists():
        if payload.get("wrapper"):
            return ("FAIL", f"configured wrapper is missing: {wrapper_path}", "restore it or remove the wrapper key")
        return None
    if wrapper_path.is_symlink():
        return ("FAIL", f"configured wrapper is a symlink: {wrapper_path}", "replace it with a regular generated file")
    try:
        content = wrapper_path.read_text(encoding="utf-8")
    except OSError as exc:
        return ("FAIL", f"cannot inspect wrapper {wrapper_path}: {exc}", "fix wrapper permissions")
    if WRAPPER_VERSION_MARKER in content:
        return ("PASS", f"v2 wrapper: {wrapper_path}", None)
    if _GENERATED_WRAPPER_SIGNATURE in content:
        return ("FAIL", f"legacy generated wrapper: {wrapper_path}", "run `todo-db refresh-wrapper`")
    return ("WARN", f"unrecognized wrapper left unmanaged: {wrapper_path}", None)


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


def _credential_metadata(
    credential: ResolvedCredential | None, error: HostedAuthError | None = None
) -> dict[str, str]:
    metadata: dict[str, str] = {}
    if credential is not None:
        metadata.update(source=credential.source, capability=credential.capability)
    if error is not None and error.code:
        metadata["code"] = error.code
    return metadata


def _doctor_hosted_probe(
    target: str,
) -> tuple[DoctorCheck, tuple[str, str] | None, bool, dict[str, str]]:
    """Read-only primary probe with shared credential resolution and non-secret provenance."""

    if target.lower().startswith("http://"):
        check = ("FAIL", "plaintext http:// is refused for the hosted backend", "use https:// or libsql://")
        return check, None, False, {}
    config = DatabaseConfig(path=target, credential_mode=CredentialMode.READ_ONLY)
    try:
        credential = resolve_credential(config)
    except HostedAuthError as exc:
        metadata = {
            "source": "TODO_DB_RO_AUTH_TOKEN|TODO_DB_AUTH_TOKEN|TODO_DB_CREDENTIAL_COMMAND",
            "capability": "read-only requested",
            **_credential_metadata(None, exc),
        }
        return ("FAIL", str(exc), None), None, True, metadata
    try:
        connection = connect(config, credential=credential)
    except HostedAuthError as exc:
        return ("FAIL", str(exc), None), None, True, _credential_metadata(credential, exc)
    except (TodoDBError, OSError, ValueError) as exc:
        return ("FAIL", str(exc), None), None, False, _credential_metadata(credential)
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        metadata = _credential_metadata(credential)
        if "schema_migrations" not in tables:
            check = ("WARN", f"{target} reachable but has no todo-db schema", "run `todo-db init`")
            return check, None, False, metadata
        version = int(connection.execute("SELECT max(version) AS v FROM schema_migrations").fetchone()["v"] or 0)
        bound = None
        if "project_identity" in tables:
            row = connection.execute(
                "SELECT project_id, repository FROM project_identity WHERE singleton = 1"
            ).fetchone()
            bound = (row["project_id"], row["repository"]) if row else None
        if version < SCHEMA_VERSION:
            detail = f"{target} schema v{version} behind packaged v{SCHEMA_VERSION}"
            return ("WARN", detail, "behind -- run init to migrate"), bound, False, metadata
        return ("PASS", f"read-only probe ok: {target} schema v{version}", None), bound, False, metadata
    except (sqlite3.Error, ValueError) as exc:
        classified = hosted_error(exc, url=target, credential=credential, context="read-only probe")
        auth_error = classified if isinstance(classified, HostedAuthError) else None
        metadata = _credential_metadata(credential, auth_error)
        return ("FAIL", str(classified), None), None, auth_error is not None, metadata
    finally:
        connection.close()


def _doctor(args: argparse.Namespace) -> int:
    checks: list[dict[str, str]] = []
    auth_failure = False

    def add(
        name: str,
        status: str,
        detail: str,
        remediation: str | None = None,
        **metadata: str,
    ) -> None:
        check = {"name": name, "status": status, "detail": detail, **metadata}
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
    wrapper_check = _doctor_wrapper_check(discovered)
    if wrapper_check is not None:
        add("wrapper", *wrapper_check)

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
        db_check, bound, db_auth, db_metadata = _doctor_hosted_probe(target)
    else:
        (db_check, bound), db_auth, db_metadata = _doctor_local_probe(Path(target)), False, {}
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
    add("database", *db_check, **db_metadata)

    if hosted and args.rw:
        rw_config = DatabaseConfig(
            path=target, identity=identity, credential_mode=CredentialMode.READ_WRITE
        )
        rw_credential: ResolvedCredential | None = None
        try:
            rw_credential = resolve_credential(rw_config)
            connect(rw_config, credential=rw_credential).close()
            add("hosted-rw", "PASS", "hosted direct connection succeeded", **_credential_metadata(rw_credential))
        except HostedAuthError as exc:
            auth_failure = True
            add("hosted-rw", "FAIL", str(exc), **_credential_metadata(rw_credential, exc))
        except (TodoDBError, OSError, ValueError, sqlite3.Error) as exc:
            add("hosted-rw", "FAIL", str(exc), **_credential_metadata(rw_credential))

    if auth_failure and not _auth_contract_v2():
        add(
            "auth-contract",
            "WARN",
            _legacy_auth_warning(),
            "refresh a generated wrapper with `todo-db refresh-wrapper`, or explicitly negotiate v2",
        )

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
    exit_code = 4 if auth_failure and _auth_contract_v2() else (2 if "FAIL" in statuses else 0)
    if args.json:
        print(json.dumps({"checks": checks, "exit": exit_code}, indent=2, sort_keys=True))
    else:
        for check in checks:
            print(f"{check['status']:4s} {check['name']}: {check['detail']}")
            if check.get("source"):
                print(f"     credential: {check['source']} ({check.get('capability', 'unknown')})")
            if check.get("code"):
                print(f"     code: {check['code']}")
            if check.get("remediation"):
                print(f"     remediation: {check['remediation']}")
    return exit_code


def _run_finding(database: TodoDatabase, args: argparse.Namespace, project_id: str) -> int:
    service = FindingsTracker(database, actor=args.actor)
    command = args.finding_command
    if command == "sync":
        result = service.sync_drafts(_drafts_dir(args, project_id))
        print(
            f"synced {len(result['synced'])}, skipped {len(result['skipped'])} (already landed),"
            f" pruned {result['pruned']} old .synced draft(s)"
        )
        return 0
    raise TodoError(f"unsupported finding command: {command}")


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
        if args.command == "refresh-wrapper":
            return _refresh_wrapper(args)
        discovered = _discover_repo_config()
        identity = _resolve_identity(args, discovered)
        args.db = _resolve_db(raw_db, discovered)
        if args.command == "init" and identity is None:
            raise TodoError(f"init requires a project identity and no longer assumes one: {IDENTITY_SOURCES_HINT}")
        mode = _mode_for(args)
        is_env_db = bool(os.environ.get("TODO_DB_PATH") or os.environ.get("TODO_DB_URL"))
        if (
            args.command not in {"init", "init-project", "doctor"}
            and discovered is None
            and raw_db is None
            and identity is None
            and not is_env_db
        ):
            raise TodoError(
                "no project boundary discovered: E_NO_PROJECT (run from a repository with .todo-db/config.json or supply --project-id/--repository)"
            )
        if (
            args.command not in {"init", "init-project", "doctor"}
            and is_env_db
            and discovered is None
            and raw_db is None
            and identity is None
            and mode is CredentialMode.READ_WRITE
        ):
            raise TodoError(
                "refusing to write to database from TODO_DB_PATH/TODO_DB_URL without a project boundary: "
                "unset TODO_DB_PATH/TODO_DB_URL or run from a repository with .todo-db/config.json "
                f"or supply --project-id/--repository ({IDENTITY_SOURCES_HINT}); "
                "use --db to set the target explicitly when intentional"
            )
        project_id = identity.project_id if identity is not None else None
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
            elif command == "complete":
                tracker.complete(args.id, args.pr, verification_override_reason=args.override_verification)
                print(f"{args.id} done")
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
        label = f"error [{exc.code}]" if exc.code else "error"
        print(f"{label}: {exc}", file=sys.stderr)
        if _auth_contract_v2():
            return 4
        print(f"warning: {_legacy_auth_warning()}", file=sys.stderr)
        return 2
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
