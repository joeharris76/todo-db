"""Configuration and identity models for the tracker boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TypeAlias


DatabaseTarget: TypeAlias = Path | str


class CredentialMode(str, Enum):
    """The local equivalent of hosted read-write/read-only credentials."""

    READ_WRITE = "read-write"
    READ_ONLY = "read-only"


@dataclass(frozen=True, slots=True)
class ProjectIdentity:
    """Immutable identity that must match the database before use."""

    project_id: str
    repository: str

    def __post_init__(self) -> None:
        if not self.project_id.strip():
            raise ValueError("project_id must not be empty")
        if not self.repository.strip():
            raise ValueError("repository must not be empty")


@dataclass(frozen=True, slots=True)
class DatabaseConfig:
    """Connection configuration for one project's physical database."""

    path: DatabaseTarget
    identity: ProjectIdentity
    credential_mode: CredentialMode = CredentialMode.READ_WRITE
    timeout: float = 5.0
    replica_path: Path | None = None
    auth_token: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if isinstance(self.path, str):
            target = self.path.strip()
            if not target:
                raise ValueError("path must not be empty")
            if "://" not in target:
                target = Path(target)
        else:
            target = Path(self.path)
        object.__setattr__(self, "path", target)
        if self.replica_path is not None:
            object.__setattr__(self, "replica_path", Path(self.replica_path))

    @property
    def is_hosted(self) -> bool:
        return isinstance(self.path, str) and self.path.lower().startswith(("libsql://", "https://", "http://"))
