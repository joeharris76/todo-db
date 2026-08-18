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

    def is_git_repo(self) -> bool:
        return (self.repo_root / ".git").exists() or (self.repo_root / ".git").is_file()

    def _run_bytes(self, args: list[str]) -> bytes:
        if not self.is_git_repo():
            return b""
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


class AgentWorkflow:
    """Streamlined, claim-coordinated workflow service for autonomous agents."""

    def __init__(self, tracker: TodoTracker, repo_root: Path | None = None):
        self.tracker = tracker
        self.database = tracker.database
        self.git_engine = GitScopeEngine(repo_root)

    def current_claim(self, principal: str | None = None) -> dict[str, Any] | None:
        p = principal or self.tracker.actor
        row = self.database.connection.execute(
            "SELECT * FROM items WHERE claimed_by = ? AND state = 'active' ORDER BY priority LIMIT 1",
            (p,),
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def next(self, principal: str | None = None) -> dict[str, Any]:
        claim = self.current_claim(principal)
        if claim:
            item_id = claim["id"]
            order = self.tracker.work_order(item_id)
            ready_units = order.get("ready_units", [])
            if ready_units:
                next_u = ready_units[0]
                next_action = {
                    "action": "progress",
                    "item_id": item_id,
                    "wid": next_u["wid"],
                    "summary": next_u["summary"],
                    "command": f"todo agent progress {item_id} {next_u['wid']} --evidence '<evidence>'",
                }
            else:
                next_action = {
                    "action": "finish",
                    "item_id": item_id,
                    "command": f"todo agent finish {item_id}",
                }
            return {
                "status": "claimed",
                "item": {
                    "id": item_id,
                    "title": claim["title"],
                    "priority": claim["priority"],
                    "worktree": claim["worktree"],
                    "claimed_at": claim["claimed_at"],
                    "claim_token": claim.get("claim_token"),
                    "git_baseline": claim.get("git_baseline"),
                },
                "next_action": next_action,
            }

        ready = self.tracker.ready_items()
        if ready:
            top = ready[0]
            return {
                "status": "ready",
                "item": {
                    "id": top["id"],
                    "title": top["title"],
                    "priority": top["priority"],
                    "worktree": top["worktree"],
                },
                "next_action": {
                    "action": "take",
                    "item_id": top["id"],
                    "command": f"todo agent take {top['id']}",
                },
            }

        return {
            "status": "idle",
            "item": None,
            "next_action": {
                "action": "wait",
                "details": "Queue is empty. No ready items available.",
            },
        }

    def take(
        self,
        item_id: str | None = None,
        *,
        session: str | None = None,
        worktree: str | None = None,
        branch: str | None = None,
    ) -> dict[str, Any]:
        """Atomically adopt existing claim or take highest-priority/specified item."""
        existing = self.current_claim()
        if existing:
            if item_id is not None and existing["id"] != item_id:
                raise TodoError(
                    f"actor {self.tracker.actor!r} already holds active claim on {existing['id']!r}; "
                    f"release it first with `todo agent release {existing['id']}` before taking {item_id!r}"
                )
            if session and existing.get("claimed_session") != session:
                with self.database.transaction():
                    self.database.connection.execute(
                        "UPDATE items SET claimed_session = ? WHERE id = ?",
                        (session, existing["id"]),
                    )
            return self.context(existing["id"])

        target_id = item_id
        if target_id is None:
            ready = self.tracker.ready_items()
            if not ready:
                raise TodoError("cannot take item: no ready items in queue")
            target_id = ready[0]["id"]

        git_state = self.git_engine.capture_state()
        self.tracker.claim(
            target_id,
            session=session,
            branch=branch or git_state.branch,
            worktree=worktree or str(git_state.root),
            git_baseline=git_state.head_sha,
        )
        return self.context(target_id)

    def context(
        self,
        item_id: str,
        *,
        fields: list[str] | None = None,
        unit_limit: int | None = None,
    ) -> dict[str, Any]:
        """Return bounded agent projection with mandatory guardrails."""
        row = self.database.connection.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise TodoError(f"item not found: {item_id!r}")
        item_dict = dict(row)

        units_rows = self.database.connection.execute(
            "SELECT wid, summary, status, evidence, notes FROM work_units WHERE item_id = ? ORDER BY wid",
            (item_id,),
        ).fetchall()
        needs_rows = self.database.connection.execute(
            "SELECT wid, needs_wid FROM work_needs WHERE item_id = ?",
            (item_id,),
        ).fetchall()
        needs_by_wid: dict[str, list[str]] = {}
        for r in needs_rows:
            needs_by_wid.setdefault(r["wid"], []).append(r["needs_wid"])

        done_wids = {r["wid"] for r in units_rows if r["status"] == "done"}
        work_units: list[dict[str, Any]] = []
        for u in units_rows:
            w_needs = sorted(needs_by_wid.get(u["wid"], []))
            is_ready = u["status"] != "done" and all(n in done_wids for n in w_needs)
            work_units.append(
                {
                    "id": u["wid"],
                    "summary": u["summary"],
                    "status": u["status"],
                    "evidence": u["evidence"],
                    "needs": w_needs,
                    "ready": is_ready,
                }
            )

        total_units = len(work_units)
        if unit_limit is not None and unit_limit >= 0:
            displayed_units = work_units[:unit_limit]
        else:
            displayed_units = work_units

        scope_rows = self.database.connection.execute(
            "SELECT kind, path_glob FROM scope_rules WHERE item_id = ? ORDER BY kind, path_glob",
            (item_id,),
        ).fetchall()
        scope = [{"kind": r["kind"], "path_glob": r["path_glob"]} for r in scope_rows]

        preserves_rows = self.database.connection.execute(
            "SELECT behavior FROM preserves WHERE item_id = ? ORDER BY behavior",
            (item_id,),
        ).fetchall()
        preserves = [r["behavior"] for r in preserves_rows]

        anti_rows = self.database.connection.execute(
            "SELECT dont, why, instead FROM anti_patterns WHERE item_id = ? ORDER BY dont",
            (item_id,),
        ).fetchall()
        anti_patterns = [{"dont": r["dont"], "why": r["why"], "instead": r["instead"]} for r in anti_rows]

        verif_rows = self.database.connection.execute(
            "SELECT seq, description, expected, last_run, last_result FROM verifications WHERE item_id = ? ORDER BY seq",
            (item_id,),
        ).fetchall()
        verifications = [
            {
                "seq": r["seq"],
                "description": r["description"],
                "expected": r["expected"],
                "last_run": r["last_run"],
                "last_result": r["last_result"],
            }
            for r in verif_rows
        ]

        git_state = self.git_engine.capture_state()

        ready_units = [u for u in work_units if u["ready"]]
        if ready_units:
            next_action = {
                "action": "progress",
                "item_id": item_id,
                "wid": ready_units[0]["id"],
                "summary": ready_units[0]["summary"],
                "command": f"todo agent progress {item_id} {ready_units[0]['id']} --evidence '<evidence>'",
            }
        else:
            next_action = {
                "action": "finish",
                "item_id": item_id,
                "command": f"todo agent finish {item_id}",
            }

        ctx = {
            "id": item_dict["id"],
            "title": item_dict["title"],
            "priority": item_dict["priority"],
            "state": item_dict["state"],
            "worktree": item_dict["worktree"],
            "description": item_dict["description"],
            "approach": item_dict.get("approach"),
            "claimed_by": item_dict.get("claimed_by"),
            "claimed_at": item_dict.get("claimed_at"),
            "claimed_session": item_dict.get("claimed_session"),
            "claim_token": item_dict.get("claim_token"),
            "git_baseline": item_dict.get("git_baseline"),
            "work_units": displayed_units,
            "scope": scope,
            "preserves": preserves,
            "anti_patterns": anti_patterns,
            "verifications": verifications,
            "git_state": {
                "branch": git_state.branch,
                "head_sha": git_state.head_sha,
                "is_clean": git_state.is_clean,
            },
            "completeness": {
                "work_units_total": total_units,
                "work_units_shown": len(displayed_units),
            },
            "next_action": next_action,
        }

        if fields:
            selected_fields = [f.strip() for f in fields if f.strip()]
            always_present = {
                "id",
                "title",
                "state",
                "scope",
                "preserves",
                "anti_patterns",
                "next_action",
                "completeness",
            }
            keep = set(selected_fields) | always_present
            return {k: v for k, v in ctx.items() if k in keep}

        return ctx

    def progress(
        self,
        item_id: str,
        wid: str,
        evidence: str,
        *,
        claim_token: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        """Complete a work unit and atomically refresh the claim lease."""
        if not evidence or not evidence.strip():
            raise TodoError("evidence is required to mark a work unit done")

        with self.database.transaction():
            item = self.tracker._require_item(item_id)
            if item["claimed_by"] != self.tracker.actor:
                raise TodoError(f"{item_id!r} is not claimed by actor {self.tracker.actor!r}")
            tok = item["claim_token"] if "claim_token" in item.keys() else None
            if claim_token and tok and tok != claim_token:
                raise TodoError(f"claim token mismatch on {item_id!r}: E_CLAIM_STALE")

            unit = self.tracker._require_unit(item_id, wid)
            if unit["status"] == "done":
                raise TodoError(f"{item_id}:{wid} is already done")
            self.tracker._require_unit_needs_done(item_id, wid)

            now = utc_now()
            self.database.connection.execute(
                "UPDATE work_units SET status = 'done', evidence = ?, notes = COALESCE(?, notes) WHERE item_id = ? AND wid = ?",
                (evidence.strip(), notes, item_id, wid),
            )
            self.database.connection.execute(
                "UPDATE items SET claimed_at = ? WHERE id = ?",
                (now, item_id),
            )
            self.tracker._event("done_unit", item_id, {"wid": wid, "evidence": evidence.strip()})

        return self.context(item_id)

    def finish(
        self,
        item_id: str,
        *,
        claim_token: str | None = None,
        model_assert: bool = False,
        pr: int | None = None,
        verification_override_reason: str | None = None,
    ) -> dict[str, Any]:
        """Finish gate composing lint, scope check, verification validation, and audit verification."""
        lint_issues = self.tracker.lint(item_id)
        if lint_issues:
            raise TodoError(f"cannot finish {item_id!r}: lint findings detected: {'; '.join(lint_issues)}")

        item = self.tracker.get_item(item_id)
        tok = item.get("claim_token")
        if claim_token and tok and tok != claim_token:
            raise TodoError(f"claim token mismatch on {item_id!r}: E_CLAIM_STALE")

        base = item.get("git_baseline")
        changed_files = self.git_engine.changed_files(base=base)
        scope_violations = self.tracker.check_scope(item_id, changed_files)
        if scope_violations:
            raise TodoError(
                f"cannot finish {item_id!r}: scope violations detected: {'; '.join(scope_violations)}"
            )

        work_units = self.database.connection.execute(
            "SELECT wid, status FROM work_units WHERE item_id = ?", (item_id,)
        ).fetchall()
        unfinished = [u["wid"] for u in work_units if u["status"] != "done"]
        if unfinished:
            raise TodoError(f"cannot finish {item_id!r}: work units not done: {', '.join(unfinished)}")

        if model_assert:
            verifs = self.database.connection.execute(
                "SELECT seq, last_result FROM verifications WHERE item_id = ?", (item_id,)
            ).fetchall()
            failed = [v["seq"] for v in verifs if v["last_result"] != "pass"]
            if failed and verification_override_reason is None:
                raise TodoError(
                    f"cannot finish {item_id!r}: verifications not passed: {', '.join(map(str, failed))}; "
                    f"run `todo verify {item_id} --run` to execute verifications"
                )

            with self.database.transaction():
                self.database.verify_audit()
                self.tracker._transition(item, "done")
                self.database.connection.execute(
                    "UPDATE items SET completed_at = ?, completed_pr = ?, claimed_by = NULL, claimed_at = NULL, "
                    "claimed_session = NULL, claim_token = NULL, claimed_branch = NULL, claimed_worktree = NULL, "
                    "git_baseline = NULL WHERE id = ?",
                    (utc_now(), pr, item_id),
                )
                self.tracker._event(
                    "complete",
                    item_id,
                    {"pr": pr, "model_assert": True, "verification": {"result": "pass"}},
                )
        else:
            self.tracker.complete(item_id, pr=pr, verification_override_reason=verification_override_reason)

        return {"id": item_id, "state": "done", "status": "completed"}
