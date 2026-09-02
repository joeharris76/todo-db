"""Principal and session resolution for the MCP server.

ADR 0006 G5 / plan §8.3:

- The principal (``claimed_by``) comes from ``--actor`` -> ``TODO_DB_ACTOR`` ->
  ``mcp:<clientInfo.name>:<user>@<host>`` derived from the MCP ``initialize``
  handshake. It is **never** ``default_actor()`` (which would treat a session id
  as a stable principal, breaking ADR 0003 §2.1/§2.2).
- The session id is a per-process ``uuid4().hex`` unless ``--session`` overrides
  it. It is logged at startup on stderr and passed on every ``take`` so a
  restarted server re-adopts its own claim via same-principal adoption.
"""

from __future__ import annotations

import getpass
import os
import re
import socket
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")
_MAX_NAME_LEN = 64


def _sanitize_client_name(raw: Any) -> str:
    """Normalise an untrusted ``clientInfo.name`` into a principal-safe token."""

    text = (raw or "").strip() if isinstance(raw, str) else ""
    text = _UNSAFE_NAME_CHARS.sub("-", text)[:_MAX_NAME_LEN]
    return text or "unknown"


def _user_host() -> str:
    try:
        user = getpass.getuser()
    except Exception:  # pragma: no cover - exotic environments without a username
        user = "unknown"
    return f"{user}@{socket.gethostname()}"


def principal_from_client_info(client_info: Any) -> str:
    """Derive ``mcp:<clientInfo.name>:<user>@<host>`` from an ``initialize`` peer."""

    name = _sanitize_client_name(getattr(client_info, "name", None))
    return f"mcp:{name}:{_user_host()}"


@dataclass(frozen=True)
class Identity:
    """Resolved server identity. ``actor`` is ``None`` until ``initialize``."""

    session_id: str
    actor: str | None = None

    @property
    def actor_pending(self) -> bool:
        return self.actor is None

    def principal(self, client_info: Any | None = None) -> str | None:
        """The stable principal, or ``None`` if no actor and no handshake yet."""

        if self.actor:
            return self.actor
        if client_info is None:
            return None
        return principal_from_client_info(client_info)

    def resolved(self, client_info: Any) -> "Identity":
        """Return a copy with the actor pinned from the handshake, if still unset."""

        if self.actor:
            return self
        return Identity(session_id=self.session_id, actor=principal_from_client_info(client_info))


class PrincipalHolder:
    """Mutable slot for the resolved principal.

    ``LaunchConfig`` / :class:`Identity` are frozen, so the actor derived from the
    first ``initialize`` handshake needs somewhere to live. Created at
    ``build_server`` time and captured by the tool closures; the first tool call
    pins the principal if ``--actor`` / ``TODO_DB_ACTOR`` left it unset.
    """

    def __init__(self, identity: Identity) -> None:
        self._identity = identity
        self.principal: str | None = identity.actor

    @property
    def pending(self) -> bool:
        return self.principal is None

    def ensure(self, client_info: Any | None) -> str | None:
        if self.principal is None and client_info is not None:
            self.principal = principal_from_client_info(client_info)
        return self.principal


def resolve_identity(actor: str | None, session: str | None) -> Identity:
    """Build the launch-time identity. Does not touch ``default_actor()``."""

    resolved_actor = actor or os.environ.get("TODO_DB_ACTOR") or None
    session_id = (session or "").strip() or uuid4().hex
    return Identity(session_id=session_id, actor=resolved_actor)
