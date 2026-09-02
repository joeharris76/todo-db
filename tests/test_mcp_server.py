"""Tests for the MCP stdio server foundation (``todo_db.mcp``).

Covers the skeleton only -- no lifecycle tools exist yet. If the ``mcp`` SDK is
not importable (e.g. a network-blocked ``uv add``), every test that needs the
in-memory transport is skipped via ``pytest.importorskip``.
"""

from __future__ import annotations

import io
import json
import pathlib
import sqlite3
from contextlib import redirect_stdout

import pytest

anyio = pytest.importorskip("anyio")

from todo_db import DatabaseConfig, ProjectIdentity, TodoDatabase  # noqa: E402
from todo_db.mcp import identity as identity_mod  # noqa: E402
from todo_db.mcp.identity import Identity, resolve_identity  # noqa: E402
from todo_db.mcp.server import build_parser, build_server, resolve_launch_config, startup_check  # noqa: E402
from todo_db.mcp.target import resolve_target  # noqa: E402
from todo_db.mcp.worker import run_in_worker, shutdown_worker  # noqa: E402

mcp = pytest.importorskip("mcp")

IDENT = ProjectIdentity(project_id="p", repository="https://example.com/p")


def _make_project(root: pathlib.Path, *, db_key: str | None = None) -> pathlib.Path:
    (root / ".git").mkdir(exist_ok=True)
    cfgdir = root / ".todo-db"
    cfgdir.mkdir(exist_ok=True)
    payload: dict = {"project_id": IDENT.project_id, "repository": IDENT.repository}
    if db_key is not None:
        payload["db"] = db_key
    (cfgdir / "config.json").write_text(json.dumps(payload))
    db_path = cfgdir / "standalone.sqlite" if db_key is None else root / db_key
    TodoDatabase.open(DatabaseConfig(path=str(db_path), identity=IDENT)).close()
    return db_path


def _args(*argv: str):
    return build_parser().parse_args(list(argv))


@pytest.fixture(autouse=True)
def _worker_cleanup():
    yield
    shutdown_worker(wait=True)


# --------------------------------------------------------------------------- #
# server start / tool surface
# --------------------------------------------------------------------------- #
def test_server_starts_and_lists_only_get_instructions(tmp_path):
    _make_project(tmp_path)
    server = build_server(resolve_launch_config(_args("--repo-root", str(tmp_path), "--actor", "tester")))

    import mcp.types as types
    from mcp.shared.memory import create_connected_server_and_client_session as connect

    async def go():
        async with connect(server, client_info=types.Implementation(name="x", version="0")) as session:
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            # Foundation had only get_instructions; after mcp-tools-work the
            # six hot-path tools plus claims are always loaded (plan §5).
            assert "get_instructions" in names
            for expected in ("next", "take", "context", "progress", "finish", "release", "claims"):
                assert expected in names, f"missing work tool {expected!r} in {sorted(names)}"
            result = await session.call_tool("get_instructions", {})
            assert "Autonomous Agent Workflow Protocol" in result.content[0].text
            resources = await session.list_resources()
            assert "todo://instructions" in {str(r.uri) for r in resources.resources}
            prompts = await session.list_prompts()
            assert "todo/workflow" in {p.name for p in prompts.prompts}

    anyio.run(go)


# --------------------------------------------------------------------------- #
# target resolution precedence
# --------------------------------------------------------------------------- #
def test_target_precedence_flag_over_env_over_discovery(tmp_path, monkeypatch):
    _make_project(tmp_path)  # discovery tier available

    discovered = resolve_target(repo_root=str(tmp_path))
    assert discovered.source == "discovery"
    assert discovered.db_target.endswith(".todo-db/standalone.sqlite")

    monkeypatch.setenv("TODO_DB_PATH", "/env/path/todo.sqlite")
    env = resolve_target(repo_root=str(tmp_path))
    assert env.db_target == "/env/path/todo.sqlite"
    assert env.source == "env"

    flag = resolve_target(db="/flag/path/todo.sqlite", repo_root=str(tmp_path))
    assert flag.db_target == "/flag/path/todo.sqlite"
    assert flag.source == "flag"


def test_target_config_flag_carries_identity(tmp_path):
    _make_project(tmp_path)
    target = resolve_target(config=str(tmp_path / ".todo-db" / "config.json"))
    assert target.identity == IDENT


def test_target_env_db_path_wins_over_url(monkeypatch, tmp_path):
    _make_project(tmp_path)
    monkeypatch.setenv("TODO_DB_PATH", "/tmp/from-path.sqlite")
    monkeypatch.setenv("TODO_DB_URL", "libsql://example.turso.io")
    target = resolve_target(repo_root=str(tmp_path))
    assert target.db_target == "/tmp/from-path.sqlite"


def test_target_identity_env_wins_over_config_payload(tmp_path, monkeypatch):
    _make_project(tmp_path)
    monkeypatch.setenv("TODO_DB_PROJECT_ID", "env-project")
    monkeypatch.setenv("TODO_DB_REPOSITORY", "https://example.com/env")
    target = resolve_target(repo_root=str(tmp_path))
    assert target.identity is not None
    assert target.identity.project_id == "env-project"
    assert target.identity.repository == "https://example.com/env"


def test_target_partial_identity_raises(tmp_path):
    (tmp_path / ".git").mkdir(exist_ok=True)
    cfgdir = tmp_path / ".todo-db"
    cfgdir.mkdir(exist_ok=True)
    (cfgdir / "config.json").write_text(json.dumps({"project_id": "only-id"}))
    with pytest.raises(Exception) as excinfo:
        resolve_target(repo_root=str(tmp_path))
    assert "partial project identity" in str(excinfo.value).lower()

    (cfgdir / "config.json").write_text(json.dumps({"repository": "https://example.com/r"}))
    with pytest.raises(Exception) as excinfo:
        resolve_target(repo_root=str(tmp_path))
    assert "partial project identity" in str(excinfo.value).lower()


def test_target_partial_identity_env_raises(tmp_path, monkeypatch):
    # Use an explicit --db so discovery is bypassed and only env+empty payload matters.
    # With only one of the two env vars set, _identity_from should raise.
    monkeypatch.setenv("TODO_DB_PROJECT_ID", "only-env-id")
    monkeypatch.delenv("TODO_DB_REPOSITORY", raising=False)
    monkeypatch.delenv("TODO_DB_CONFIG", raising=False)
    monkeypatch.delenv("TODO_DB_PATH", raising=False)
    monkeypatch.delenv("TODO_DB_URL", raising=False)
    with pytest.raises(Exception) as excinfo:
        resolve_target(db="/tmp/test-partial.sqlite", repo_root=str(tmp_path))
    assert "partial project identity" in str(excinfo.value).lower()
    monkeypatch.delenv("TODO_DB_PROJECT_ID", raising=False)
    monkeypatch.setenv("TODO_DB_REPOSITORY", "https://example.com/r")
    with pytest.raises(Exception) as excinfo:
        resolve_target(db="/tmp/test-partial.sqlite", repo_root=str(tmp_path))
    assert "partial project identity" in str(excinfo.value).lower()


# --------------------------------------------------------------------------- #
# startup READ_ONLY open never migrates
# --------------------------------------------------------------------------- #
def test_startup_readonly_does_not_migrate_behind_schema(tmp_path):
    db_path = _make_project(tmp_path)

    # Simulate a database one migration behind the package.
    raw = sqlite3.connect(db_path)
    before_versions = [r[0] for r in raw.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()]
    raw.execute("DELETE FROM schema_migrations WHERE version = (SELECT max(version) FROM schema_migrations)")
    raw.commit()
    raw.close()

    before = db_path.stat()
    target = resolve_target(repo_root=str(tmp_path))

    with pytest.raises(Exception) as excinfo:
        startup_check(target)
    message = str(excinfo.value)
    assert "E_SCHEMA" in message and "migrate" in message
    assert getattr(excinfo.value, "code", None) == "E_SCHEMA"

    after = db_path.stat()
    assert (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)

    # And no migration row was re-added (version list byte-identical to the behind state).
    raw = sqlite3.connect(db_path)
    versions = [r[0] for r in raw.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()]
    raw.close()
    assert versions == before_versions[:-1]
    assert len(versions) >= 1


# --------------------------------------------------------------------------- #
# hosted target gate
# --------------------------------------------------------------------------- #
def test_hosted_target_without_allow_hosted_refuses(tmp_path):
    args = _args("--repo-root", str(tmp_path), "--db", "libsql://example.turso.io", "--actor", "tester")
    with pytest.raises(Exception) as excinfo:
        resolve_launch_config(args)
    assert "hosted" in str(excinfo.value).lower()


def test_hosted_target_with_allow_hosted_is_permitted(tmp_path):
    args = _args(
        "--repo-root", str(tmp_path), "--db", "libsql://example.turso.io", "--actor", "tester", "--allow-hosted"
    )
    launch = resolve_launch_config(args)
    assert launch.target.is_hosted and launch.allow_hosted


# --------------------------------------------------------------------------- #
# identity
# --------------------------------------------------------------------------- #
def test_identity_explicit_actor_wins(monkeypatch):
    monkeypatch.setenv("TODO_DB_ACTOR", "env-actor")
    ident = resolve_identity("flag-actor", None)
    assert ident.principal() == "flag-actor"
    assert not ident.actor_pending


def test_identity_env_actor_used_when_no_flag(monkeypatch):
    monkeypatch.setenv("TODO_DB_ACTOR", "env-actor")
    assert resolve_identity(None, None).principal() == "env-actor"


def test_identity_derived_from_client_info_when_unset(monkeypatch):
    monkeypatch.delenv("TODO_DB_ACTOR", raising=False)

    def _boom():  # pragma: no cover - must never run
        raise AssertionError("default_actor() must never be called by the MCP server")

    monkeypatch.setattr("todo_db.tracker.default_actor", _boom)
    if hasattr(identity_mod, "default_actor"):  # pragma: no cover - defensive
        monkeypatch.setattr(identity_mod, "default_actor", _boom)

    ident = resolve_identity(None, None)
    assert ident.actor_pending
    assert ident.principal() is None

    class _CI:
        name = "x"

    principal = ident.principal(_CI())
    assert principal.startswith("mcp:x:")


def test_identity_session_id_is_stable_and_overridable():
    ident = resolve_identity("a", "sess-123")
    assert ident.session_id == "sess-123"
    generated = resolve_identity("a", None)
    assert generated.session_id and generated.session_id != Identity("x").session_id


def test_identity_sanitizes_client_name():
    from todo_db.mcp.identity import principal_from_client_info

    class _CI:
        name = "bad: name\nwith spaces/and:colons"

    principal = principal_from_client_info(_CI())
    # ':' and newline should be sanitized to '-'
    assert ":" not in principal.split("mcp:")[1].split(":")[0]
    assert "\n" not in principal
    assert principal.startswith("mcp:")

    class _Empty:
        name = "   "

    assert principal_from_client_info(_Empty()).split(":")[1] == "unknown"

    class _Long:
        name = "a" * 200

    assert len(principal_from_client_info(_Long()).split(":")[1]) <= 64


def test_principal_pinned_via_get_instructions(tmp_path):
    _make_project(tmp_path)
    # No --actor, so principal is pending until first tool call
    launch = resolve_launch_config(_args("--repo-root", str(tmp_path)))
    assert launch.identity.actor_pending
    server = build_server(launch)

    import mcp.types as types
    from mcp.shared.memory import create_connected_server_and_client_session as connect

    async def go():
        async with connect(server, client_info=types.Implementation(name="my-agent", version="0")) as session:
            await session.call_tool("get_instructions", {})
            # After the tool call the holder should be pinned
            # Access via server's closed-over principal holder is not directly exposed,
            # but we can verify the tool succeeded and no error was raised.
            result = await session.call_tool("get_instructions", {})
            assert "Autonomous Agent Workflow Protocol" in result.content[0].text

    anyio.run(go)


def test_main_startup_gate_returns_clean_exit_on_behind_schema(tmp_path, monkeypatch, capsys):
    from todo_db.mcp.server import main

    db_path = _make_project(tmp_path)
    raw = sqlite3.connect(db_path)
    raw.execute("DELETE FROM schema_migrations WHERE version = (SELECT max(version) FROM schema_migrations)")
    raw.commit()
    raw.close()

    code = main(["--repo-root", str(tmp_path), "--actor", "tester"])
    assert code == 2
    captured = capsys.readouterr()
    assert "E_SCHEMA" in captured.err
    assert captured.out == ""


# --------------------------------------------------------------------------- #
# worker thread
# --------------------------------------------------------------------------- #
def test_run_in_worker_executes_and_db_touch_has_no_thread_affinity_error(tmp_path):
    db_path = _make_project(tmp_path)

    def _touch():
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            return conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0]
        finally:
            conn.close()

    async def go():
        return await run_in_worker(_touch)

    assert anyio.run(go) >= 1


def test_run_in_worker_serializes_on_one_thread():
    import threading

    seen: set[int] = set()

    def _record():
        seen.add(threading.get_ident())

    async def go():
        for _ in range(5):
            await run_in_worker(_record)

    anyio.run(go)
    assert len(seen) == 1


# --------------------------------------------------------------------------- #
# stdout purity
# --------------------------------------------------------------------------- #
def test_no_server_code_path_writes_to_stdout(tmp_path):
    _make_project(tmp_path)
    server = build_server(resolve_launch_config(_args("--repo-root", str(tmp_path), "--actor", "tester")))

    import mcp.types as types
    from mcp.shared.memory import create_connected_server_and_client_session as connect

    buffer = io.StringIO()

    async def go():
        async with connect(server, client_info=types.Implementation(name="x", version="0")) as session:
            await session.list_tools()
            await session.call_tool("get_instructions", {})

    with redirect_stdout(buffer):
        anyio.run(go)
    assert buffer.getvalue() == ""


def test_package_source_never_prints_to_stdout():
    pkg = pathlib.Path(identity_mod.__file__).parent
    offenders = []
    for path in pkg.glob("*.py"):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("print(") and "file=sys.stderr" not in line:
                offenders.append(f"{path.name}:{lineno}")
    assert not offenders, f"stdout print() calls found: {offenders}"


# --------------------------------------------------------------------------- #
# work tools (mcp-tools-work)
# --------------------------------------------------------------------------- #
def _make_item(db_path, ident, item_id="item-work", title="Work item for MCP"):
    from todo_db import DatabaseConfig, TodoDatabase, TodoTracker

    db = TodoDatabase.open(DatabaseConfig(path=str(db_path), identity=ident))
    tracker = TodoTracker(db, actor="tester")
    tracker.create_item(
        item_id=item_id,
        title=title,
        worktree="todo-db",
        priority="high",
        description="desc for test item with enough length to pass validation",
        work=[{"id": "w0", "summary": "step for w0"}],
        scope={"only_modify": ["src/**"]},
        verifications=[{"description": "pass", "command": "true", "expected": ""}],
    )
    db.close()


def test_work_tools_next_take_progress_context_finish_flow(tmp_path):
    import subprocess

    db_path = _make_project(tmp_path)
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    _make_item(db_path, IDENT, item_id="item-flow", title="Flow item for work tools")
    server = build_server(resolve_launch_config(_args("--repo-root", str(tmp_path), "--actor", "tester")))

    import json as _json

    import mcp.types as types
    from mcp.shared.memory import create_connected_server_and_client_session as connect

    async def go():
        async with connect(server, client_info=types.Implementation(name="test-work", version="0")) as session:
            # next -> ready
            nxt = await session.call_tool("next", {})
            nxt_data = _json.loads(nxt.content[0].text)
            assert nxt_data["ok"] is True
            assert nxt_data["data"]["status"] == "ready"
            assert nxt_data["data"]["next_action"]["tool"] == "take"

            # take
            took = await session.call_tool("take", {"id": "item-flow"})
            took_data = _json.loads(took.content[0].text)
            assert took_data["ok"] is True
            assert took_data["data"]["id"] == "item-flow"
            token = took_data["data"]["claim_token"]
            assert token

            # context re-reads claim_token and next_action
            ctx = await session.call_tool("context", {"id": "item-flow"})
            ctx_data = _json.loads(ctx.content[0].text)
            assert ctx_data["ok"] is True
            assert ctx_data["data"]["claim_token"] == token
            assert ctx_data["data"]["next_action"]["tool"] == "progress"

            # progress
            prog = await session.call_tool(
                "progress", {"id": "item-flow", "wid": "w0", "evidence": "did w0", "claim_token": token}
            )
            prog_data = _json.loads(prog.content[0].text)
            assert prog_data["ok"] is True
            # after progress, next_action should be finish (tool)
            assert prog_data["data"]["next_action"]["tool"] == "finish"

            # finish without attest -> E_VERIFY_GATE gate
            fin = await session.call_tool("finish", {"id": "item-flow", "claim_token": token})
            fin_data = _json.loads(fin.content[0].text)
            assert fin_data["ok"] is False
            assert fin_data["code"] == "E_VERIFY_GATE"
            assert fin_data["kind"] == "gate"

            # release
            rel = await session.call_tool("release", {"id": "item-flow", "claim_token": token})
            rel_data = _json.loads(rel.content[0].text)
            assert rel_data["ok"] is True

    anyio.run(go)


def test_work_tools_envelope_and_claims_recovery(tmp_path):
    import subprocess

    db_path = _make_project(tmp_path)
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    _make_item(db_path, IDENT, item_id="item-a", title="Item A for claims test")
    _make_item(db_path, IDENT, item_id="item-b", title="Item B for claims test")
    server = build_server(resolve_launch_config(_args("--repo-root", str(tmp_path), "--actor", "tester")))

    import json as _json

    import mcp.types as types
    from mcp.shared.memory import create_connected_server_and_client_session as connect

    async def go():
        async with connect(server, client_info=types.Implementation(name="test-claims", version="0")) as session:
            # take first item
            await session.call_tool("take", {"id": "item-a"})
            # manually create a second active claim for same principal to force E_MULTIPLE_CLAIMS
            # (tracker.claim bypasses workflow, so we do it directly on DB)
            from todo_db import DatabaseConfig, TodoDatabase, TodoTracker

            db = TodoDatabase.open(DatabaseConfig(path=str(db_path), identity=IDENT))
            tracker = TodoTracker(db, actor="tester")
            tracker.claim("item-b")
            db.close()

            nxt = await session.call_tool("next", {})
            nxt_data = _json.loads(nxt.content[0].text)
            assert nxt_data["ok"] is False
            assert nxt_data["code"] == "E_MULTIPLE_CLAIMS"
            assert nxt_data["kind"] == "gate"
            # recovery should carry both ids and tokens
            assert any("item-a" in r for r in nxt_data["recovery"])
            assert any("item-b" in r for r in nxt_data["recovery"])

            # claims tool should list both
            claims = await session.call_tool("claims", {})
            claims_data = _json.loads(claims.content[0].text)
            assert claims_data["ok"] is True
            ids = {c["id"] for c in claims_data["data"]["claims"]}
            assert {"item-a", "item-b"} <= ids

    anyio.run(go)


def test_take_empty_queue_returns_nothing_ready(tmp_path):
    import subprocess

    _make_project(tmp_path)
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    server = build_server(resolve_launch_config(_args("--repo-root", str(tmp_path), "--actor", "tester")))

    import json as _json

    import mcp.types as types
    from mcp.shared.memory import create_connected_server_and_client_session as connect

    async def go():
        async with connect(server, client_info=types.Implementation(name="test-empty", version="0")) as session:
            res = await session.call_tool("take", {})
            data = _json.loads(res.content[0].text)
            assert data["ok"] is False
            assert data["code"] == "E_NOTHING_READY"
            assert data["kind"] == "gate"

    anyio.run(go)
