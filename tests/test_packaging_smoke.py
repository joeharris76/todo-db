"""Real distribution-artifact installation smoke tests."""

from __future__ import annotations

import subprocess
import tarfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_wheel_and_sdist_build_and_wheel_cli_smoke(tmp_path: Path) -> None:
    distribution_dir = tmp_path / "dist"
    build = subprocess.run(
        ["uv", "build", "--out-dir", str(distribution_dir)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert build.returncode == 0, build.stderr
    wheels = list(distribution_dir.glob("todo_db-*.whl"))
    sdists = list(distribution_dir.glob("todo_db-*.tar.gz"))
    assert len(wheels) == 1
    assert len(sdists) == 1

    with tarfile.open(sdists[0], "r:gz") as archive:
        names = archive.getnames()
    forbidden_parts = {".claude", ".codex", ".gemini", "node_modules", ".todo-db", ".todo-batch"}
    assert not any(forbidden_parts.intersection(Path(name).parts) for name in names)
    assert not any(Path(name).name.startswith("skill-sync") for name in names)
    assert sdists[0].stat().st_size < 2 * 1024 * 1024

    for distribution in (*wheels, *sdists):
        for command in ("todo-db", "todo"):
            smoke = subprocess.run(
                ["uv", "run", "--isolated", "--with", str(distribution), "--", command, "--help"],
                cwd=tmp_path,
                capture_output=True,
                text=True,
                check=False,
            )
            assert smoke.returncode == 0, smoke.stderr
            assert "database-backed TODO tracker" in smoke.stdout
