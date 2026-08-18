import json
import subprocess
from pathlib import Path

import pytest

from todo_db.cli import main


def _init_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TODO_DB_CONFIG", raising=False)
    monkeypatch.delenv("TODO_DB_PROJECT_ID", raising=False)
    monkeypatch.delenv("TODO_DB_REPOSITORY", raising=False)

    assert (
        main(
            [
                "init-project",
                "--project-id",
                "cli-agent-test",
                "--repository",
                "todo-db",
                "--wrapper",
            ]
        )
        == 0
    )
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "init repo"], cwd=tmp_path, check=True)


def test_agent_instructions_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    # Completely empty dir, no git, no db, no config
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TODO_DB_CONFIG", raising=False)
    monkeypatch.delenv("TODO_DB_PROJECT_ID", raising=False)
    monkeypatch.delenv("TODO_DB_REPOSITORY", raising=False)

    assert main(["agent", "instructions"]) == 0
    out = capsys.readouterr().out
    assert "Autonomous Agent Workflow Protocol" in out
    assert "todo agent next" in out


def test_agent_cli_rebaseline_and_generation_checked_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_repo(tmp_path, monkeypatch)
    assert main([
        "create", "claim-cli-item", "--title", "Claim CLI item", "--worktree", "todo-db",
        "--priority", "medium", "--description", "Exercise rebaseline and release generation",
    ]) == 0
    capsys.readouterr()
    assert main(["agent", "take", "claim-cli-item", "--session", "first"]) == 0
    first = json.loads(capsys.readouterr().out)
    old_token = first["claim_token"]
    assert main([
        "agent", "rebaseline", "claim-cli-item", "--reason", "confirm current clean head",
        "--claim-token", old_token,
    ]) == 0
    capsys.readouterr()
    assert main(["agent", "adopt", "claim-cli-item", "--session", "second"]) == 0
    adopted = json.loads(capsys.readouterr().out)
    new_token = adopted["claim_token"]
    assert new_token != old_token
    assert main(["agent", "release", "claim-cli-item", "--claim-token", old_token]) == 2
    capsys.readouterr()
    assert main(["agent", "release", "claim-cli-item", "--claim-token", new_token]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "released"


def test_agent_cli_full_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _init_repo(tmp_path, monkeypatch)

    # 1. Create a planned item using standard CLI
    assert (
        main(
            [
                "create",
                "item-cli-flow",
                "--title",
                "CLI Flow Item",
                "--worktree",
                "todo-db",
                "--priority",
                "high",
                "--description",
                "Flow description",
                "--work",
                "w0:Implement feature",
                "--only-modify",
                "src/**",
                "--verify",
                "smoke::true::",
            ]
        )
        == 0
    )
    capsys.readouterr()

    # 2. agent next -> reports ready item
    assert main(["agent", "next"]) == 0
    res = json.loads(capsys.readouterr().out)
    assert res["status"] == "ready"
    assert res["item"]["id"] == "item-cli-flow"
    assert res["next_action"]["action"] == "take"

    # 3. agent take -> takes item and returns context
    assert main(["agent", "take", "--session", "sess-1"]) == 0
    ctx = json.loads(capsys.readouterr().out)
    assert ctx["id"] == "item-cli-flow"
    assert ctx["claimed_session"] == "sess-1"
    token = ctx["claim_token"]
    assert token is not None

    # 4. agent claims -> lists active claim
    assert main(["agent", "claims"]) == 0
    claims = json.loads(capsys.readouterr().out)
    assert len(claims) == 1
    assert claims[0]["id"] == "item-cli-flow"

    # 5. agent context with limit
    assert main(["agent", "context", "item-cli-flow", "--unit-limit", "1"]) == 0
    ctx_sub = json.loads(capsys.readouterr().out)
    assert ctx_sub["completeness"]["work_units_shown"] == 1

    # 6. agent progress
    assert (
        main(
            [
                "agent",
                "progress",
                "item-cli-flow",
                "w0",
                "--evidence",
                "Code committed and reviewed",
                "--claim-token",
                token,
            ]
        )
        == 0
    )
    p_ctx = json.loads(capsys.readouterr().out)
    assert p_ctx["work_units"][0]["status"] == "done"

    # 7. agent finish model-assert without prior verification -> exit 1 with E_VERIFY_GATE
    assert main(["agent", "finish", "item-cli-flow", "--claim-token", token, "--model-assert"]) == 1
    err = json.loads(capsys.readouterr().err)
    assert err["code"] == "E_VERIFY_GATE"

    # 8. Human run_verifications finish -> succeeds with exit 0
    assert main(["agent", "finish", "item-cli-flow", "--claim-token", token, "--run-verifications"]) == 0
    fin = json.loads(capsys.readouterr().out)
    assert fin["status"] == "completed"
