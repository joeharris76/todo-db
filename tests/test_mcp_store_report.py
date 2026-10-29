"""The MCP server reports which state store it is bound to, without credentials."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import anyio
import mcp.types as types
import pytest
from mcp.shared.memory import create_connected_server_and_client_session as connect

from todo_db.git_backend import StateRef, bootstrap, redact_remote
from todo_db.mcp.instructions import INSTRUCTIONS, render_instructions
from todo_db.mcp.server import build_parser, build_server, resolve_launch_config
from todo_db.mcp.target import ResolvedTarget

TOKEN = "ghp_secret_token_value"


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        (f"https://user:{TOKEN}@github.com/o/r.git", "https://github.com/o/r.git"),
        (f"https://{TOKEN}@github.com/o/r.git", "https://github.com/o/r.git"),
        (f"https://user:{TOKEN}@host.example:8443/o/r.git?x=1#frag", "https://host.example:8443/o/r.git"),
        ("ssh://git@github.com/o/r.git", "ssh://github.com/o/r.git"),
        ("https://github.com/o/r.git", "https://github.com/o/r.git"),
        ("git@github.com:o/r.git", "git@github.com:o/r.git"),
        ("/tmp/state.git", "/tmp/state.git"),
        ("file:///tmp/state.git", "file:///tmp/state.git"),
    ],
)
def test_redact_remote_removes_credentials_only(remote: str, expected: str) -> None:
    assert redact_remote(remote) == expected


def _target(source: str, remote: str = "https://github.com/BenchBox-dev/BenchBox.git", config: Path | None = None):
    return ResolvedTarget(
        state_ref=StateRef(remote=remote, branch="todo-state"),
        cache_dir=Path("/tmp/cache"),
        source=source,
        config_path=config,
    )


@pytest.mark.parametrize(
    ("source", "config", "expected"),
    [
        ("flag", None, "launch flags"),
        ("env", None, "environment (TODO_DB_STATE_REMOTE)"),
        ("config", Path("/work/.todo-db/config.json"), "config file /work/.todo-db/config.json"),
        ("discovery", Path("/work/.todo-db/config.json"), "discovered config file /work/.todo-db/config.json"),
    ],
)
def test_render_instructions_names_store_and_resolution(source: str, config: Path | None, expected: str) -> None:
    text = render_instructions(_target(source, config=config), "worker-1")

    assert "## Bound store" in text
    assert "Remote: https://github.com/BenchBox-dev/BenchBox.git" in text
    assert "Branch: todo-state" in text
    assert f"Resolved from: {expected}" in text
    assert "Worker: worker-1" in text


def test_render_instructions_keeps_the_workflow_text_and_orders_the_store_first() -> None:
    text = render_instructions(_target("flag"))

    assert text.index("## Bound store") < text.index("## The loop")
    assert "pending" in text
    for line in INSTRUCTIONS.splitlines():
        assert line in text
    assert render_instructions(None) == INSTRUCTIONS


def test_credentials_never_reach_the_rendered_text_or_the_log_line() -> None:
    target = _target("flag", remote=f"https://user:{TOKEN}@github.com/o/r.git")

    assert TOKEN not in render_instructions(target, "w")
    assert TOKEN not in target.describe()
    assert TOKEN not in target.state_ref.display
    assert "github.com/o/r.git" in target.describe()


def _launch(tmp_path: Path, *extra: str):
    remote = tmp_path / "BenchBox-state.git"
    if not remote.exists():
        subprocess.run(["git", "init", "--quiet", "--bare", str(remote)], check=True)
        bootstrap(StateRef(remote=str(remote), branch="todo-state"))
    argv = ["--state-remote", str(remote), "--cache-dir", str(tmp_path / "cache"), "--actor", "w1", *extra]
    return resolve_launch_config(build_parser().parse_args(argv))


def _result(call) -> object:
    structured = getattr(call, "structuredContent", None)
    if structured is not None:
        return structured.get("result", structured)
    for block in call.content:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except ValueError:
                return text
    raise AssertionError(f"no content in {call!r}")


def _run(tmp_path: Path, scenario) -> None:
    async def go() -> None:
        server = build_server(_launch(tmp_path))
        async with connect(server, client_info=types.Implementation(name="w1", version="0")) as session:
            await scenario(server, session)

    anyio.run(go)


def test_handshake_instructions_name_the_bound_store(tmp_path: Path) -> None:
    async def scenario(server, session) -> None:
        assert "## Bound store" in server.instructions
        assert "BenchBox-state.git" in server.instructions
        assert "Worker: w1" in server.instructions

    _run(tmp_path, scenario)


def test_get_instructions_resource_and_prompt_report_the_store(tmp_path: Path) -> None:
    async def scenario(server, session) -> None:
        tool = _result(await session.call_tool("get_instructions", {}))
        assert isinstance(tool, str) and "## Bound store" in tool and "BenchBox-state.git" in tool

        resource = await session.read_resource("todo://instructions")
        assert "BenchBox-state.git" in resource.contents[0].text

        prompt = await session.get_prompt("todo/workflow")
        assert "BenchBox-state.git" in prompt.messages[0].content.text

    _run(tmp_path, scenario)


def test_expect_store_accepts_a_fragment_ignoring_case(tmp_path: Path) -> None:
    async def scenario(server, session) -> None:
        for fragment in ("benchbox-state", "BENCHBOX-STATE.git#todo-state", "  BenchBox  "):
            outcome = _result(await session.call_tool("get_instructions", {"expect_store": fragment}))
            assert isinstance(outcome, str), outcome
            assert "## Bound store" in outcome

    _run(tmp_path, scenario)


def test_expect_store_refuses_another_project_with_a_gate_error(tmp_path: Path) -> None:
    async def scenario(server, session) -> None:
        outcome = _result(await session.call_tool("get_instructions", {"expect_store": "todo-db"}))

        assert isinstance(outcome, dict)
        assert outcome["ok"] is False
        assert outcome["code"] == "E_STATE"
        assert outcome["kind"] == "gate"
        assert "BenchBox-state.git" in outcome["error"]
        assert "'todo-db'" in outcome["error"]
        assert outcome["recovery"]

    _run(tmp_path, scenario)


def test_expect_store_must_not_be_blank(tmp_path: Path) -> None:
    async def scenario(server, session) -> None:
        outcome = _result(await session.call_tool("get_instructions", {"expect_store": "   "}))

        assert isinstance(outcome, dict)
        assert outcome["ok"] is False
        assert outcome["code"] == "E_STATE"

    _run(tmp_path, scenario)


def test_reported_resolution_source_follows_the_real_resolver(tmp_path: Path, monkeypatch) -> None:
    from todo_db.mcp.target import resolve_target

    for name in ("TODO_DB_STATE_REMOTE", "TODO_DB_STATE_BRANCH", "TODO_DB_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    config = tmp_path / ".todo-db" / "config.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"state_remote": "https://example.test/o/r.git", "state_branch": "tracker"}))

    flag = resolve_target(state_remote="https://example.test/flag.git", cache_dir=str(tmp_path))
    explicit = resolve_target(config=str(config), cache_dir=str(tmp_path))
    discovered = resolve_target(repo_root=str(tmp_path), cache_dir=str(tmp_path))
    monkeypatch.setenv("TODO_DB_STATE_REMOTE", "https://example.test/env.git")
    environment = resolve_target(cache_dir=str(tmp_path))

    assert "launch flags" in render_instructions(flag)
    assert f"Resolved from: config file {config.resolve()}" in render_instructions(explicit)
    assert f"Resolved from: discovered config file {config.resolve()}" in render_instructions(discovered)
    assert "Branch: tracker" in render_instructions(discovered)
    assert "environment (TODO_DB_STATE_REMOTE)" in render_instructions(environment)
