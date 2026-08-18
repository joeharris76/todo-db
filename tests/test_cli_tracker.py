from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_cli_complete_help_exposes_verification_override(capsys) -> None:
    from todo_db.cli import main

    with pytest.raises(SystemExit) as raised:
        main(["complete", "--help"])
    assert raised.value.code == 0
    help_text = capsys.readouterr().out
    assert "verification ladder" in help_text
    assert "--override-verification REASON" in help_text


def test_cli_create_lifecycle_and_export(tmp_path: Path, capsys) -> None:
    from todo_db.cli import main

    db_path = tmp_path / "standalone.sqlite"
    common = ["--db", str(db_path), "--project-id", "cli-test", "--repository", "todo-db"]
    assert main([*common, "init"]) == 0
    capsys.readouterr()
    assert (
        main(
            [
                *common,
                "create",
                "cli-item",
                "--title",
                "CLI item",
                "--worktree",
                "todo-db",
                "--priority",
                "high",
                "--description",
                "A CLI-created item for an end-to-end test.",
                "--work",
                "w0:Run the test",
                "--verify",
                "smoke::printf PASS::PASS",
                "--only-modify",
                "src/**",
            ]
        )
        == 0
    )
    assert main([*common, "claim", "cli-item"]) == 0
    assert main([*common, "done", "cli-item", "w0", "--evidence", "CLI test evidence"]) == 0
    assert main([*common, "complete", "cli-item"]) == 0
    export_path = tmp_path / "export.json"
    assert main([*common, "export", "--output", str(export_path)]) == 0
    assert json.loads(export_path.read_text(encoding="utf-8"))["tables"]["items"][0]["state"] == "done"
    assert "cli-item done" in capsys.readouterr().out


def test_cli_complete_requires_passing_verification_or_audited_override(tmp_path: Path, capsys) -> None:
    from todo_db.cli import main

    db_path = tmp_path / "standalone.sqlite"
    common = [
        "--db",
        str(db_path),
        "--project-id",
        "cli-complete-test",
        "--repository",
        "todo-db",
        "--actor",
        "cli-actor",
    ]
    assert main([*common, "init"]) == 0
    capsys.readouterr()
    assert (
        main(
            [
                *common,
                "create",
                "failing-item",
                "--title",
                "Failing completion item",
                "--worktree",
                "todo-db",
                "--priority",
                "high",
                "--description",
                "The CLI completion gate runs this failing verification.",
                "--verify",
                "vacuous selector::exit 5",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert main([*common, "claim", "failing-item"]) == 0
    capsys.readouterr()
    assert main([*common, "complete", "failing-item"]) == 2
    assert "verification seq=1 failed" in capsys.readouterr().err

    reason = "External release gate supplied equivalent evidence"
    assert main([*common, "complete", "failing-item", "--override-verification", reason]) == 0
    capsys.readouterr()
    export_path = tmp_path / "completion-export.json"
    assert main([*common, "export", "--output", str(export_path)]) == 0
    completion = [
        event
        for event in json.loads(export_path.read_text(encoding="utf-8"))["events"]
        if event["action"] == "complete"
    ][-1]
    assert completion["actor"] == "cli-actor"
    assert completion["detail"]["verification_override"]["reason"] == reason


def test_cli_release_is_holder_only_via_cli(tmp_path: Path, capsys) -> None:
    """CLI regression: non-holder release exits 2 per cli.py:1273 TodoError mapping."""

    from todo_db.cli import main

    db_path = tmp_path / "standalone.sqlite"
    base = ["--db", str(db_path), "--project-id", "cli-test", "--repository", "todo-db"]
    assert main([*base, "--actor", "alice", "init"]) == 0
    capsys.readouterr()
    assert (
        main(
            [
                *base,
                "--actor",
                "alice",
                "create",
                "lease-item",
                "--title",
                "Lease item",
                "--worktree",
                "todo-db",
                "--priority",
                "medium",
                "--description",
                "Item used to verify holder-only release via CLI.",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert main([*base, "--actor", "alice", "claim", "lease-item"]) == 0
    capsys.readouterr()
    # Bob's release must map TodoError -> exit 2
    assert main([*base, "--actor", "bob", "release", "lease-item"]) == 2
    assert "only the holder can release" in capsys.readouterr().err
    # Holder can release (exit 0)
    assert main([*base, "--actor", "alice", "release", "lease-item"]) == 0
    capsys.readouterr()
    # Unclaimed is no-op (exit 0)
    assert main([*base, "--actor", "bob", "release", "lease-item"]) == 0


def test_cli_yaml_import_requires_explicit_source_and_preserves_items(tmp_path: Path, capsys) -> None:
    from todo_db.cli import main

    todo_dir = tmp_path / "TODO"
    todo_dir.mkdir()
    (todo_dir / "example.yaml").write_text(
        "id: yaml-item\n"
        "title: Imported item\n"
        "worktree: todo-db\n"
        "priority: High\n"
        "description: Imported from the legacy YAML tree.\n"
        "work:\n"
        "  - id: w0\n"
        "    summary: Import and validate\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "standalone.sqlite"
    common = ["--db", str(db_path), "--project-id", "yaml-test", "--repository", "todo-db"]
    assert main([*common, "import-yaml", "--todo-dir", str(todo_dir)]) == 0
    assert "yaml-item" in capsys.readouterr().out
    assert main([*common, "show", "yaml-item", "--json"]) == 0
    assert '"id": "yaml-item"' in capsys.readouterr().out


def test_cli_fields_limit_and_max_bytes_compact_contracts(tmp_path: Path, capsys) -> None:
    from todo_db.cli import main

    db_path = tmp_path / "compact.sqlite"
    common = ["--db", str(db_path), "--project-id", "compact-test", "--repository", "todo-db"]
    assert main([*common, "init"]) == 0

    for i in range(5):
        assert (
            main(
                [
                    *common,
                    "create",
                    f"item-{i:02d}",
                    "--title",
                    f"Item {i}",
                    "--worktree",
                    "todo-db",
                    "--priority",
                    "high",
                    "--description",
                    f"Description {i}",
                ]
            )
            == 0
        )
    capsys.readouterr()

    # Test --limit
    assert main([*common, "--limit", "2", "list"]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 2

    # Test --fields on list
    assert main([*common, "--fields", "id,priority", "list"]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert out[0] == "item-00 high"

    # Test --fields on show --json
    assert main([*common, "--fields", "id,title", "show", "item-00", "--json"]) == 0
    item_json = json.loads(capsys.readouterr().out)
    assert set(item_json.keys()) == {"id", "title"}

    # Test --max-bytes truncation
    assert main([*common, "--max-bytes", "30", "list"]) == 0
    out = capsys.readouterr().out
    assert "... [truncated:" in out


def test_cli_verification_output_is_bounded(tmp_path: Path, capsys) -> None:
    from todo_db.cli import main

    db_path = tmp_path / "verify_bounded.sqlite"
    common = ["--db", str(db_path), "--project-id", "verify-test", "--repository", "todo-db"]
    assert main([*common, "init"]) == 0

    assert (
        main(
            [
                *common,
                "create",
                "large-verify",
                "--title",
                "Large verify output",
                "--worktree",
                "todo-db",
                "--priority",
                "high",
                "--description",
                "Produces >4KB output",
                "--verify",
                "loud::python3 -c \"print('A' * 6000)\"",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert main([*common, "verify", "large-verify", "--run", "1"]) == 0
    out = capsys.readouterr().out
    assert "seq 1: pass" in out
    assert "... [truncated:" in out
