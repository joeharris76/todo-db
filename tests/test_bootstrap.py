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
        "wrapper": "_project/scripts/todo",
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

FAKE_TURSO_MUST_NOT_RUN = """#!/usr/bin/env bash
touch "$TURSO_CALLED"
exit 99
"""

FAKE_TODO_DB_AUTH_FAILS = """#!/usr/bin/env bash
echo call >> "$STATE_FILE"
if [ "${TODO_DB_AUTH_CONTRACT:-}" != "v2" ]; then
  echo "missing v2 auth contract" >&2
  exit 88
fi
echo "error: hosted backend connection failed: [REDACTED] 401" >&2
exit 4
"""

FAKE_TODO_DB_EXITS_17 = """#!/usr/bin/env bash
echo call >> "$STATE_FILE"
exit 17
"""


def _scaffold_wrapper_repo(tmp_path: Path, fake_todo_db: str) -> tuple[Path, dict[str, str]]:
    from todo_db.cli import main

    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    assert main(["init-project", "--project-id", "wrapper-test", "--repository", "todo-db", "--wrapper"]) == 0
    config_path = tmp_path / ".todo-db" / "config.json"
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["db"] = WRAPPER_URL
    config_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    for name, content in (("turso", FAKE_TURSO_MUST_NOT_RUN), ("todo-db", fake_todo_db)):
        script = fakebin / name
        script.write_text(content, encoding="utf-8")
        script.chmod(0o755)
    env = {key: value for key, value in os.environ.items() if not key.startswith("TODO_DB_")}
    env["PATH"] = f"{fakebin}:{env['PATH']}"
    env["STATE_FILE"] = str(tmp_path / "todo-db-call-state")
    env["TURSO_CALLED"] = str(tmp_path / "turso-called")
    return tmp_path / "_project" / "scripts" / "todo", env


@pytest.mark.skipif(shutil.which("git") is None, reason="the wrapper scaffold test uses a git repo")
def test_wrapper_exports_v2_contract_runs_once_and_never_calls_turso(tmp_path: Path) -> None:
    wrapper, env = _scaffold_wrapper_repo(tmp_path, FAKE_TODO_DB_AUTH_FAILS)
    result = subprocess.run([str(wrapper), "list"], cwd=tmp_path, env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 4
    assert (tmp_path / "todo-db-call-state").read_text(encoding="utf-8").splitlines() == ["call"]
    assert not (tmp_path / "turso-called").exists()
    assert "automatic token minting is disabled" in result.stderr
    assert "tokens create" not in result.stdout + result.stderr


@pytest.mark.skipif(shutil.which("git") is None, reason="the wrapper scaffold test uses a git repo")
def test_wrapper_preserves_non_auth_exit_code_without_retry(tmp_path: Path) -> None:
    wrapper, env = _scaffold_wrapper_repo(tmp_path, FAKE_TODO_DB_EXITS_17)
    result = subprocess.run([str(wrapper), "list"], cwd=tmp_path, env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 17
    assert (tmp_path / "todo-db-call-state").read_text(encoding="utf-8").splitlines() == ["call"]
    assert not (tmp_path / "turso-called").exists()


def test_refresh_wrapper_replaces_only_a_recognized_legacy_wrapper(tmp_path: Path) -> None:
    from todo_db.cli import WRAPPER_VERSION_MARKER, main

    assert main(["init-project", "--project-id", "refresh-test", "--repository", "todo-db", "--wrapper"]) == 0
    config_path = tmp_path / ".todo-db" / "config.json"
    gitignore_path = tmp_path / ".todo-db" / ".gitignore"
    wrapper = tmp_path / "_project" / "scripts" / "todo"
    config_before = config_path.read_bytes()
    gitignore_before = gitignore_path.read_bytes()
    wrapper.write_text(wrapper.read_text(encoding="utf-8").replace(f"{WRAPPER_VERSION_MARKER}\n", ""), encoding="utf-8")

    assert main(["refresh-wrapper"]) == 0
    assert WRAPPER_VERSION_MARKER in wrapper.read_text(encoding="utf-8")
    assert config_path.read_bytes() == config_before
    assert gitignore_path.read_bytes() == gitignore_before


def test_refresh_wrapper_refuses_an_unrecognized_file(tmp_path: Path, capsys) -> None:
    from todo_db.cli import main

    assert main(["init-project", "--project-id", "refresh-test", "--repository", "todo-db", "--wrapper"]) == 0
    wrapper = tmp_path / "_project" / "scripts" / "todo"
    wrapper.write_text("#!/bin/sh\necho custom\n", encoding="utf-8")

    assert main(["refresh-wrapper"]) == 2
    assert "refusing to replace unrecognized wrapper" in capsys.readouterr().err
    assert wrapper.read_text(encoding="utf-8") == "#!/bin/sh\necho custom\n"


def test_doctor_fails_with_targeted_remediation_for_a_legacy_wrapper(tmp_path: Path, capsys) -> None:
    from todo_db.cli import WRAPPER_VERSION_MARKER, main

    assert main(["init-project", "--project-id", "refresh-test", "--repository", "todo-db", "--wrapper"]) == 0
    capsys.readouterr()
    wrapper = tmp_path / "_project" / "scripts" / "todo"
    wrapper.write_text(wrapper.read_text(encoding="utf-8").replace(f"{WRAPPER_VERSION_MARKER}\n", ""), encoding="utf-8")

    assert main(["doctor"]) == 2
    output = capsys.readouterr().out
    assert "FAIL wrapper: legacy generated wrapper" in output
    assert "todo-db refresh-wrapper" in output


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


def test_custom_wrapper_depth_derives_correct_repo_root(tmp_path: Path) -> None:
    from todo_db.cli import main

    assert (
        main(
            [
                "init-project",
                "--project-id",
                "custom-wrapper",
                "--repository",
                "todo-db",
                "--wrapper",
                "scripts/nested/deep/todo",
            ]
        )
        == 0
    )
    wrapper = tmp_path / "scripts" / "nested" / "deep" / "todo"
    content = wrapper.read_text(encoding="utf-8")
    assert 'REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)"' in content


def test_e_no_project_pre_open_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from todo_db.cli import main

    # In a clean directory with no config, no git, no flags
    clean_dir = tmp_path / "empty_dir"
    clean_dir.mkdir()
    monkeypatch.chdir(clean_dir)
    monkeypatch.delenv("TODO_DB_CONFIG", raising=False)
    monkeypatch.delenv("TODO_DB_PROJECT_ID", raising=False)
    monkeypatch.delenv("TODO_DB_REPOSITORY", raising=False)

    assert main(["ready"]) == 2
    err = capsys.readouterr().err
    assert "E_NO_PROJECT" in err
