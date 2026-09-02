from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from .errors import (
    E_BASE_DIVERGED,
    E_BASE_UNREACHABLE,
    E_CLAIM_STALE,
    E_LINT_GATE,
    E_MULTIPLE_CLAIMS,
    E_NOTHING_READY,
    E_SCOPE_GATE,
    E_VERIFY_GATE,
    TodoError,
)
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
        if not self.is_git_repo():
            return GitState(
                root=self.repo_root,
                head_sha=None,
                branch=None,
                is_clean=False,
            )
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
        return os.fsdecode(proc.stdout)

    def is_git_repo(self) -> bool:
        return (self.repo_root / ".git").exists()

    def _run_bytes(self, args: list[str]) -> bytes:
        if not self.is_git_repo():
            raise TodoError(f"not a git repository: {self.repo_root}", code=E_SCOPE_GATE)
        proc = subprocess.run(
            ["git", "-C", str(self.repo_root), *args],
            capture_output=True,
            text=False,
            check=False,
        )
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", errors="replace")
            raise TodoError(f"git command failed ({' '.join(args)}): {err.strip()}", code=E_SCOPE_GATE)
        return proc.stdout

    def changed_files(self, base: str | None = None) -> list[str]:
        """Return root-relative changed, added, deleted, renamed, and untracked files."""
        if not self.is_git_repo():
            raise TodoError(f"not a git repository: {self.repo_root}", code=E_SCOPE_GATE)

        changed: set[str] = set()

        if base:
            if base.startswith("-"):
                raise TodoError(f"invalid git baseline {base!r}: E_BASE_UNREACHABLE", code=E_BASE_UNREACHABLE)

            # Check baseline reachability
            proc = subprocess.run(
                ["git", "-C", str(self.repo_root), "rev-parse", "--verify", f"{base}^{{commit}}"],
                capture_output=True,
                check=False,
            )
            if proc.returncode != 0:
                raise TodoError(f"baseline commit {base!r} unreachable: E_BASE_UNREACHABLE", code=E_BASE_UNREACHABLE)

            # Check ancestor relationship
            anc_proc = subprocess.run(
                ["git", "-C", str(self.repo_root), "merge-base", "--is-ancestor", base, "HEAD"],
                capture_output=True,
                check=False,
            )
            if anc_proc.returncode != 0:
                raise TodoError(
                    f"baseline commit {base!r} has diverged from HEAD: E_BASE_DIVERGED; "
                    "audited human rebaseline required",
                    code=E_BASE_DIVERGED,
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
            status_code = os.fsdecode(parts[i])
            i += 1
            if not status_code:
                continue
            if status_code.startswith(("R", "C")):
                if i < len(parts) and parts[i]:
                    out.add(os.fsdecode(parts[i]))
                    i += 1
                if i < len(parts) and parts[i]:
                    out.add(os.fsdecode(parts[i]))
                    i += 1
            elif i < len(parts) and parts[i]:
                out.add(os.fsdecode(parts[i]))
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
            status_tag = os.fsdecode(entry[:2])
            out.add(os.fsdecode(entry[3:]))
            if ("R" in status_tag or "C" in status_tag) and i < len(parts) and parts[i]:
                out.add(os.fsdecode(parts[i]))
                i += 1

    def workspace_fingerprint(self) -> str:
        """Hash HEAD plus tracked/index/worktree diffs and untracked file contents."""
        if not self.is_git_repo():
            raise TodoError(f"not a git repository: {self.repo_root}", code=E_SCOPE_GATE)
        digest = hashlib.sha256()
        head = self._run_bytes(["rev-parse", "--verify", "HEAD"]) if self.capture_state().head_sha else b"<unborn>"
        digest.update(b"HEAD\0" + head)
        if head != b"<unborn>":
            digest.update(b"WORKTREE\0" + self._run_bytes(["diff", "--binary", "HEAD", "--"]))
            digest.update(b"INDEX\0" + self._run_bytes(["diff", "--cached", "--binary", "HEAD", "--"]))
        untracked = self._run_bytes(["ls-files", "-z", "--others", "--exclude-standard", "--full-name"])
        for raw_path in sorted(path for path in untracked.split(b"\x00") if path):
            digest.update(b"UNTRACKED\0" + raw_path + b"\0")
            path = self.repo_root / os.fsdecode(raw_path)
            if path.is_symlink():
                digest.update(b"SYMLINK\0" + os.fsencode(os.readlink(path)))
            elif path.is_file():
                digest.update(b"FILE\0" + path.read_bytes())
            else:
                digest.update(b"OTHER\0")
        return digest.hexdigest()


class AgentWorkflow:
    """Streamlined, claim-coordinated workflow service for autonomous agents."""

    def __init__(
        self,
        tracker: TodoTracker,
        repo_root: Path | None = None,
        git_engine: GitScopeEngine | None = None,
    ):
        self.tracker = tracker
        self.database = tracker.database
        if git_engine is not None and repo_root is not None:
            raise TodoError("pass at most one of repo_root and git_engine", code=E_SCOPE_GATE)
        if git_engine is not None:
            self.git_engine = git_engine
        else:
            self.git_engine = GitScopeEngine(repo_root)

    def current_claim(self, principal: str | None = None) -> dict[str, Any] | None:
        p = principal or self.tracker.actor
        rows = self.database.connection.execute(
            """
            SELECT * FROM items
            WHERE claimed_by = ? AND state = 'active'
            ORDER BY CASE priority
                WHEN 'critical' THEN 1
                WHEN 'high' THEN 2
                WHEN 'medium-high' THEN 3
                WHEN 'medium' THEN 4
                WHEN 'low' THEN 5
                ELSE 6
            END, id
            LIMIT 2
            """,
            (p,),
        ).fetchall()
        if len(rows) > 1:
            raise TodoError(
                f"principal {p!r} holds multiple active claims; release all but one with `todo agent release <id> --claim-token <token>`",
                code=E_MULTIPLE_CLAIMS,
            )
        return dict(rows[0]) if rows else None

    def next(self, principal: str | None = None) -> dict[str, Any]:
        claim = self.current_claim(principal)
        if claim:
            item_id = claim["id"]
            context = self.context(item_id, fields=["work_units"])
            return {
                "status": "claimed",
                "item": {
                    "id": item_id,
                    "title": claim["title"],
                    "priority": claim["priority"],
                    "worktree": claim["worktree"],
                    "claimed_at": claim["claimed_at"],
                    "claim_token": claim.get("claim_token") if claim.get("claimed_by") == self.tracker.actor else None,
                    "git_baseline": claim.get("git_baseline"),
                },
                "next_action": context["next_action"],
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
                    "tool": "take",
                    "arguments": {"id": top["id"]},
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

    def _adopt_internal(self, item_id: str, session: str) -> None:
        item = dict(self.tracker._require_item(item_id))
        if item["state"] != "active" or item.get("claimed_by") != self.tracker.actor:
            raise TodoError(
                f"cannot adopt {item_id!r}: not an active claim held by actor {self.tracker.actor!r}; "
                f"use `todo agent take {item_id}` instead"
            )
        new_token = uuid4().hex
        now = utc_now()
        self.database.connection.execute(
            "UPDATE items SET claimed_session = ?, claim_token = ?, claimed_at = ? WHERE id = ? AND claimed_by = ?",
            (session.strip(), new_token, now, item_id, self.tracker.actor),
        )
        self.tracker._event(
            "adopt",
            item_id,
            {"session": session.strip(), "previous_session": item.get("claimed_session")},
        )

    def adopt(self, item_id: str, session: str) -> dict[str, Any]:
        """Adopt an active claim for the current principal with a new session id and rotated claim token."""
        if not session or not session.strip():
            raise TodoError("session is required for claim adoption")
        with self.database.transaction():
            self._adopt_internal(item_id, session)
        return self.context(item_id)

    def take(
        self,
        item_id: str | None = None,
        *,
        session: str | None = None,
        worktree: str | None = None,
        branch: str | None = None,
    ) -> dict[str, Any]:
        """Atomically adopt existing claim or take highest-priority/specified item."""
        git_state = self.git_engine.capture_state()
        eff_worktree = worktree or str(git_state.root)
        eff_branch = branch or git_state.branch
        git_baseline = git_state.head_sha

        target_id: str
        with self.database.transaction():
            existing = self.current_claim()
            if existing:
                if item_id is not None and existing["id"] != item_id:
                    raise TodoError(
                        f"actor {self.tracker.actor!r} already holds active claim on {existing['id']!r}; "
                        f"release it first with `todo agent release {existing['id']}` before taking {item_id!r}"
                    )
                if session and existing.get("claimed_session") != session:
                    self._adopt_internal(existing["id"], session)
                target_id = existing["id"]
            else:
                target_id = item_id if item_id is not None else ""
                if not target_id:
                    ready = self.tracker.ready_items()
                    if not ready:
                        raise TodoError("cannot take item: no ready items in queue", code=E_NOTHING_READY)
                    target_id = ready[0]["id"]

                self.tracker._claim_internal(
                    target_id,
                    session=session,
                    branch=eff_branch,
                    worktree=eff_worktree,
                    git_baseline=git_baseline,
                )

        return self.context(target_id)

    def context(
        self,
        item_id: str,
        *,
        fields: list[str] | None = None,
        unit_limit: int | None = None,
        section: str | None = None,
        cursor: int = 0,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Return bounded, recoverable context with mandatory planning guardrails."""
        allowed_sections = {
            "work_units",
            "scope",
            "preserves",
            "anti_patterns",
            "verifications",
            "item_dependencies",
            "open_deferrals",
            "prior_art",
        }
        if section is not None and section not in allowed_sections:
            raise TodoError(f"unknown context section {section!r}")
        if cursor < 0 or limit < 0 or limit > 100:
            raise TodoError("context cursor must be non-negative and limit must be between 0 and 100")
        if unit_limit is not None:
            if unit_limit < 0:
                raise TodoError(f"--unit-limit must be non-negative, got {unit_limit}")
            limit = min(unit_limit, 100)
            section = section or "work_units"

        row = self.database.connection.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise TodoError(f"item not found: {item_id!r}")
        item = dict(row)

        unit_rows = self.database.connection.execute(
            "SELECT wid, summary, status FROM work_units WHERE item_id = ? ORDER BY wid", (item_id,)
        ).fetchall()
        need_rows = self.database.connection.execute(
            "SELECT wid, needs_wid FROM work_needs WHERE item_id = ? ORDER BY wid, needs_wid", (item_id,)
        ).fetchall()
        needs_by_wid: dict[str, list[str]] = {}
        for need in need_rows:
            needs_by_wid.setdefault(need["wid"], []).append(need["needs_wid"])
        done = {row["wid"] for row in unit_rows if row["status"] == "done"}
        work_units = [
            {
                "id": row["wid"],
                "summary": row["summary"],
                "status": row["status"],
                "needs": sorted(needs_by_wid.get(row["wid"], [])),
                "ready": row["status"] != "done"
                and all(need in done for need in needs_by_wid.get(row["wid"], [])),
            }
            for row in unit_rows
        ]
        sections: dict[str, list[Any]] = {
            "work_units": work_units,
            "scope": [
                {"kind": row["kind"], "path_glob": row["path_glob"]}
                for row in self.database.connection.execute(
                    "SELECT kind, path_glob FROM scope_rules WHERE item_id = ? ORDER BY kind, path_glob", (item_id,)
                )
            ],
            "preserves": [
                row["behavior"]
                for row in self.database.connection.execute(
                    "SELECT behavior FROM preserves WHERE item_id = ? ORDER BY behavior", (item_id,)
                )
            ],
            "anti_patterns": [
                {"dont": row["dont"], "why": row["why"], "instead": row["instead"]}
                for row in self.database.connection.execute(
                    "SELECT dont, why, instead FROM anti_patterns WHERE item_id = ? ORDER BY dont", (item_id,)
                )
            ],
            "verifications": [
                {
                    "seq": row["seq"],
                    "description": row["description"],
                    "expected": row["expected"],
                    "last_run": row["last_run"],
                    "last_result": row["last_result"],
                }
                for row in self.database.connection.execute(
                    "SELECT seq, description, expected, last_run, last_result FROM verifications "
                    "WHERE item_id = ? ORDER BY seq",
                    (item_id,),
                )
            ],
            "item_dependencies": [
                {"id": row["needs_item"], "state": row["state"]}
                for row in self.database.connection.execute(
                    "SELECT d.needs_item, i.state FROM item_deps d JOIN items i ON i.id = d.needs_item "
                    "WHERE d.item_id = ? ORDER BY d.needs_item",
                    (item_id,),
                )
            ],
            "open_deferrals": [
                {"id": row["id"], "summary": row["summary"], "reason": row["reason"]}
                for row in self.database.connection.execute(
                    "SELECT id, summary, reason FROM deferrals WHERE from_item = ? AND resolution = 'open' ORDER BY id",
                    (item_id,),
                )
            ],
            "prior_art": [
                {"path": row["path"], "concept": row["concept"], "decision": row["decision"]}
                for row in self.database.connection.execute(
                    "SELECT path, concept, decision FROM prior_art WHERE item_id = ? ORDER BY path, concept", (item_id,)
                )
            ],
        }

        completeness: dict[str, Any] = {}
        displayed: dict[str, list[Any]] = {}
        for name, entries in sections.items():
            start = cursor if section == name else 0
            page = entries[start : start + limit]
            next_cursor = start + len(page) if start + len(page) < len(entries) else None
            displayed[name] = page
            completeness[name] = {
                "complete": next_cursor is None,
                "returned": len(page),
                "total": len(entries),
                "next_cursor": next_cursor,
            }
        completeness["work_units_total"] = len(work_units)
        completeness["work_units_shown"] = len(displayed["work_units"])

        ready_units = [unit for unit in work_units if unit["ready"]]
        unfinished = [unit for unit in work_units if unit["status"] != "done"]
        unmet_items = [dep["id"] for dep in sections["item_dependencies"] if dep["state"] != "done"]
        if item["state"] in {"done", "dropped"}:
            next_action = {"action": "terminal", "details": f"item is {item['state']}"}
        elif item.get("claimed_by") not in {None, self.tracker.actor}:
            next_action = {"action": "wait", "details": f"claimed by {item['claimed_by']}"}
        elif item.get("blocked_reason"):
            next_action = {
                "action": "human_action_required",
                "details": item["blocked_reason"],
                "tool": "unblock",
                "arguments": {"id": item_id},
                "command": f"todo unblock {item_id}",
            }
        elif sections["open_deferrals"]:
            next_action = {
                "action": "human_action_required",
                "details": "resolve every open deferral",
                "tool": "dismiss_deferral",
                "arguments": {"id": sections["open_deferrals"][0]["id"], "reason": "<reason>"},
                "commands": [f"todo dismiss {entry['id']} --reason '<reason>'" for entry in sections["open_deferrals"]],
            }
        elif unmet_items:
            next_action = {"action": "wait", "details": f"unmet item dependencies: {', '.join(unmet_items)}"}
        elif item.get("claimed_by") is None:
            next_action = {
                "action": "take",
                "item_id": item_id,
                "tool": "take",
                "arguments": {"id": item_id},
                "command": f"todo agent take {item_id}",
            }
        elif not item.get("claim_token"):
            next_action = {
                "action": "take",
                "item_id": item_id,
                "details": "adopt this legacy claim to create a generation token before mutation",
                "tool": "take",
                "arguments": {"id": item_id, "session": "<session-id>"},
                "command": f"todo agent take {item_id} --session <session-id>",
            }
        elif ready_units:
            unit = ready_units[0]
            next_action = {
                "action": "progress",
                "item_id": item_id,
                "wid": unit["id"],
                "summary": unit["summary"],
                "tool": "progress",
                "arguments": {
                    "id": item_id,
                    "wid": unit["id"],
                    "evidence": "<evidence>",
                    "claim_token": item.get("claim_token") or "<claim-token>",
                },
                "command": f"todo agent progress {item_id} {unit['id']} --evidence '<evidence>'",
            }
        elif unfinished:
            next_action = {
                "action": "human_action_required",
                "details": "unfinished work units have no executable dependency order; repair the plan",
                "tool": "show_item",
                "arguments": {"id": item_id},
                "command": f"todo show {item_id} --json",
            }
        else:
            next_action = {
                "action": "finish",
                "item_id": item_id,
                "tool": "finish",
                "arguments": {"id": item_id, "claim_token": item.get("claim_token") or "<claim-token>"},
                "command": f"todo agent finish {item_id} --claim-token <claim-token>",
            }

        git_state = self.git_engine.capture_state()
        ctx = {
            "id": item["id"],
            "title": item["title"],
            "priority": item["priority"],
            "state": item["state"],
            "worktree": item["worktree"],
            "description": item["description"],
            "approach": item.get("approach"),
            "blocked_reason": item.get("blocked_reason"),
            "claimed_by": item.get("claimed_by"),
            "claimed_at": item.get("claimed_at"),
            "claimed_session": item.get("claimed_session"),
            "claim_token": item.get("claim_token") if item.get("claimed_by") == self.tracker.actor else None,
            "git_baseline": item.get("git_baseline"),
            **displayed,
            "git_state": {"branch": git_state.branch, "head_sha": git_state.head_sha, "is_clean": git_state.is_clean},
            "completeness": completeness,
            "next_action": next_action,
        }
        if fields:
            always = {
                "id", "title", "state", "blocked_reason", "scope", "preserves", "anti_patterns",
                "item_dependencies", "open_deferrals", "next_action", "completeness",
            }
            keep = {field.strip() for field in fields if field.strip()} | always
            return {key: value for key, value in ctx.items() if key in keep}
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
            item = dict(self.tracker._require_item(item_id))
            if item["claimed_by"] != self.tracker.actor:
                raise TodoError(f"{item_id!r} is not claimed by actor {self.tracker.actor!r}")
            tok = item.get("claim_token")
            if tok:
                if not claim_token:
                    raise TodoError(f"claim token required on {item_id!r}: E_CLAIM_STALE", code=E_CLAIM_STALE)
                if tok != claim_token:
                    raise TodoError(f"claim token mismatch on {item_id!r}: E_CLAIM_STALE", code=E_CLAIM_STALE)

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
        run_verifications: bool = False,
        pr: int | None = None,
        verification_override_reason: str | None = None,
    ) -> dict[str, Any]:
        """Finish gate composing ownership, lint, scope check, verification validation, and audit verification."""
        item = self.tracker.get_item(item_id)
        if item.get("claimed_by") != self.tracker.actor:
            raise TodoError(f"{item_id!r} is not claimed by actor {self.tracker.actor!r}")

        tok = item.get("claim_token")
        if tok:
            if not claim_token:
                raise TodoError(f"claim token required on {item_id!r}: E_CLAIM_STALE", code=E_CLAIM_STALE)
            if tok != claim_token:
                raise TodoError(f"claim token mismatch on {item_id!r}: E_CLAIM_STALE", code=E_CLAIM_STALE)

        structural_commands: list[str] = []
        if item.get("blocked_reason"):
            structural_commands.append(f"todo unblock {item_id}")
        structural_commands.extend(
            f"todo dismiss {entry['id']} --reason '<reason>'"
            for entry in item.get("deferrals", [])
            if entry.get("resolution") == "open"
        )
        lint_issues = self.tracker.lint(item_id)
        if lint_issues:
            structural_commands.insert(0, f"todo lint {item_id}")
        if lint_issues and not structural_commands[1:]:
            # Lint-only failure retains the claim so the agent can repair and retry (ADR 0006 G7).
            details = "; ".join(lint_issues)
            commands = "; then ".join(structural_commands)
            raise TodoError(
                f"cannot finish {item_id!r}: {details}; claim retained; run `{commands}`",
                code=E_LINT_GATE,
            )
        if lint_issues or structural_commands:
            self.release(item_id, claim_token)
            details = "; ".join(lint_issues) if lint_issues else "structural blockers remain"
            commands = "; then ".join(structural_commands)
            raise TodoError(
                f"cannot finish {item_id!r}: {details}; streamlined claim released; run `{commands}`",
                code=E_LINT_GATE,
            )

        work_units = self.database.connection.execute(
            "SELECT wid, status FROM work_units WHERE item_id = ?", (item_id,)
        ).fetchall()
        unfinished = [u["wid"] for u in work_units if u["status"] != "done"]
        if unfinished:
            raise TodoError(f"cannot finish {item_id!r}: work units not done: {', '.join(unfinished)}")

        base = item.get("git_baseline")
        changed_files = self.git_engine.changed_files(base=base)
        scope_violations = self.tracker.check_scope(item_id, changed_files)
        if scope_violations:
            raise TodoError(
                f"cannot finish {item_id!r}: scope violations detected: {'; '.join(scope_violations)}",
                code=E_SCOPE_GATE,
            )

        if verification_override_reason is not None:
            raise TodoError("agent finish does not permit verification overrides; use the explicit human complete command")

        if run_verifications:
            stable_fingerprint = self.git_engine.workspace_fingerprint()
            verifs = self.database.connection.execute(
                "SELECT seq FROM verifications WHERE item_id = ? ORDER BY seq", (item_id,)
            ).fetchall()
            for verification in verifs:
                result, output = self.tracker.run_verification(
                    item_id, verification["seq"], cwd=self.git_engine.repo_root
                )
                if result != "pass":
                    raise TodoError(
                        f"cannot finish {item_id!r}: verification seq {verification['seq']} failed: "
                        f"{output.strip()[:200]}",
                        code=E_VERIFY_GATE,
                    )
                if self.git_engine.workspace_fingerprint() != stable_fingerprint:
                    raise TodoError(
                        f"cannot finish {item_id!r}: verification seq {verification['seq']} modified the Git workspace; "
                        "review the change and rerun the full ladder",
                        code=E_VERIFY_GATE,
                    )
            post_changed = self.git_engine.changed_files(base=base)
            post_scope = self.tracker.check_scope(item_id, post_changed)
            if post_scope:
                raise TodoError(
                    f"cannot finish {item_id!r}: post-verification scope violations: {'; '.join(post_scope)}",
                    code=E_SCOPE_GATE,
                )
            fingerprint = self.git_engine.workspace_fingerprint()
            self.tracker.attest_verifications(item_id, fingerprint)
        else:
            fingerprint = self.git_engine.workspace_fingerprint()

        self.tracker.complete(
            item_id,
            pr=pr,
            model_assert=model_assert,
            verified_workspace_fingerprint=fingerprint,
            expected_claimed_by=self.tracker.actor,
            expected_claim_token=claim_token,
            enforce_claim_generation=True,
        )
        return {"id": item_id, "state": "done", "status": "completed"}

    def release(self, item_id: str, claim_token: str | None) -> dict[str, Any]:
        self.tracker.release(item_id, claim_token=claim_token, require_claim_token=True)
        return {"id": item_id, "status": "released"}

    def rebaseline(self, item_id: str, reason: str, claim_token: str | None, new_baseline: str | None = None) -> dict[str, Any]:
        state = self.git_engine.capture_state()
        if not state.is_clean:
            raise TodoError("rebaseline requires a clean worktree")
        baseline = new_baseline or state.head_sha
        if baseline and baseline.startswith("-"):
            raise TodoError("rebaseline commit must not begin with '-'")
        if not baseline:
            raise TodoError("rebaseline requires a reachable commit")
        self.git_engine._run_bytes(["rev-parse", "--verify", f"{baseline}^{{commit}}"])
        self.tracker.rebaseline_scope(item_id, baseline, reason, claim_token=claim_token)
        return self.context(item_id)
