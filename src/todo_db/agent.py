"""Claim-coordinated agent workflow service, projections, and Git scope engine."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import TodoError
from .tracker import TodoTracker, utc_now


@dataclass(frozen=True)
class GitState:
    root: Path
    head_sha: str | None
    branch: str | None
    is_clean: bool


class GitScopeEngine:
    """Root-safe, NUL-delimited Git changed-file and baseline divergence inspector."""

    def __init__(self, repo_root: Path | None = None):
        self.repo_root = (repo_root or self._find_repo_root()).resolve()

    @staticmethod
    def _find_repo_root(start: Path | None = None) -> Path:
        probe = (start or Path.cwd()).resolve()
        while True:
            if (probe / ".git").exists():
                return probe
            if probe.parent == probe:
                return Path.cwd().resolve()
            probe = probe.parent

    def capture_state(self) -> GitState:
        head = self._run(["rev-parse", "HEAD"])
        branch = self._run(["branch", "--show-current"])
        status = self._run(["status", "--porcelain"])
        return GitState(
            root=self.repo_root,
            head_sha=head.strip() if head else None,
            branch=branch.strip() if branch else None,
            is_clean=len(status.strip()) == 0,
        )

    def _run(self, args: list[str]) -> str:
        proc = subprocess.run(
            ["git", "-C", str(self.repo_root), *args],
            capture_output=True,
            text=False,
            check=False,
        )
        if proc.returncode != 0:
            return ""
        return proc.stdout.decode("utf-8", errors="replace")

    def _run_bytes(self, args: list[str]) -> bytes:
        proc = subprocess.run(
            ["git", "-C", str(self.repo_root), *args],
            capture_output=True,
            text=False,
            check=False,
        )
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", errors="replace")
            raise TodoError(f"git command failed ({' '.join(args)}): {err.strip()}")
        return proc.stdout

    def changed_files(self, base: str | None = None) -> list[str]:
        """Return root-relative changed, added, deleted, renamed, and untracked files."""
        changed: set[str] = set()

        if base:
            # Check baseline reachability
            proc = subprocess.run(
                ["git", "-C", str(self.repo_root), "rev-parse", "--verify", f"{base}^{{commit}}"],
                capture_output=True,
                check=False,
            )
            if proc.returncode != 0:
                raise TodoError(f"baseline commit {base!r} unreachable: E_BASE_UNREACHABLE")

            # Check ancestor relationship
            anc_proc = subprocess.run(
                ["git", "-C", str(self.repo_root), "merge-base", "--is-ancestor", base, "HEAD"],
                capture_output=True,
                check=False,
            )
            if anc_proc.returncode != 0:
                raise TodoError(
                    f"baseline commit {base!r} has diverged from HEAD: E_BASE_DIVERGED; "
                    "audited human rebaseline required"
                )

            # Diff between baseline and HEAD
            raw_diff = self._run_bytes(["diff", "-z", "--name-status", base, "HEAD"])
            self._parse_diff_z(raw_diff, changed)

        # Working tree and index changes vs HEAD
        raw_status = self._run_bytes(["status", "-z", "--porcelain=v1"])
        self._parse_status_z(raw_status, changed)

        return sorted(changed)

    def _parse_diff_z(self, data: bytes, out: set[str]) -> None:
        parts = data.split(b"\x00")
        i = 0
        while i < len(parts):
            if not parts[i]:
                i += 1
                continue
            status_code = parts[i].decode("utf-8", errors="replace")
            i += 1
            if not status_code:
                continue
            if status_code.startswith("R") or status_code.startswith("C"):
                if i < len(parts) and parts[i]:
                    out.add(parts[i].decode("utf-8", errors="replace"))
                    i += 1
                if i < len(parts) and parts[i]:
                    out.add(parts[i].decode("utf-8", errors="replace"))
                    i += 1
            else:
                if i < len(parts) and parts[i]:
                    out.add(parts[i].decode("utf-8", errors="replace"))
                    i += 1

    def _parse_status_z(self, data: bytes, out: set[str]) -> None:
        parts = data.split(b"\x00")
        i = 0
        while i < len(parts):
            if not parts[i]:
                i += 1
                continue
            entry = parts[i]
            i += 1
            if len(entry) < 3:
                continue
            status_tag = entry[:2].decode("utf-8", errors="replace")
            path1 = entry[3:].decode("utf-8", errors="replace")
            out.add(path1)
            if "R" in status_tag or "C" in status_tag:
                if i < len(parts) and parts[i]:
                    path2 = parts[i].decode("utf-8", errors="replace")
                    out.add(path2)
                    i += 1
