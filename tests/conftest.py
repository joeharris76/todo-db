"""Shared test isolation."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_state_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test's object store out of the user's real cache.

    Writes and recovery default to ``TODO_DB_CACHE_DIR``; maintenance runs
    inline so no detached process outlives a test.
    """
    monkeypatch.setenv("TODO_DB_CACHE_DIR", str(tmp_path / "default-cache"))
    monkeypatch.setenv("TODO_DB_STORE_MAINTENANCE", "inline")
