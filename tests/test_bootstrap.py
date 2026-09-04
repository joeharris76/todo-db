"""New-project bootstrap: config discovery, identity precedence, and init-project scaffolding."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


IDENTITY_ENV = ("TODO_DB_PROJECT_ID", "TODO_DB_REPOSITORY", "TODO_DB_PATH", "TODO_DB_URL", "TODO_DB_CONFIG")


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Bootstrap behavior depends on env and cwd; pin both per test."""

    for variable in IDENTITY_ENV:
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.chdir(tmp_path)


def _write_config(root: Path, payload: dict[str, object]) -> Path:
    config_dir = root / ".todo-db"
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "config.json"
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    return path


def _bound_identity(db_path: Path) -> tuple[str, str]:
    import sqlite3

    connection = sqlite3.connect(db_path)
    try:
        row = connection.execute("SELECT project_id, repository FROM project_identity WHERE singleton = 1").fetchone()
    finally:
        connection.close()
    assert row is not None
    return row[0], row[1]


def test_init_without_any_identity_source_is_a_hard_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from todo_db.cli import main

    assert main(["--db", str(tmp_path / "todo.sqlite"), "init"]) == 2
    err = capsys.readouterr().err
    assert "init requires a project identity" in err
    assert "--project-id" in err
    assert "init-project" in err
    assert not (tmp_path / "todo.sqlite").exists()


def test_discovered_config_supplies_identity_and_db_from_a_subdirectory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from todo_db.cli import main

    _write_config(tmp_path, {"project_id": "disc-test", "repository": "https://example.test/disc"})
    nested = tmp_path / "src" / "deep"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    assert main(["init"]) == 0
    assert "disc-test" in capsys.readouterr().out
    db_path = tmp_path / ".todo-db" / "standalone.sqlite"
    assert db_path.exists(), "db must resolve against the config root, not the nested cwd"
    assert _bound_identity(db_path) == ("disc-test", "https://example.test/disc")

    # A write verb (floor: sweep-stale) resolves identity and db from the discovered config.
    assert main(["sweep-stale"]) == 0
    # A read verb (floor: audit verify) resolves the same discovered boundary.
    assert main(["audit", "verify"]) == 0


def test_config_db_entry_resolves_relative_to_the_config_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from todo_db.cli import main

    _write_config(
        tmp_path,
        {"project_id": "rel-db", "repository": "todo-db", "db": "var/tracker.sqlite"},
    )
    nested = tmp_path / "nested"
    nested.mkdir()
    monkeypatch.chdir(nested)
    assert main(["init"]) == 0
    assert (tmp_path / "var" / "tracker.sqlite").exists()


def test_identity_precedence_is_flags_then_env_then_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from todo_db.cli import main

    _write_config(tmp_path, {"project_id": "from-config", "repository": "https://example.test/config"})

    env_db = tmp_path / "env.sqlite"
    monkeypatch.setenv("TODO_DB_PROJECT_ID", "from-env")
    monkeypatch.setenv("TODO_DB_REPOSITORY", "https://example.test/env")
    assert main(["--db", str(env_db), "init"]) == 0
    assert _bound_identity(env_db) == ("from-env", "https://example.test/env")

    flag_db = tmp_path / "flag.sqlite"
    assert (
        main(
            [
                "--db",
                str(flag_db),
                "init",
                "--project-id",
                "from-flag",
                "--repository",
                "https://example.test/flag",
            ]
        )
        == 0
    )
    assert _bound_identity(flag_db) == ("from-flag", "https://example.test/flag")


def test_partial_identity_is_rejected_with_an_actionable_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from todo_db.cli import main

    assert main(["--db", str(tmp_path / "todo.sqlite"), "init", "--project-id", "half"]) == 2
    assert "--repository is also required" in capsys.readouterr().err


def test_todo_db_config_env_selects_the_config_like_the_wrapper_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from todo_db.cli import main

    repo = tmp_path / "repo"
    config_path = _write_config(repo, {"project_id": "wrapper-test", "repository": "todo-db"})
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("TODO_DB_CONFIG", str(config_path))

    assert main(["init"]) == 0
    assert "wrapper-test" in capsys.readouterr().out
    assert (repo / ".todo-db" / "standalone.sqlite").exists()

    monkeypatch.setenv("TODO_DB_CONFIG", str(repo / "missing.json"))
    assert main(["init"]) == 2
    assert "TODO_DB_CONFIG points to a missing file" in capsys.readouterr().err


def test_bound_database_serves_callers_that_supply_no_identity(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from todo_db.cli import main

    db_path = tmp_path / "todo.sqlite"
    assert main(["--db", str(db_path), "init", "--project-id", "bound-test", "--repository", "todo-db"]) == 0
    capsys.readouterr()

    # A write verb against the bound database needs no caller-supplied identity.
    assert main(["--db", str(db_path), "sweep-stale"]) == 0
    assert main(["--db", str(db_path), "audit", "verify"]) == 0

    # The mismatch guard still enforces when the caller asserts an identity.
    assert main(["--db", str(db_path), "audit", "verify", "--project-id", "other", "--repository", "todo-db"]) == 2
    assert "project identity mismatch" in capsys.readouterr().err


def test_unbound_database_refuses_identityless_writes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from todo_db.cli import main

    assert main(["--db", str(tmp_path / "fresh.sqlite"), "sweep-stale"]) == 2
    assert "no bound project identity" in capsys.readouterr().err


def test_init_project_scaffolds_config_and_gitignore(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from todo_db.cli import main

    identity = ["--project-id", "scaffold-test", "--repository", "https://example.test/scaffold"]
    assert main(["init-project", *identity]) == 0
    out = capsys.readouterr().out
    assert "schema v" in out and "wrote" in out

    config = json.loads((tmp_path / ".todo-db" / "config.json").read_text(encoding="utf-8"))
    assert config == {
        "project_id": "scaffold-test",
        "repository": "https://example.test/scaffold",
        "db": ".todo-db/standalone.sqlite",
    }
    assert (tmp_path / ".todo-db" / "standalone.sqlite").exists()

    gitignore = (tmp_path / ".todo-db" / ".gitignore").read_text(encoding="utf-8")
    assert gitignore == "*.sqlite*\nreplica.db*\n*.lock\n!config.json\n"

    # No wrapper script is scaffolded: MCP is the agent surface and the floor
    # CLI is invoked as `todo-db` directly (ADR 0006 G1/G2).
    assert not (tmp_path / "_project").exists()

    # The scaffolded repo now serves identityless calls via discovery.
    assert main(["audit", "verify"]) == 0


def test_init_project_scaffolds_mcp_registration(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Planning lives only on the MCP surface, so adoption must reach it."""

    from todo_db.cli import main

    identity = ["--project-id", "mcp-scaffold", "--repository", "https://example.test/mcp"]
    assert main(["init-project", *identity]) == 0

    registration = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert registration["mcpServers"]["todo-db"]["command"] == "todo-db-mcp"
    # No --actor: the server derives the principal from the initialize
    # handshake, which resolves the host correctly. A hand-written
    # "${USER}@${HOSTNAME}" expands to a truncated principal in most clients.
    assert registration["mcpServers"]["todo-db"]["args"] == []


def test_init_project_never_clobbers_an_existing_mcp_registration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from todo_db.cli import main

    existing = '{"mcpServers": {"other": {"command": "other-mcp"}}}\n'
    (tmp_path / ".mcp.json").write_text(existing, encoding="utf-8")

    identity = ["--project-id", "mcp-keep", "--repository", "https://example.test/keep"]
    assert main(["init-project", *identity]) == 0

    assert (tmp_path / ".mcp.json").read_text(encoding="utf-8") == existing
    assert "kept existing" in capsys.readouterr().out

    # --force is an explicit request to overwrite the scaffold.
    assert main(["init-project", *identity, "--force"]) == 0
    rewritten = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert "todo-db" in rewritten["mcpServers"]


def test_init_project_is_idempotent_only_with_force(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from todo_db.cli import main

    identity = ["--project-id", "force-test", "--repository", "todo-db"]
    assert main(["init-project", *identity]) == 0
    capsys.readouterr()

    assert main(["init-project", *identity]) == 2
    err = capsys.readouterr().err
    assert "refusing to overwrite" in err and "--force" in err

    assert main(["init-project", *identity, "--force"]) == 0
    assert "wrote" in capsys.readouterr().out


def test_init_project_requires_explicit_identity_and_ignores_discovered_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from todo_db.cli import main

    parent = tmp_path / "parent"
    _write_config(parent, {"project_id": "parent-project", "repository": "todo-db"})
    child = parent / "child"
    child.mkdir()
    monkeypatch.chdir(child)

    assert main(["init-project"]) == 2
    assert "init-project requires an explicit project identity" in capsys.readouterr().err
    assert not (child / ".todo-db").exists()


def test_init_project_records_a_custom_db_target_in_the_config(tmp_path: Path) -> None:
    from todo_db.cli import main

    identity = ["--project-id", "custom-db", "--repository", "todo-db"]
    assert main(["init-project", *identity, "--db", "state/tracker.sqlite"]) == 0
    config = json.loads((tmp_path / ".todo-db" / "config.json").read_text(encoding="utf-8"))
    assert config["db"] == "state/tracker.sqlite"
    assert (tmp_path / "state" / "tracker.sqlite").exists()

    # Discovery resolves the recorded relative path against the config root.
    assert main(["audit", "verify"]) == 0


def test_doctor_ignores_a_legacy_wrapper_config_key(tmp_path: Path, capsys) -> None:
    """A legacy ``"wrapper"`` key left in config.json is an unknown key doctor
    must ignore, not fail on (ADR 0006 G2 / migration note)."""
    from todo_db.cli import main

    assert main(["init-project", "--project-id", "legacy-key-test", "--repository", "todo-db"]) == 0
    config_path = tmp_path / ".todo-db" / "config.json"
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["wrapper"] = "_project/scripts/todo"
    config_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    capsys.readouterr()

    assert main(["doctor"]) == 0
    output = capsys.readouterr().out
    assert "FAIL" not in output
    assert "wrapper:" not in output  # no wrapper check line is emitted at all

@pytest.mark.skipif(shutil.which("git") is None, reason="git is required to verify ignore semantics")
def test_scaffolded_gitignore_ignores_databases_but_keeps_config_tracked(tmp_path: Path) -> None:
    from todo_db.cli import main

    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    assert main(["init-project", "--project-id", "git-test", "--repository", "todo-db"]) == 0
    (tmp_path / ".todo-db" / "local.lock").write_bytes(b"")
    (tmp_path / ".todo-db" / "replica.db").write_bytes(b"")

    def ignored(path: str) -> bool:
        return (
            subprocess.run(
                ["git", "check-ignore", "-q", path], cwd=tmp_path, capture_output=True, check=False
            ).returncode
            == 0
        )

    assert ignored(".todo-db/standalone.sqlite")
    assert ignored(".todo-db/standalone.sqlite-wal")
    assert ignored(".todo-db/replica.db")
    assert ignored(".todo-db/local.lock")
    assert not ignored(".todo-db/config.json")
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=tmp_path, capture_output=True, text=True, check=True
    ).stdout
    assert ".todo-db/" in status


def test_discovery_ceiling_stops_at_git_root_and_does_not_hijack_subproject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from todo_db.cli import _discover_repo_config

    # Ancestor has a .todo-db/config.json
    ancestor = tmp_path / "workspace"
    ancestor.mkdir()
    _write_config(ancestor, {"project_id": "ancestor-proj", "repository": "repo"})

    # Child is a separate git repo without a config
    child_repo = ancestor / "child-repo"
    child_repo.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=child_repo, check=True)
    monkeypatch.chdir(child_repo)

    # Config discovery must stop at git root and return None
    assert _discover_repo_config() is None


def test_e_no_project_pre_open_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from todo_db.cli import main

    # In a clean directory with no config, no git, no flags
    clean_dir = tmp_path / "empty_dir"
    clean_dir.mkdir()
    monkeypatch.chdir(clean_dir)
    monkeypatch.delenv("TODO_DB_CONFIG", raising=False)
    monkeypatch.delenv("TODO_DB_PROJECT_ID", raising=False)
    monkeypatch.delenv("TODO_DB_REPOSITORY", raising=False)

    assert main(["audit", "verify"]) == 2
    err = capsys.readouterr().err
    assert "E_NO_PROJECT" in err

def test_env_db_without_identity_refuses_writes_but_allows_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Stale TODO_DB_PATH/TODO_DB_URL without discovered config must not contaminate another tracker.

    Pre-0.2.0 defaults accidentally provided this guard via E_NO_PROJECT; this
    restores it deliberately: env-sourced DB + identity-from-nowhere => refuse
    writes with an actionable error while reads still succeed.
    """

    from todo_db.cli import main

    from todo_db import DatabaseConfig, ProjectIdentity, TodoDatabase, TodoTracker

    other_db = tmp_path / "other.sqlite"
    assert main(["--db", str(other_db), "init", "--project-id", "other-proj", "--repository", "https://example.test/other"]) == 0
    capsys.readouterr()
    _other_config = DatabaseConfig(
        path=other_db,
        identity=ProjectIdentity(project_id="other-proj", repository="https://example.test/other"),
    )
    _other = TodoDatabase.open(_other_config)
    TodoTracker(_other, actor="seed").create_item(
        item_id="other-item",
        title="Other item title",
        worktree="todo-db",
        priority="medium",
        description="Other item description long enough.",
    )
    _other.close()

    clean_dir = tmp_path / "clean"
    clean_dir.mkdir()
    monkeypatch.chdir(clean_dir)
    monkeypatch.delenv("TODO_DB_CONFIG", raising=False)
    monkeypatch.delenv("TODO_DB_PROJECT_ID", raising=False)
    monkeypatch.delenv("TODO_DB_REPOSITORY", raising=False)
    monkeypatch.setenv("TODO_DB_PATH", str(other_db))
    monkeypatch.delenv("TODO_DB_URL", raising=False)

    # A write verb (sweep-stale) is refused: env-sourced DB with identity from nowhere.
    assert main(["sweep-stale"]) == 2
    err = capsys.readouterr().err
    assert "refusing to write" in err
    assert "TODO_DB_PATH/TODO_DB_URL" in err
    assert "--db" in err

    # A read verb still succeeds against the env-sourced DB.
    assert main(["audit", "verify"]) == 0

    import sqlite3

    assert [row[0] for row in sqlite3.connect(other_db).execute("SELECT id FROM items ORDER BY id")] == ["other-item"]

    # An explicit identity assertion unblocks the write.
    assert (
        main(
            [
                "sweep-stale",
                "--project-id",
                "other-proj",
                "--repository",
                "https://example.test/other",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert main(["--db", str(other_db), "sweep-stale"]) == 0
    capsys.readouterr()

    monkeypatch.delenv("TODO_DB_PATH", raising=False)
    monkeypatch.setenv("TODO_DB_URL", str(other_db))
    assert main(["sweep-stale"]) == 2
    assert "TODO_DB_PATH/TODO_DB_URL" in capsys.readouterr().err
    assert main(["audit", "verify"]) == 0
