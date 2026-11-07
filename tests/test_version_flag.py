"""`--version` on both entry points works with no config, remote, or network."""

from __future__ import annotations

import shutil
import subprocess
import sys

import pytest

from tractable import TOOL_VERSION
from tractable import cli
from tractable.mcp import server


@pytest.mark.parametrize(
    ("main", "prog"),
    [(cli.main, "tractable"), (server.main, "tractable-mcp")],
)
def test_version_flag_exits_zero_without_config(main, prog, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert not (tmp_path / ".tractable").exists()
    for var in ("TRACTABLE_STATE_REMOTE", "TRACTABLE_STATE_BRANCH", "TRACTABLE_CONFIG"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out == f"{prog} {TOOL_VERSION}\n"


@pytest.mark.parametrize("prog", ["tractable", "tractable-mcp"])
def test_version_flag_console_script(prog, tmp_path):
    exe = shutil.which(prog, path=str(__import__("pathlib").Path(sys.executable).parent))
    if exe is None:
        pytest.skip(f"{prog} console script not installed")
    result = subprocess.run([exe, "--version"], cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0
    assert result.stdout == f"{prog} {TOOL_VERSION}\n"
