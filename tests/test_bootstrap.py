"""New-project bootstrap: config discovery, identity precedence, and init-project scaffolding."""

from __future__ import annotations

import json
import os
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

    assert (
        main(
            [
                "create",
                "disc-item",
                "--title",
                "Discovered item",
                "--worktree",
                "local",
                "--priority",
                "medium",
                "--description",
                "Created with identity and db discovered from config.",
            ]
        )
        == 0
    )
    assert main(["show", "disc-item", "--json"]) == 0
    assert json.loads(capsys.readouterr().out.split("created disc-item\n")[-1])["id"] == "disc-item"


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

    assert (
        main(
            [
                "--db",
                str(db_path),
                "create",
                "bound-item",
                "--title",
                "Bound item",
                "--worktree",
                "local",
                "--priority",
                "medium",
                "--description",
                "Written without any caller-supplied identity.",
            ]
        )
        == 0
    )
    assert main(["--db", str(db_path), "list"]) == 0
    assert "bound-item" in capsys.readouterr().out

    # The mismatch guard still enforces when the caller asserts an identity.
    assert main(["--db", str(db_path), "list", "--project-id", "other", "--repository", "todo-db"]) == 2
    assert "project identity mismatch" in capsys.readouterr().err


def test_unbound_database_refuses_identityless_writes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from todo_db.cli import main

    assert (
        main(
            [
                "--db",
                str(tmp_path / "fresh.sqlite"),
                "create",
                "orphan-item",
                "--title",
                "Orphan",
                "--worktree",
                "local",
                "--priority",
                "medium",
                "--description",
                "Must not bind an identity implicitly.",
            ]
        )
        == 2
    )
    assert "no bound project identity" in capsys.readouterr().err


def test_init_project_scaffolds_config_gitignore_and_wrapper(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from todo_db.cli import main

    identity = ["--project-id", "scaffold-test", "--repository", "https://example.test/scaffold"]
    assert main(["init-project", *identity, "--wrapper"]) == 0
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

    wrapper = tmp_path / "_project" / "scripts" / "todo"
    content = wrapper.read_text(encoding="utf-8")
    assert os.access(wrapper, os.X_OK)
    assert 'TODO_DB_CONFIG="${TODO_DB_CONFIG:-$REPO_ROOT/.todo-db/config.json}"' in content
    assert "command -v todo-db" in content
    assert 'TODO_DB_TOOL="$REPO_ROOT/../todo-db"' in content
    assert "--project-id" not in content, "identity comes from the config file, not hardcoded flags"

    # The scaffolded repo now serves identityless calls via discovery.
    assert main(["stats"]) == 0
    assert json.loads(capsys.readouterr().out)["items_by_state"] == {}


def test_init_project_is_idempotent_only_with_force(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from todo_db.cli import main

    identity = ["--project-id", "force-test", "--repository", "todo-db"]
    assert main(["init-project", *identity, "--wrapper", "bin/todo"]) == 0
    capsys.readouterr()

    assert main(["init-project", *identity, "--wrapper", "bin/todo"]) == 2
    err = capsys.readouterr().err
    assert "refusing to overwrite" in err and "--force" in err

    assert main(["init-project", *identity, "--wrapper", "bin/todo", "--force"]) == 0
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
    assert main(["list"]) == 0


WRAPPER_URL = "libsql://wrapper-test.aws-us-east-1.turso.io"
WRAPPER_MARKER_TOKEN = "wrapper-marker-token"

FAKE_TURSO_AUTHENTICATED = f"""#!/usr/bin/env bash
case "$1 ${{2:-}}" in
  "auth whoami") echo "fake-user"; exit 0 ;;
  "db list")
    printf 'NAME            GROUP    URL\\n'
    printf 'other-db        default  libsql://other-db.aws-us-east-1.turso.io\\n'
    printf 'wrapper-test    default  {WRAPPER_URL}\\n'
    exit 0 ;;
  "db tokens")
    if [ "${{3:-}}" = "create" ] && [ "${{4:-}}" = "wrapper-test" ]; then
      echo "{WRAPPER_MARKER_TOKEN}"
      exit 0
    fi
    exit 1 ;;
esac
exit 1
"""

FAKE_TURSO_LOGGED_OUT = """#!/usr/bin/env bash
if [ "$1 ${2:-}" = "auth whoami" ]; then
  echo "You are not logged in." >&2
  exit 1
fi
exit 1
"""

FAKE_TODO_DB_RETRY_AWARE = f"""#!/usr/bin/env bash
if [ ! -f "$STATE_FILE" ]; then
  : > "$STATE_FILE"
  echo "error: hosted backend sync failed: [REDACTED] 401" >&2
  exit 4
fi
if [ "${{TODO_DB_AUTH_TOKEN:-}}" = "{WRAPPER_MARKER_TOKEN}" ]; then
  echo "retried ok"
  exit 0
fi
echo "retry ran without the minted token" >&2
exit 88
"""

FAKE_TODO_DB_ALWAYS_AUTH_FAILS = """#!/usr/bin/env bash
echo "error: hosted backend sync failed: [REDACTED] 401" >&2
exit 4
"""


def _scaffold_wrapper_repo(tmp_path: Path, fake_turso: str, fake_todo_db: str) -> tuple[Path, dict[str, str]]:
    from todo_db.cli import main

    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    assert main(["init-project", "--project-id", "wrapper-test", "--repository", "todo-db", "--wrapper"]) == 0
    config_path = tmp_path / ".todo-db" / "config.json"
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["db"] = WRAPPER_URL
    config_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    for name, content in (("turso", fake_turso), ("todo-db", fake_todo_db)):
        script = fakebin / name
        script.write_text(content, encoding="utf-8")
        script.chmod(0o755)
    env = {key: value for key, value in os.environ.items() if not key.startswith("TODO_DB_")}
    env["PATH"] = f"{fakebin}:{env['PATH']}"
    env["STATE_FILE"] = str(tmp_path / "todo-db-call-state")
    return tmp_path / "_project" / "scripts" / "todo", env


@pytest.mark.skipif(shutil.which("git") is None, reason="the wrapper scaffold test uses a git repo")
def test_wrapper_remints_a_token_and_retries_once_on_auth_exit_4(tmp_path: Path) -> None:
    wrapper, env = _scaffold_wrapper_repo(tmp_path, FAKE_TURSO_AUTHENTICATED, FAKE_TODO_DB_RETRY_AWARE)
    result = subprocess.run([str(wrapper), "list"], cwd=tmp_path, env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "retried ok" in result.stdout
    assert "retrying once" in result.stderr
    assert WRAPPER_MARKER_TOKEN not in result.stdout + result.stderr, "the minted token must never be echoed"


@pytest.mark.skipif(shutil.which("git") is None, reason="the wrapper scaffold test uses a git repo")
def test_wrapper_prints_the_alert_block_and_exits_4_when_the_turso_cli_is_logged_out(tmp_path: Path) -> None:
    wrapper, env = _scaffold_wrapper_repo(tmp_path, FAKE_TURSO_LOGGED_OUT, FAKE_TODO_DB_ALWAYS_AUTH_FAILS)
    result = subprocess.run([str(wrapper), "list"], cwd=tmp_path, env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 4
    assert "TODO-DB AUTH ALERT" in result.stderr
    assert "Tracker writes are BLOCKED" in result.stderr
    assert "turso auth login" in result.stderr
    assert "turso db tokens create" in result.stderr
    assert "Do not continue batch work until resolved" in result.stderr


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
