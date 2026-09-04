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
    """Connection configuration for one project's physical database.

    ``identity`` may be ``None`` for callers that supply no identity at all;
    the binding check then defers to the identity already bound in the
    database, and binding a fresh database is refused.
    """

    path: DatabaseTarget
    identity: ProjectIdentity | None = None
    credential_mode: CredentialMode = CredentialMode.READ_WRITE
    timeout: float = 5.0
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

    #: Network schemes routed to the hosted backend, cleartext ones included so
    #: `backends._secure_url` can refuse them with a real message. Leaving one
    #: out would route a remote URL to the local SQLite backend and open it as
    #: a filename -- a confusing failure rather than a safe one.
    HOSTED_SCHEMES = ("libsql://", "https://", "http://", "wss://", "ws://")

    #: The subset that carries the auth token over an encrypted transport.
    #: `backends._secure_url` accepts only these; it is a strict subset, which
    #: `tests/test_hosted_backend.py` pins.
    SECURE_SCHEMES = ("libsql://", "https://", "wss://")

    @property
    def is_hosted(self) -> bool:
        return isinstance(self.path, str) and self.path.lower().startswith(self.HOSTED_SCHEMES)
