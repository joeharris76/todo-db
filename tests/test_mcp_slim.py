"""Slim MCP proving cases: lifecycle, serialization caps, schema cost."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import anyio
import mcp.types as types
from mcp.shared.memory import create_connected_server_and_client_session as connect

from todo_db import count_tokens
from todo_db.git_backend import StateRef, bootstrap
from todo_db.mcp.instructions import INSTRUCTIONS
from todo_db.mcp.server import build_parser, build_server, resolve_launch_config
from todo_db.service import MAX_BYTES


def _launch(tmp_path: Path, actor: str | None = "w1"):
    remote = tmp_path / "state.git"
    if not remote.exists():
        subprocess.run(["git", "init", "--quiet", "--bare", str(remote)], check=True)
        bootstrap(StateRef(remote=str(remote), branch="todo-state"))
    parser = build_parser()
    argv = ["--state-remote", str(remote), "--cache-dir", str(tmp_path / "cache")]
    if actor is not None:
        argv += ["--actor", actor]
    return resolve_launch_config(parser.parse_args(argv))


def _payload(result) -> dict:
    for block in result.content:
        text = getattr(block, "text", None)
        if text:
            return json.loads(text)
    if getattr(result, "structuredContent", None):
        return result.structuredContent
    raise AssertionError(f"no JSON payload in {result!r}")


def _text_len(result) -> int:
    for block in result.content:
        text = getattr(block, "text", None)
        if text:
            return len(text.encode("utf-8"))
    return 0


def test_lifecycle_over_tools(tmp_path: Path) -> None:
    async def go():
        server = build_server(_launch(tmp_path))
        async with connect(server, client_info=types.Implementation(name="w1", version="0")) as session:
            created = _payload(await session.call_tool("create_item", {"id": "t1", "title": "First"}))
            assert created["ok"], created
            took = _payload(await session.call_tool("take", {"id": "t1"}))
            assert took["ok"], took
            gen = took["data"]["claim"]["generation"]
            # Take returns enough context to begin work: no immediate show needed.
            assert took["data"]["title"] == "First"
            assert "description_excerpt" in took["data"]
            shown = _payload(await session.call_tool("show_item", {"id": "t1"}))
            assert shown["ok"] and shown["data"]["status"] == "active"
            renewed = _payload(await session.call_tool("renew", {"id": "t1", "generation": gen}))
            assert renewed["ok"], renewed
            finished = _payload(await session.call_tool("finish", {"id": "t1", "generation": gen}))
            assert finished["ok"] and finished["data"]["status"] == "done"
            dropped = _payload(await session.call_tool("create_item", {"id": "t2", "title": "Drop me"}))
            assert dropped["ok"]
            gone = _payload(await session.call_tool("drop", {"id": "t2"}))
            assert gone["ok"] and gone["data"]["status"] == "dropped"

    anyio.run(go)


def test_second_worker_conflict_and_stale_generation(tmp_path: Path) -> None:
    async def go():
        # One server instance pins one principal, so distinct workers run
        # distinct server instances against the same state branch.
        server1 = build_server(_launch(tmp_path, actor="w1"))
        server2 = build_server(_launch(tmp_path, actor="w2"))
        async with connect(server1, client_info=types.Implementation(name="w1", version="0")) as s1:
            assert _payload(await s1.call_tool("create_item", {"id": "c", "title": "Contested"}))["ok"]
            took = _payload(await s1.call_tool("take", {"id": "c"}))
            gen = took["data"]["claim"]["generation"]
        async with connect(server2, client_info=types.Implementation(name="w2", version="0")) as s2:
            conflict = _payload(await s2.call_tool("take", {"id": "c"}))
            assert not conflict["ok"] and conflict["code"] == "E_CONFLICT"
            stale = _payload(await s2.call_tool("finish", {"id": "c", "generation": "0" * 32}))
            assert not stale["ok"] and stale["code"] == "E_CLAIM_STALE"
        async with connect(server1, client_info=types.Implementation(name="w1", version="0")) as s1:
            released = _payload(await s1.call_tool("release", {"id": "c", "generation": gen}))
            assert released["ok"]

    anyio.run(go)


def test_every_tool_result_fits_final_serialization(tmp_path: Path) -> None:
    async def go():
        server = build_server(_launch(tmp_path))
        async with connect(server, client_info=types.Implementation(name="w1", version="0")) as session:
            big = "y" * 50000
            assert _payload(await session.call_tool(
                "create_item", {"id": "big", "title": "Big", "description": big}))["ok"]
            for name, args in (
                ("list_items", {}),
                ("list_items", {"limit": 50}),
                ("show_item", {"id": "big"}),
                ("show_item", {"id": "big", "field": "description"}),
                ("update_item", {"id": "big", "title": "Big v2"}),
                ("take", {"id": "big"}),
            ):
                result = await session.call_tool(name, args)
                assert _text_len(result) <= MAX_BYTES, name

    anyio.run(go)


def test_token_targets_on_actual_surfaces(tmp_path: Path) -> None:
    async def go():
        server = build_server(_launch(tmp_path))
        async with connect(server, client_info=types.Implementation(name="w1", version="0")) as session:
            for i in range(5):
                assert _payload(await session.call_tool(
                    "create_item", {"id": f"r{i}", "title": f"Row task {i}"}))["ok"]
            listed = await session.call_tool("list_items", {})
            tokens, _ = count_tokens(json.dumps(_payload(listed)))
            assert tokens <= 600, tokens
            took = await session.call_tool("take", {"id": "r0"})
            take_tokens, _ = count_tokens(json.dumps(_payload(took)))
            assert take_tokens <= 1600, take_tokens
            gen = _payload(took)["data"]["claim"]["generation"]
            finished = await session.call_tool("finish", {"id": "r0", "generation": gen})
            ack_tokens, _ = count_tokens(json.dumps(_payload(finished)))
            assert ack_tokens <= 100, ack_tokens

    anyio.run(go)


def test_startup_schema_cost_is_small(tmp_path: Path) -> None:
    async def go():
        server = build_server(_launch(tmp_path))
        async with connect(server, client_info=types.Implementation(name="x", version="0")) as session:
            tools = await session.list_tools()
            schema_blob = json.dumps([{"name": t.name, "description": t.description, "inputSchema": t.inputSchema}
                                      for t in tools.tools])
            tokens, tokenizer = count_tokens(schema_blob + INSTRUCTIONS)
            assert tokenizer == "o200k_base"
            # 10 tools plus the full protocol guidance, against 3,344 for
            # the old 26 tool definitions alone.
            assert tokens <= 3500, tokens

    anyio.run(go)
