"""MCP launch identity precedence and session behavior."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from todo_db.mcp import identity as identity_module
from todo_db.mcp.identity import PrincipalHolder, resolve_identity


def test_explicit_actor_beats_environment(monkeypatch) -> None:
    monkeypatch.setenv("TODO_DB_ACTOR", "environment-worker")
    identity = resolve_identity("flag-worker", "session-a")
    assert identity.actor == "flag-worker"
    assert identity.session_id == "session-a"


def test_environment_actor_beats_client_derived_principal(monkeypatch) -> None:
    monkeypatch.setenv("TODO_DB_ACTOR", "environment-worker")
    holder = PrincipalHolder(resolve_identity(None, "session-a"))
    assert holder.ensure(SimpleNamespace(name="same-client")) == "environment-worker"


def test_default_principal_is_stable_per_session_and_distinct_between_sessions(monkeypatch) -> None:
    monkeypatch.delenv("TODO_DB_ACTOR", raising=False)
    first = resolve_identity(None, None)
    second = resolve_identity(None, None)
    client = SimpleNamespace(name="same-client")
    assert first.principal(client) == first.principal(client)
    assert first.principal(client) != second.principal(client)
    assert first.session_id != second.session_id


def test_explicit_session_recreates_default_principal(monkeypatch) -> None:
    monkeypatch.delenv("TODO_DB_ACTOR", raising=False)
    client = SimpleNamespace(name="same-client")
    first = resolve_identity(None, "resume-session")
    second = resolve_identity(None, "resume-session")
    assert first.principal(client) == second.principal(client)


def test_generated_principal_is_bounded_and_single_line(monkeypatch) -> None:
    monkeypatch.delenv("TODO_DB_ACTOR", raising=False)
    monkeypatch.setattr(identity_module.getpass, "getuser", lambda: "u" * 200)
    monkeypatch.setattr(identity_module.socket, "gethostname", lambda: "h" * 200)
    principal = resolve_identity(None, "session").principal(SimpleNamespace(name="x" * 200 + "\nunsafe"))
    assert principal is not None
    assert len(principal) <= 128
    assert all(ord(char) >= 32 for char in principal)


def test_session_override_rejects_log_injection() -> None:
    with pytest.raises(ValueError, match="one line"):
        resolve_identity(None, "session\nforged log")
