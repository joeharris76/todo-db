"""Subprocess stdio conformance, stdout purity, and tool-schema freeze (mcp-stdio-conformance)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from todo_db import DatabaseConfig, ProjectIdentity, TodoDatabase


def _make_project(root: Path) -> Path:
    (root / ".git").mkdir(exist_ok=True)
    # Make it a real git repo for the stdio test (GitScopeEngine needs it).
    subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    cfgdir = root / ".todo-db"
    cfgdir.mkdir(exist_ok=True)
    ident = ProjectIdentity(project_id="p", repository="https://example.com/p")
    payload = {"project_id": ident.project_id, "repository": ident.repository}
    (cfgdir / "config.json").write_text(json.dumps(payload))
    db_path = cfgdir / "standalone.sqlite"
    TodoDatabase.open(DatabaseConfig(path=str(db_path), identity=ident)).close()
    return db_path


def _spawn_server(repo_root: Path) -> subprocess.Popen:
    env = dict(subprocess.os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    return subprocess.Popen(
        [sys.executable, "-m", "todo_db.mcp", "--repo-root", str(repo_root), "--actor", "tester"],
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
    # Read one line of stdout
    line = proc.stdout.readline()
    if not line:
        # Check stderr for error
        err = proc.stderr.read()
        raise RuntimeError(f"no response for {method}: stderr={err!r}")
    return json.loads(line)


def test_stdio_smoke_initialize_and_tools_list(tmp_path: Path):
    _make_project(tmp_path)
    proc = _spawn_server(tmp_path)
    try:
        # initialize
        resp = _rpc(
            proc,
            "initialize",
            {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test-stdio", "version": "0"}},
            req_id=1,
        )
        assert "result" in resp, f"initialize failed: {resp}"
        # initialized notification
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        proc.stdin.flush()
        # tools/list
        resp2 = _rpc(proc, "tools/list", {}, req_id=2)
        assert "result" in resp2, f"tools/list failed: {resp2}"
        tools = resp2["result"].get("tools") or resp2["result"].get("tools", [])
        # Also handle result.tools
        if isinstance(tools, dict):
            tools = tools.get("tools", [])
        names = {t["name"] for t in tools}
        assert "get_instructions" in names
        assert "next" in names
        assert "take" in names
        # call next
        resp3 = _rpc(proc, "tools/call", {"name": "next", "arguments": {}}, req_id=3)
        assert "result" in resp3
        # Ensure every stdout line was JSON-RPC
        # (we already parsed 3 lines; ensure no extra non-JSON)
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            proc.kill()


def test_stdout_purity_no_non_json(tmp_path: Path):
    _make_project(tmp_path)
    proc = _spawn_server(tmp_path)
    try:
        # Do a full interaction and collect all stdout lines
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
        _rpc(proc, "tools/call", {"name": "next", "arguments": {}}, req_id=3)
        _rpc(proc, "tools/call", {"name": "get_instructions", "arguments": {}}, req_id=4)
        # Give server a moment to flush
        proc.stdin.close()
        # Read remaining stdout lines
        remaining = proc.stdout.read()
        # All lines should be JSON or empty
        for line in remaining.splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                raise AssertionError(f"non-JSON stdout line: {line!r}")
            assert "jsonrpc" in obj
        # Stderr should contain logs, not stdout
        stderr = proc.stderr.read()
        assert "session id" in stderr or "principal" in stderr or "target" in stderr
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            proc.kill()


def test_tool_schema_freeze():
    snap_path = Path("scripts/mcp_snapshots/tools.json")
    assert snap_path.is_file(), f"snapshot missing: {snap_path}"
    snapshot = json.loads(snap_path.read_text())

    # Build current snapshot via in-memory server (agent profile)
    import tempfile

    import anyio
    import mcp.types as types
    from mcp.shared.memory import create_connected_server_and_client_session as connect
    from todo_db.mcp.server import build_parser, build_server, resolve_launch_config

    tmp = Path(tempfile.mkdtemp())
    subprocess.run(["git", "init", "--quiet"], cwd=tmp, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp, check=True)
    cfgdir = tmp / ".todo-db"
    cfgdir.mkdir()
    ident = ProjectIdentity(project_id="p", repository="https://example.com/p")
    payload = {"project_id": ident.project_id, "repository": ident.repository}
    (cfgdir / "config.json").write_text(json.dumps(payload))
    db_path = cfgdir / "standalone.sqlite"
    TodoDatabase.open(DatabaseConfig(path=str(db_path), identity=ident)).close()

    parser = build_parser()
    args = parser.parse_args(["--repo-root", str(tmp), "--actor", "tester"])
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
            # Compare names and descriptions and schemas
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
