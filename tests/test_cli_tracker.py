from __future__ import annotations

import json
from pathlib import Path


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
