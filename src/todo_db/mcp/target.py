"""State target resolution, pinned for the server process lifetime.

Resolution order lives in :func:`todo_db.git_backend.resolve_ref`
(flag > env > ``.todo-db/config.json`` discovery); this module adds the
state cache directory (one Git object store per state branch). Task state lives outside the code worktree.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ..git_backend import StateRef, default_cache_dir, resolve_ref


@dataclass(frozen=True)
class ResolvedTarget:
    state_ref: StateRef
    cache_dir: Path
    source: str
    config_path: Path | None

    def source_text(self) -> str:
        """How the target was resolved, in words an agent can read."""
        if self.source == "flag":
            return "launch flags (--state-remote)"
        if self.source == "env":
            return "environment (TODO_DB_STATE_REMOTE)"
        label = "config file" if self.source == "config" else "discovered config file"
        return f"{label} {self.config_path}" if self.config_path else label

    def describe(self) -> str:
        """One log line: the redacted store, how it was resolved, and the cache."""
        return f"{self.state_ref.display} (source={self.source}, cache={self.cache_dir})"


def _default_cache() -> Path:
    return default_cache_dir()


def resolve_target(
    *,
    state_remote: str | None = None,
    state_branch: str | None = None,
    cache_dir: str | None = None,
    repo_root: str | None = None,
    config: str | None = None,
) -> ResolvedTarget:
    state_ref, config_path = resolve_ref(
        remote=state_remote,
        branch=state_branch,
        config=config,
        repo_root=repo_root,
    )
    if state_remote or os.environ.get("TODO_DB_STATE_REMOTE"):
        source = "flag" if state_remote else "env"
    elif config or os.environ.get("TODO_DB_CONFIG"):
        source = "config"
    else:
        source = "discovery"
    cache = Path(cache_dir).expanduser() if cache_dir else _default_cache()
    return ResolvedTarget(state_ref=state_ref, cache_dir=cache, source=source, config_path=config_path)
