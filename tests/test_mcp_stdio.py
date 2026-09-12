"""Subprocess stdio conformance, stdout purity, and tool-schema freeze."""

from __future__ import annotations

import json
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

EXPECTED_TOOLS = {
    "create_item",
    "drop",
    "finish",
    "get_instructions",
    "list_items",
    "release",
    "renew",
    "show_item",
    "take",
    "update_item",
}


def _make_state(root: Path) -> str:
    remote = root / "state.git"
    subprocess.run(["git", "init", "--quiet", "--bare", str(remote)], check=True)
    subprocess.run(
        [sys.executable, "-m", "todo_db.cli", "bootstrap",
         "--state-remote", str(remote), "--state-branch", "todo-state"],
        capture_output=True, text=True, check=True,
        cwd=Path(__file__).resolve().parents[1],
    )
    return str(remote)


def _spawn_server(remote: str, cache: Path) -> subprocess.Popen:
    import os

    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["TODO_DB_CACHE_DIR"] = str(cache)
    return subprocess.Popen(
        [sys.executable, "-m", "todo_db.mcp",
         "--state-remote", remote, "--actor", "tester"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
    )


def _rpc(proc: subprocess.Popen, method: str, params: dict | None = None, req_id: int = 1) -> dict:
    msg = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        msg["params"] = params
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    if not line:
        err = proc.stderr.read()
        raise RuntimeError(f"no response for {method}: stderr={err!r}")
    return json.loads(line)


def test_stdio_smoke_initialize_and_tools_list(tmp_path: Path):
    remote = _make_state(tmp_path)
    proc = _spawn_server(remote, tmp_path / "cache")
    try:
        resp = _rpc(
            proc,
            "initialize",
            {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test-stdio", "version": "0"}},
            req_id=1,
        )
        assert "result" in resp, f"initialize failed: {resp}"
        from todo_db import TOOL_VERSION

        assert TOOL_VERSION == version("todo-db")
        assert resp["result"]["serverInfo"] == {"name": "todo-db", "version": TOOL_VERSION}
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        proc.stdin.flush()
        resp2 = _rpc(proc, "tools/list", {}, req_id=2)
        assert "result" in resp2, f"tools/list failed: {resp2}"
        names = {t["name"] for t in resp2["result"]["tools"]}
        assert names == EXPECTED_TOOLS, f"tool surface drift: {sorted(names)}"
        resp3 = _rpc(proc, "tools/call", {"name": "list_items", "arguments": {}}, req_id=3)
        assert "result" in resp3
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


def test_stdout_purity_no_non_json(tmp_path: Path):
    remote = _make_state(tmp_path)
    proc = _spawn_server(remote, tmp_path / "cache")
    try:
        _rpc(
            proc,
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test-purity", "version": "0"},
            },
            req_id=1,
        )
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        proc.stdin.flush()
        _rpc(proc, "tools/list", {}, req_id=2)
        _rpc(proc, "tools/call", {"name": "list_items", "arguments": {}}, req_id=3)
        _rpc(proc, "tools/call", {"name": "get_instructions", "arguments": {}}, req_id=4)
        proc.stdin.close()
        remaining = proc.stdout.read()
        for line in remaining.splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                raise AssertionError(f"non-JSON stdout line: {line!r}")
            assert "jsonrpc" in obj
        stderr = proc.stderr.read()
        assert "session id" in stderr or "principal" in stderr or "target" in stderr
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


def test_tool_schema_freeze():
    snap_path = Path("scripts/mcp_snapshots/tools.json")
    assert snap_path.is_file(), f"snapshot missing: {snap_path}"
    snapshot = json.loads(snap_path.read_text())

    import tempfile

    import anyio
    import mcp.types as types
    from mcp.shared.memory import create_connected_server_and_client_session as connect
    from todo_db.mcp.server import build_parser, build_server, resolve_launch_config

    tmp = Path(tempfile.mkdtemp())
    remote = tmp / "state.git"
    subprocess.run(["git", "init", "--quiet", "--bare", str(remote)], check=True)
    cache = tmp / "cache"
    parser = build_parser()
    args = parser.parse_args(["--state-remote", str(remote), "--actor", "tester", "--cache-dir", str(cache)])
    launch = resolve_launch_config(args)
    server = build_server(launch)

    async def go():
        async with connect(server, client_info=types.Implementation(name="x", version="0")) as session:
            tools = await session.list_tools()
            current = []
            for t in tools.tools:
                entry = {"name": t.name, "description": t.description, "inputSchema": t.inputSchema}
                if hasattr(t, "outputSchema") and t.outputSchema:
                    entry["outputSchema"] = t.outputSchema
                current.append(entry)
            current = sorted(current, key=lambda x: x["name"])
            assert len(current) == len(snapshot), f"tool count drift: {len(current)} vs {len(snapshot)}"
            for cur, snap in zip(current, snapshot):
                assert cur["name"] == snap["name"], f"name drift: {cur['name']} vs {snap['name']}"
                assert cur["description"] == snap["description"], f"description drift for {cur['name']}"
                assert cur["inputSchema"] == snap["inputSchema"], f"inputSchema drift for {cur['name']}"

    anyio.run(go)


def test_no_print_to_stdout_in_mcp_package():
    pkg = Path("src/todo_db/mcp")
    offenders = []
    for path in pkg.glob("*.py"):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("print(") and "file=sys.stderr" not in line:
                offenders.append(f"{path.name}:{lineno}:{line.strip()}")
    assert not offenders, f"stdout print() calls found in mcp package: {offenders}"
