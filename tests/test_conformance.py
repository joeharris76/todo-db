"""Parity conformance gate: help, exit-code, and DDL freeze."""

from pathlib import Path
import json
import subprocess
import sys


def test_parity_conformance() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/parity_conformance.py", "--check"],
        capture_output=True,
        text=True,
        check=False,
        cwd=Path(__file__).resolve().parents[1],
    )
    assert result.returncode == 0, f"parity conformance failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"


def test_parity_allowlist_is_versioned() -> None:
    root = Path(__file__).resolve().parents[1]
    data = json.loads((root / "scripts/parity_allowlist.json").read_text(encoding="utf-8"))
    assert "help" in data and "exit_codes" in data and "ddl" in data and "freeze" in data


def test_parity_snapshots_exist() -> None:
    root = Path(__file__).resolve().parents[1]
    assert (root / "scripts/parity_snapshots/standalone_help.json").is_file()
    assert (root / "scripts/parity_snapshots/standalone_ddl.json").is_file()
    assert (root / "scripts/parity_snapshots/standalone_exit.json").is_file()
