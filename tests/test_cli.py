"""Minimal CLI proving cases: bootstrap, validate, migrate, recover, reads."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from todo_db import git_backend
from todo_db.cli import main
from todo_db.git_backend import StateRef
from todo_db.service import TrackerService


def _remote(tmp_path: Path, name: str = "state.git") -> str:
    remote = tmp_path / name
    subprocess.run(["git", "init", "--quiet", "--bare", str(remote)], check=True)
    return str(remote)


def _args(remote: str, *extra: str) -> list[str]:
    return ["--state-remote", remote, "--state-branch", "todo-state", *extra]


def test_bootstrap_validate_and_collision(tmp_path: Path, capsys) -> None:
    remote = _remote(tmp_path)
    assert main(["bootstrap", *_args(remote), "--write-config", "--repo-root", str(tmp_path)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["branch"] == "todo-state"
    config = json.loads((tmp_path / ".todo-db" / "config.json").read_text())
    assert config["state_remote"] == remote
    assert main(["validate", *_args(remote)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["items"] == 0 and report["stale"] is False
    # Bootstrap refuses to overwrite an existing state branch.
    assert main(["bootstrap", *_args(remote)]) == 2


def test_list_show_recover_round_trip(tmp_path: Path, capsys) -> None:
    remote = _remote(tmp_path)
    assert main(["bootstrap", *_args(remote)]) == 0
    capsys.readouterr()
    ref = StateRef(remote=remote, branch="todo-state")
    svc = TrackerService(ref=ref, cache_dir=tmp_path / "cache", worker="seeder")
    assert svc.create_item("cli-t1", "CLI task", description="visible")["ok"]
    assert main(["list", *_args(remote)]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["ok"] and listed["data"]["total"] == 1
    assert main(["show", *_args(remote), "cli-t1"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["ok"] and shown["data"]["description"] == "visible"
    assert main(["show", *_args(remote), "missing"]) == 2
    capsys.readouterr()
    assert main(["recover", *_args(remote), "--limit", "5"]) == 0
    history = json.loads(capsys.readouterr().out)["history"]
    assert len(history) >= 2


def test_recover_op_id_and_restore(tmp_path: Path, capsys) -> None:
    remote = _remote(tmp_path)
    assert main(["bootstrap", *_args(remote)]) == 0
    capsys.readouterr()
    ref = StateRef(remote=remote, branch="todo-state")
    op_id = git_backend.new_op_id()
    from todo_db import store as S

    outcome = git_backend.mutate(
        ref, op="create", summary="seed", worker="w", op_id=op_id,
        apply=lambda snap: (S.op_create(snap, item_id="s1", title="Seed"), {"id": "s1"}),
    )
    assert outcome.ok
    assert main(["recover", *_args(remote), "--op-id", op_id]) == 0
    found = json.loads(capsys.readouterr().out)
    assert found["applied"] and found["rev"] == outcome.sha
    assert main(["recover", *_args(remote), "--op-id", "0" * 32]) == 0
    assert json.loads(capsys.readouterr().out)["applied"] is False
    # Append-only restore of the bootstrap revision drops the seeded item
    # without rewriting history.
    first = git_backend.history(ref, limit=10)[-1]["sha"]
    assert main(["recover", *_args(remote), "--restore-rev", first, "--actor", "w"]) == 0
    restored = json.loads(capsys.readouterr().out)
    assert restored["restored_to"] == first
    ro = git_backend.read(ref, tmp_path / "cache")
    assert "s1" not in ro.snapshot.index["items"]
    assert len(git_backend.history(ref, limit=10)) >= 3


def test_migrate_via_cli(tmp_path: Path, capsys) -> None:
    remote = _remote(tmp_path)
    ref = StateRef(remote=remote, branch="todo-state")
    assert main(["bootstrap", *_args(remote)]) == 0
    capsys.readouterr()
    export = {
        "format_version": 2,
        "project": {"project_id": "p", "repository": "https://example.test/p"},
        "tables": {
            "items": [{
                "id": "m1", "title": "Migrated", "worktree": "t", "priority": "high",
                "state": "planning", "blocked_reason": None, "category": "c",
                "description": "old desc", "approach": None, "claimed_by": None,
                "claimed_at": None, "claimed_session": None, "claim_token": None,
                "claimed_branch": None, "claimed_worktree": None, "git_baseline": None,
                "created_at": None, "completed_at": None, "completed_pr": None,
            }],
            "item_deps": [], "work_units": [], "work_needs": [], "scope_rules": [],
            "verifications": [], "preserves": [], "anti_patterns": [], "prior_art": [],
            "deferrals": [], "meta": [],
        },
    }
    source = tmp_path / "export.json"
    source.write_text(json.dumps(export))
    assert main(["migrate", *_args(remote), "--from-export", str(source), "--dry-run"]) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["dry_run"] and dry["items"] == 1 and not dry["applied"]
    assert main(["migrate", *_args(remote), "--from-export", str(source),
                 "--backup-dir", str(tmp_path / "bak")]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["applied"] and "rollback" in applied
    ro = git_backend.read(ref, tmp_path / "cache")
    assert ro.snapshot.index["items"]["m1"]["title"] == "Migrated"
