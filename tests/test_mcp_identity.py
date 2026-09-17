"""MCP launch identity precedence and session behavior."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from todo_db.mcp import identity as identity_module
from todo_db.mcp.identity import PrincipalHolder, resolve_identity, sanitize_client_name


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


def test_holder_exposes_server_session_id() -> None:
    assert PrincipalHolder(resolve_identity("actor-a", "session-a")).session_id == "session-a"
    assert PrincipalHolder(resolve_identity(None, "session-b")).session_id == "session-b"


def test_sanitize_client_name_reports_absence_and_bounds() -> None:
    assert sanitize_client_name(None) is None
    assert sanitize_client_name("   ") is None
    assert sanitize_client_name("claude-code") == "claude-code"
    bounded = sanitize_client_name("x" * 200 + "\nunsafe!")
    assert bounded is not None
    assert len(bounded) <= 64
    assert all(ord(char) >= 32 for char in bounded)


def test_mcp_service_wiring_forwards_session_and_client(tmp_path) -> None:
    from todo_db.git_backend import StateRef
    from todo_db.mcp.target import ResolvedTarget
    from todo_db.mcp.tools import _service

    target = ResolvedTarget(
        state_ref=StateRef(remote="example", branch="todo-state"),
        cache_dir=tmp_path / "cache",
        source="test",
        config_path=None,
    )
    holder = PrincipalHolder(resolve_identity("worker-a", "session-a"))
    ctx = SimpleNamespace(
        request_context=SimpleNamespace(
            session=SimpleNamespace(
                client_params=SimpleNamespace(clientInfo=SimpleNamespace(name="claude-code"))
            )
        )
    )
    svc = _service(target, "worker-a", holder, ctx)
    assert svc.worker == "worker-a"
    assert svc.session_id == "session-a"
    assert svc.client_name == "claude-code"
    headless = _service(target, "worker-a", holder, None)
    assert headless.session_id == "session-a"
    assert headless.client_name is None
