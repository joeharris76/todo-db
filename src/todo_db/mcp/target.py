"""Project / database target resolution, pinned for the server process lifetime.

Precedence (plan §4), highest first:

1. ``--config`` / ``--db`` / ``--repo-root`` launch flags.
2. ``TODO_DB_CONFIG`` / ``TODO_DB_URL`` / ``TODO_DB_PATH`` environment.
3. Upward discovery from ``--repo-root`` (default: server cwd) for
   ``.todo-db/config.json``.

This is a self-contained reimplementation of the CLI's ``_discover_repo_config``
walk so the MCP package does not import a private CLI function.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from ..errors import TodoError
from ..models import CredentialMode, DatabaseConfig, ProjectIdentity

CONFIG_DIRNAME = ".todo-db"
CONFIG_FILENAME = "config.json"
DEFAULT_DB_RELATIVE = f"{CONFIG_DIRNAME}/standalone.sqlite"


@dataclass(frozen=True)
class ResolvedTarget:
    db_target: str
    repo_root: Path
    identity: ProjectIdentity | None
    source: str
    config_path: Path | None

    def database_config(self, mode: CredentialMode) -> DatabaseConfig:
        return DatabaseConfig(path=self.db_target, identity=self.identity, credential_mode=mode)

    @property
    def is_hosted(self) -> bool:
        return self.database_config(CredentialMode.READ_ONLY).is_hosted


def _git_root(path: Path) -> Path | None:
    probe = path.resolve()
    while True:
        if (probe / ".git").exists():
            return probe
        if probe.parent == probe:
            return None
        probe = probe.parent


def _load_config(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TodoError(f"invalid repo config {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise TodoError(f"invalid repo config {path}: expected a JSON object")
    for key in ("project_id", "repository", "db"):
        if key in payload and (not isinstance(payload[key], str) or not payload[key].strip()):
            raise TodoError(f"invalid repo config {path}: {key!r} must be a non-empty string")
    return payload


def _discover_config(repo_root: Path) -> tuple[Path, dict] | None:
    current = repo_root.resolve()
    git_root = _git_root(current)
    home = Path.home().resolve()
    for candidate in (current, *current.parents):
        path = candidate / CONFIG_DIRNAME / CONFIG_FILENAME
        if path.is_file():
            return path, _load_config(path)
        if git_root is not None and candidate == git_root:
            break
        if candidate == home:
            break
    return None


def _identity_from(payload: dict) -> ProjectIdentity | None:
    project_id = payload.get("project_id") or os.environ.get("TODO_DB_PROJECT_ID")
    repository = payload.get("repository") or os.environ.get("TODO_DB_REPOSITORY")
    if project_id and repository:
        return ProjectIdentity(project_id=project_id, repository=repository)
    return None


def _config_root(config_path: Path) -> Path:
    return config_path.parent.parent


def _db_from_config(config_path: Path, payload: dict) -> str:
    target = payload.get("db")
    root = _config_root(config_path)
    if target is None:
        return str(root / DEFAULT_DB_RELATIVE)
    if "://" in target or Path(target).is_absolute():
        return target
    return str(root / target)


def resolve_target(
    *,
    config: str | None = None,
    db: str | None = None,
    repo_root: str | None = None,
) -> ResolvedTarget:
    root = Path(repo_root).expanduser().resolve() if repo_root else Path.cwd().resolve()

    # Tier 1/2: an explicit config file (flag or env).
    config_ref = config or os.environ.get("TODO_DB_CONFIG")
    if config_ref:
        path = Path(config_ref).expanduser()
        if not path.is_file():
            raise TodoError(f"--config/TODO_DB_CONFIG points to a missing file: {path}")
        payload = _load_config(path)
        explicit_db = db or os.environ.get("TODO_DB_URL") or os.environ.get("TODO_DB_PATH")
        db_target = explicit_db or _db_from_config(path, payload)
        return ResolvedTarget(
            db_target=db_target,
            repo_root=_config_root(path) if repo_root is None else root,
            identity=_identity_from(payload),
            source="config" if not explicit_db else "flag/env db + config identity",
            config_path=path,
        )

    # Tier 1/2: an explicit db (flag or env), no config file.
    explicit_db = db or os.environ.get("TODO_DB_URL") or os.environ.get("TODO_DB_PATH")
    if explicit_db:
        return ResolvedTarget(
            db_target=explicit_db,
            repo_root=root,
            identity=_identity_from({}),
            source="flag" if db else "env",
            config_path=None,
        )

    # Tier 3: upward discovery from repo_root.
    discovered = _discover_config(root)
    if discovered is not None:
        path, payload = discovered
        return ResolvedTarget(
            db_target=_db_from_config(path, payload),
            repo_root=root,
            identity=_identity_from(payload),
            source="discovery",
            config_path=path,
        )

    return ResolvedTarget(
        db_target=str(root / DEFAULT_DB_RELATIVE),
        repo_root=root,
        identity=_identity_from({}),
        source="default",
        config_path=None,
    )
