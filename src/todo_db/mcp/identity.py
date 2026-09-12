"""Principal and session resolution for the MCP server.

ADR 0006 G5 / plan §8.3:

- The principal (``claimed_by``) comes from ``--actor`` -> ``TODO_DB_ACTOR`` ->
  an instance-scoped value derived from the MCP ``initialize`` client name,
  local user/host, and server session. It is **never** ``default_actor()``.
- The session id is a per-process ``uuid4().hex`` unless ``--session`` overrides
  it. It is logged at startup on stderr and contributes a bounded digest to the
  fallback principal. Independent same-name clients therefore do not collide;
  a deliberate restart can resume by reusing ``--session`` or an explicit actor.
"""

from __future__ import annotations

import getpass
import hashlib
import os
import re
import socket
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")
_UNSAFE_USER_HOST_CHARS = re.compile(r"[^A-Za-z0-9._@-]")
_MAX_NAME_LEN = 64
_MAX_USER_HOST_LEN = 32
_INSTANCE_TAG_LEN = 24
_MAX_SESSION_LEN = 256


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
    value = _UNSAFE_USER_HOST_CHARS.sub("-", f"{user}@{socket.gethostname()}")
    return value[:_MAX_USER_HOST_LEN] or "unknown@unknown"


def principal_from_client_info(client_info: Any, session_id: str) -> str:
    """Derive an instance-scoped fallback principal from an ``initialize`` peer."""

    name = _sanitize_client_name(getattr(client_info, "name", None))
    instance = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:_INSTANCE_TAG_LEN]
    return f"mcp:{name}:{_user_host()}:{instance}"


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
        return principal_from_client_info(client_info, self.session_id)

    def resolved(self, client_info: Any) -> "Identity":
        """Return a copy with the actor pinned from the handshake, if still unset."""

        if self.actor:
            return self
        return Identity(
            session_id=self.session_id,
            actor=principal_from_client_info(client_info, self.session_id),
        )


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
            self.principal = principal_from_client_info(client_info, self._identity.session_id)
        return self.principal


def resolve_identity(actor: str | None, session: str | None) -> Identity:
    """Build the launch-time identity. Does not touch ``default_actor()``."""

    resolved_actor = actor or os.environ.get("TODO_DB_ACTOR") or None
    supplied_session = (session or "").strip()
    if len(supplied_session) > _MAX_SESSION_LEN or any(ord(char) < 32 for char in supplied_session):
        raise ValueError(f"session id must be one line of at most {_MAX_SESSION_LEN} characters")
    session_id = supplied_session or uuid4().hex
    return Identity(session_id=session_id, actor=resolved_actor)
