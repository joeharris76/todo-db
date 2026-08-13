"""The database-backed TODO lifecycle.

This module intentionally keeps policy in executable code.  The database layer
owns identity, migrations, and the audit chain; this service owns item state,
work-unit prerequisites, leases, and the legacy YAML bridge.
"""

from __future__ import annotations

import fnmatch
import getpass
import os
import re
import shlex
import socket
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable

from .database import TodoDatabase
from .errors import TodoError

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10
    try:
        import tomli as tomllib
    except ModuleNotFoundError:  # pragma: no cover - optional lint check degrades conservatively
        tomllib = None  # type: ignore[assignment]


PRIORITIES = ("critical", "high", "medium-high", "medium", "low")
STATES = ("planning", "active", "done", "dropped")
UNIT_STATUSES = ("pending", "in_progress", "done")
TRANSITIONS = {
    ("planning", "active"),
    ("active", "done"),
    ("planning", "dropped"),
    ("active", "dropped"),
}
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")
WID_RE = re.compile(r"^w[0-9]{1,3}$")
EVIDENCE_PIN_RE = re.compile(r"verified on|@ [0-9a-f]{7,}|\bPASS\b", re.IGNORECASE)
STATIC_SHELL_TEST_RE = re.compile(
    r"""(?:^|(?:&&|\|\||;)\s*)
    (?:test|\[)\s+
    -[zn]\s+
    (?:'[^']*'|"(?:\\.|[^"$`\\])*"|[A-Za-z0-9_./:@%+=,-]+)
    (?=\s*(?:\]|&&|\|\||;|$))""",
    re.VERBOSE,
)
DEFAULT_LEASE_TTL_HOURS = 24.0
CONFIG_KEYS = {
    "lint.require_w0_revalidation": ("on", "off"),
    "lint.require_scope_rules": ("on", "off"),
}
STATUS_MAP = {
    "not started": ("planning", None),
    "identified": ("planning", None),
    "in progress": ("active", None),
    "under review": ("active", None),
    "blocked": ("active", "imported from YAML status: Blocked"),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def default_actor() -> str:
    for variable in ("TODO_ACTOR", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID", "AGENT_SESSION_ID"):
        if os.environ.get(variable):
            return os.environ[variable]
    return f"{getpass.getuser()}@{socket.gethostname()}"


def _dict(row: Any) -> dict[str, Any]:
    return dict(row)


def _slug_valid(item_id: str) -> bool:
    return bool(SLUG_RE.fullmatch(item_id))


def _slugify(raw: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", raw.lower()).strip("-")
    return re.sub(r"-{2,}", "-", slug)


def _parse_anti_pattern(text: str) -> tuple[str, str, str]:
    cleaned = re.sub(r"(?i)^\s*do not\s+", "", text.strip())
    parts = re.split(r"\s+--\s+|\s+[-—]\s+", cleaned, maxsplit=2)
    dont = parts[0].strip()
    why = re.sub(r"(?i)^because\s+", "", parts[1].strip()) if len(parts) > 1 else "(unstated)"
    instead = parts[2].strip() if len(parts) > 2 else "(not specified)"
    return dont, why, instead


def _parse_prior_art(text: str) -> tuple[str, str, str]:
    decision = next(
        (value for value in ("supersede", "extend", "reuse") if re.search(rf"\b{value}\b", text, re.IGNORECASE)),
        "reuse",
    )
    first = text.split()[0].rstrip(":,;") if text.split() else text
    return first, text.strip(), decision


def _is_absolute_scope_glob(pattern: str) -> bool:
    return pattern.startswith("~") or PurePosixPath(pattern).is_absolute() or PureWindowsPath(pattern).is_absolute()


def _shell_command_sources(command: str) -> list[str]:
    sources = [command]
    try:
        tokens = shlex.split(command)
    except ValueError:
        return sources
    for index, token in enumerate(tokens[:-2]):
        if Path(token).name in {"bash", "sh", "zsh"} and tokens[index + 1] == "-c":
            sources.append(tokens[index + 2])
    return sources


def _constant_shell_test_sequences(verifications: Iterable[dict[str, Any]]) -> list[int]:
    sequences = []
    for verification in verifications:
        command = verification.get("command") or ""
        if any(STATIC_SHELL_TEST_RE.search(source) for source in _shell_command_sources(command)):
            sequences.append(verification["seq"])
    return sequences


def _coerce_verifications(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict) and "commands" in raw:
        return [{"description": str(command), "command": str(command)} for command in raw.get("commands") or []]
    if not isinstance(raw, list):
        return []
    result: list[dict[str, Any]] = []
    for entry in raw:
        if isinstance(entry, dict):
            result.append(
                {
                    "description": str(entry.get("description", "")) or "(no description)",
                    "command": entry.get("command"),
                    "expected": entry.get("expected_output"),
                }
            )
        else:
            result.append({"description": str(entry)})
    return result


def _lease_expired(claimed_at: str | None, ttl_hours: float) -> bool:
    if not claimed_at:
        return True
    try:
        when = datetime.fromisoformat(claimed_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    return when < datetime.now(timezone.utc) - timedelta(hours=ttl_hours)


def _git_location() -> tuple[str | None, str | None]:
    def run(*args: str) -> str | None:
        result = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
        return result.stdout.strip() or None if result.returncode == 0 else None

    return run("rev-parse", "--show-toplevel"), run("rev-parse", "--abbrev-ref", "HEAD")


def _would_cycle(edges: dict[str, list[str]], source: str, target: str) -> bool:
    if source == target:
        return True
    pending = [target]
    seen: set[str] = set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        if current == source:
            return True
        seen.add(current)
        pending.extend(edges.get(current, ()))
    return False


_REQUIREMENT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")
_SHELL_BOUNDARIES = frozenset({"&&", "||", ";", "|"})


def _requirement_name(value: str) -> str | None:
    match = _REQUIREMENT_NAME_RE.match(value.strip())
    return re.sub(r"[-_.]+", "-", match.group(0)).lower() if match else None


def _option_values(tokens: list[str], name: str) -> list[str]:
    values: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == name and index + 1 < len(tokens):
            values.append(tokens[index + 1])
            index += 2
            continue
        prefix = f"{name}="
        if token.startswith(prefix):
            values.append(token[len(prefix) :])
        index += 1
    return values


def _invokes_pytest(tokens: list[str]) -> bool:
    for index, token in enumerate(tokens):
        executable = Path(token.rstrip(";")).name
        if executable == "pytest":
            return True
        if token == "-m" and index + 1 < len(tokens) and tokens[index + 1].rstrip(";") == "pytest":
            return True
    return False


def _dependency_names(entries: Any) -> set[str]:
    if not isinstance(entries, list):
        return set()
    return {name for entry in entries if isinstance(entry, str) and (name := _requirement_name(entry)) is not None}


def _group_dependency_names(groups: dict[str, Any], selected: set[str]) -> set[str]:
    names: set[str] = set()
    pending = list(selected)
    seen: set[str] = set()
    while pending:
        group = pending.pop()
        if group in seen:
            continue
        seen.add(group)
        entries = groups.get(group, [])
        names.update(_dependency_names(entries))
        if isinstance(entries, list):
            pending.extend(
                entry["include-group"]
                for entry in entries
                if isinstance(entry, dict) and isinstance(entry.get("include-group"), str)
            )
    return names


def _uv_project_missing_pytest(command: str) -> str | None:
    """Return the selected project label when a pytest command cannot provide pytest."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return None
    for uv_index, token in enumerate(tokens):
        if Path(token).name != "uv" or uv_index + 1 >= len(tokens) or tokens[uv_index + 1] != "run":
            continue
        if "cd" in tokens[:uv_index]:
            continue
        end = next(
            (index for index in range(uv_index + 2, len(tokens)) if tokens[index] in _SHELL_BOUNDARIES),
            len(tokens),
        )
        run_tokens = tokens[uv_index + 2 : end]
        separator = run_tokens.index("--") if "--" in run_tokens else None
        uv_options = run_tokens[:separator] if separator is not None else run_tokens
        invoked_command = run_tokens[separator + 1 :] if separator is not None else run_tokens
        if not _invokes_pytest(invoked_command):
            continue
        if _option_values(uv_options, "--directory"):
            continue
        injected = _option_values(uv_options, "--with") + _option_values(uv_options, "--with-editable")
        if "pytest" in {_requirement_name(value) for value in injected}:
            continue
        if _option_values(uv_options, "--with-requirements"):
            continue

        project_values = _option_values(uv_options, "--project")
        project_label = project_values[-1] if project_values else "."
        project_path = Path(project_label).expanduser()
        if not project_path.is_absolute():
            project_path = Path.cwd() / project_path
        project_path = project_path.resolve()
        try:
            project_path.relative_to(Path.cwd().resolve())
        except ValueError:
            continue
        manifest = project_path if project_path.name == "pyproject.toml" else project_path / "pyproject.toml"
        if tomllib is None or not manifest.is_file():
            continue
        try:
            payload = tomllib.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue

        project = payload.get("project")
        if not isinstance(project, dict):
            continue
        names = _dependency_names(project.get("dependencies"))
        groups = payload.get("dependency-groups")
        groups = groups if isinstance(groups, dict) else {}
        selected_groups: set[str] = set()
        only_groups = set(_option_values(uv_options, "--only-group"))
        if not only_groups and "--no-default-groups" not in uv_options and "--no-dev" not in uv_options:
            tool = payload.get("tool")
            tool = tool if isinstance(tool, dict) else {}
            uv_config = tool.get("uv")
            uv_config = uv_config if isinstance(uv_config, dict) else {}
            defaults = uv_config.get("default-groups", ["dev"])
            if defaults == "all":
                selected_groups.update(groups)
            elif isinstance(defaults, list):
                selected_groups.update(value for value in defaults if isinstance(value, str))
        selected_groups.update(_option_values(uv_options, "--group"))
        selected_groups.update(only_groups)
        if "--all-groups" in uv_options:
            selected_groups.update(groups)
        selected_groups.difference_update(_option_values(uv_options, "--no-group"))
        names.update(_group_dependency_names(groups, selected_groups))

        if only_groups or "--no-project" in uv_options:
            names = set()
            names.update(_group_dependency_names(groups, selected_groups))

        extras = project.get("optional-dependencies")
        extras = extras if isinstance(extras, dict) else {}
        selected_extras = set(extras) if "--all-extras" in uv_options else set(_option_values(uv_options, "--extra"))
        selected_extras.difference_update(_option_values(uv_options, "--no-extra"))
        for extra in selected_extras:
            names.update(_dependency_names(extras.get(extra)))
        if "pytest" not in names:
            return project_label
    return None


class TodoTracker:
    """Transactional TODO operations over one :class:`TodoDatabase`."""

    def __init__(self, database: TodoDatabase, actor: str | None = None) -> None:
        self.database = database
        self.actor = actor or default_actor()

    @property
    def connection(self) -> Any:
        return self.database.connection

    def _event(self, action: str, item_id: str | None, detail: dict[str, Any] | None = None) -> None:
        event = dict(detail or {})
        if item_id is not None:
            event["item_id"] = item_id
        self.database.append_event(actor=self.actor, action=action, detail=event)

    def _require_item(self, item_id: str) -> Any:
        row = self.connection.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise TodoError(f"no such item: {item_id}")
        return row

    def _require_unit(self, item_id: str, wid: str) -> Any:
        row = self.connection.execute(
            "SELECT * FROM work_units WHERE item_id = ? AND wid = ?", (item_id, wid)
        ).fetchone()
        if row is None:
            raise TodoError(f"no such work unit: {item_id}:{wid}")
        return row

    def _require_deferral(self, deferral_id: int) -> Any:
        row = self.connection.execute("SELECT * FROM deferrals WHERE id = ?", (deferral_id,)).fetchone()
        if row is None:
            raise TodoError(f"no such deferral: {deferral_id}")
        return row

    def _item_dep_edges(self) -> dict[str, list[str]]:
        edges: dict[str, list[str]] = {}
        for row in self.connection.execute("SELECT item_id, needs_item FROM item_deps"):
            edges.setdefault(row["item_id"], []).append(row["needs_item"])
        return edges

    def _work_edges(self, item_id: str) -> dict[str, list[str]]:
        edges: dict[str, list[str]] = {}
        for row in self.connection.execute("SELECT wid, needs_wid FROM work_needs WHERE item_id = ?", (item_id,)):
            edges.setdefault(row["wid"], []).append(row["needs_wid"])
        return edges

    def _validate_item(self, item_id: str, title: str, worktree: str, priority: str, description: str) -> None:
        if not _slug_valid(item_id):
            raise TodoError(f"invalid item id {item_id!r}; use lowercase letters, digits, and hyphens")
        if not 5 <= len(title) <= 200:
            raise TodoError("title must be between 5 and 200 characters")
        if not worktree.strip():
            raise TodoError("worktree must not be empty")
        if priority not in PRIORITIES:
            raise TodoError(f"invalid priority {priority!r}; choose one of {', '.join(PRIORITIES)}")
        if len(description) < 10:
            raise TodoError("description must be at least 10 characters")

    def _insert_item(
        self,
        *,
        item_id: str,
        title: str,
        worktree: str,
        priority: str,
        description: str,
        category: str | None = None,
        approach: str | None = None,
        state: str = "planning",
        blocked_reason: str | None = None,
        created_at: str | None = None,
        completed_at: str | None = None,
        completed_pr: int | None = None,
        work: Iterable[dict[str, Any]] = (),
        deps: Iterable[str] = (),
        scope: Iterable[tuple[str, str]] = (),
        verifications: Iterable[dict[str, Any]] = (),
        preserves: Iterable[str] = (),
        anti_patterns: Iterable[tuple[str, str, str] | dict[str, str]] = (),
        prior_art: Iterable[tuple[str, str, str] | dict[str, str]] = (),
    ) -> None:
        self._validate_item(item_id, title, worktree, priority, description)
        if state not in STATES:
            raise TodoError(f"invalid state {state!r}")
        work_list = [dict(unit) for unit in work]
        dep_list = [str(dep) for dep in deps]
        self.connection.execute(
            "INSERT INTO items (id, title, worktree, priority, state, blocked_reason, category, description, approach, "
            "created_at, completed_at, completed_pr) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item_id,
                title,
                worktree,
                priority,
                state,
                blocked_reason,
                category,
                description,
                approach,
                created_at or utc_now(),
                completed_at,
                completed_pr,
            ),
        )
        seen_wids: set[str] = set()
        for unit in work_list:
            wid = str(unit.get("id") or unit.get("wid") or "")
            summary = str(unit.get("summary") or unit.get("description") or "")
            if not WID_RE.fullmatch(wid):
                raise TodoError(f"invalid work-unit id {wid!r} on {item_id!r}")
            if wid in seen_wids:
                raise TodoError(f"duplicate work-unit id {item_id}:{wid}")
            if not 5 <= len(summary) <= 200:
                raise TodoError(f"work-unit summary must be between 5 and 200 characters: {item_id}:{wid}")
            seen_wids.add(wid)
            status = str(unit.get("status", "done" if state == "done" else "pending"))
            if status not in UNIT_STATUSES:
                raise TodoError(f"invalid work-unit status {status!r} on {item_id}:{wid}")
            self.connection.execute(
                "INSERT INTO work_units (item_id, wid, summary, status, evidence, notes, started_at, "
                "started_worktree, started_branch) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item_id,
                    wid,
                    summary,
                    status,
                    unit.get("evidence"),
                    unit.get("notes"),
                    unit.get("started_at"),
                    unit.get("started_worktree"),
                    unit.get("started_branch"),
                ),
            )
        for unit in work_list:
            wid = str(unit.get("id") or unit.get("wid"))
            for needs_wid in unit.get("needs", ()) or ():
                needs_wid = str(needs_wid)
                if _would_cycle(self._work_edges(item_id), wid, needs_wid):
                    raise TodoError(f"work-unit dependency cycle: {item_id}:{wid} -> {needs_wid}")
                try:
                    self.connection.execute(
                        "INSERT INTO work_needs (item_id, wid, needs_wid) VALUES (?, ?, ?)",
                        (item_id, wid, needs_wid),
                    )
                except Exception as exc:
                    raise TodoError(f"work need {item_id}:{wid} -> {needs_wid} references a missing unit") from exc
        for dep in dep_list:
            if dep == item_id or self.connection.execute("SELECT 1 FROM items WHERE id = ?", (dep,)).fetchone() is None:
                raise TodoError(f"dependency {item_id!r} -> {dep!r} references a missing item")
            if _would_cycle(self._item_dep_edges(), item_id, dep):
                raise TodoError(f"item dependency cycle: {item_id} -> {dep}")
            self.connection.execute("INSERT INTO item_deps (item_id, needs_item) VALUES (?, ?)", (item_id, dep))
        for kind, path_glob in scope:
            if kind not in ("only_modify", "do_not_modify"):
                raise TodoError(f"invalid scope rule kind: {kind}")
            self.connection.execute(
                "INSERT OR IGNORE INTO scope_rules (item_id, kind, path_glob) VALUES (?, ?, ?)",
                (item_id, kind, path_glob),
            )
        for seq, verification in enumerate(verifications, start=1):
            entry = dict(verification)
            description_text = str(entry.get("description") or "(no description)")
            self.connection.execute(
                "INSERT INTO verifications (item_id, seq, description, command, expected) VALUES (?, ?, ?, ?, ?)",
                (item_id, seq, description_text, entry.get("command"), entry.get("expected")),
            )
        for behavior in preserves:
            self.connection.execute(
                "INSERT OR IGNORE INTO preserves (item_id, behavior) VALUES (?, ?)", (item_id, behavior)
            )
        for entry in anti_patterns:
            values = (entry["dont"], entry["why"], entry["instead"]) if isinstance(entry, dict) else tuple(entry)
            self.connection.execute(
                "INSERT OR IGNORE INTO anti_patterns (item_id, dont, why, instead) VALUES (?, ?, ?, ?)",
                (item_id, *values),
            )
        for entry in prior_art:
            values = (entry["path"], entry["concept"], entry["decision"]) if isinstance(entry, dict) else tuple(entry)
            if values[2] not in ("reuse", "extend", "supersede"):
                raise TodoError(f"invalid prior_art decision {values[2]!r}")
            self.connection.execute(
                "INSERT OR IGNORE INTO prior_art (item_id, path, concept, decision) VALUES (?, ?, ?, ?)",
                (item_id, *values),
            )

    def create_item(self, **kwargs: Any) -> str:
        if "id" in kwargs and "item_id" not in kwargs:
            kwargs["item_id"] = kwargs.pop("id")
        scope_value = kwargs.get("scope")
        if isinstance(scope_value, dict):
            kwargs["scope"] = [
                (kind, str(path_glob))
                for kind in ("only_modify", "do_not_modify")
                for path_glob in scope_value.get(kind, ())
            ]
        item_id = str(kwargs.get("item_id") or kwargs.get("id") or "")
        kwargs["item_id"] = item_id
        deferrals = list(kwargs.pop("deferrals", ()))
        with self.database.transaction():
            self._insert_item(**kwargs)
            self._event("create", item_id, {"title": kwargs["title"], "state": kwargs.get("state", "planning")})
            for entry in deferrals:
                cursor = self.connection.execute(
                    "INSERT INTO deferrals (from_item, summary, reason, created_at) VALUES (?, ?, ?, ?)",
                    (
                        item_id,
                        str(entry.get("summary") or "(no summary)"),
                        str(entry.get("reason") or "(none recorded)"),
                        utc_now(),
                    ),
                )
                self._event(
                    "defer",
                    item_id,
                    {"deferral_id": int(cursor.lastrowid), "summary": str(entry.get("summary") or "(no summary)")},
                )
        return item_id

    def update_item(
        self,
        item_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
        priority: str | None = None,
        worktree: str | None = None,
        add_work: Iterable[dict[str, Any]] = (),
        edit_work: dict[str, str] | None = None,
        add_verify: Iterable[dict[str, str]] = (),
        drop_verify: Iterable[int] = (),
        add_scope: Iterable[tuple[str, str]] = (),
        drop_scope: Iterable[tuple[str, str]] = (),
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Amend an item without touching its lifecycle; one chained `update` event carries every diff."""

        fields = {"title": title, "description": description, "priority": priority, "worktree": worktree}
        adds = [dict(unit) for unit in add_work]
        edits = dict(edit_work or {})
        verify_adds = [dict(entry) for entry in add_verify]
        verify_drops = [int(seq) for seq in drop_verify]
        scope_adds = self._normalize_scope_updates(add_scope)
        scope_drops = self._normalize_scope_updates(drop_scope)
        reason = reason.strip() if reason and reason.strip() else None
        if all(value is None for value in fields.values()) and not (
            adds or edits or verify_adds or verify_drops or scope_adds or scope_drops
        ):
            raise TodoError("update requires at least one change flag")
        if verify_drops and reason is None:
            raise TodoError("--drop-verify removes recorded verification steps; --reason is required")
        if (scope_adds or scope_drops) and reason is None:
            raise TodoError("scope changes require --reason because they amend the item's write boundary")
        conflicting_scope = set(scope_adds) & set(scope_drops)
        if conflicting_scope:
            kind, path_glob = sorted(conflicting_scope)[0]
            raise TodoError(f"cannot add and drop the same scope rule: {kind}:{path_glob}")
        with self.database.transaction():
            item = self._require_item(item_id)
            if item["state"] in ("done", "dropped") and reason is None:
                raise TodoError(f"{item_id!r} is {item['state']}; editing a terminal item requires --reason")
            changes: dict[str, dict[str, Any]] = {}
            for field, value in fields.items():
                if value is None:
                    continue
                if value == item[field]:
                    raise TodoError(f"--{field} equals the current value on {item_id!r}; nothing to update")
                changes[field] = {"from": item[field], "to": value}
            merged = {field: changes[field]["to"] if field in changes else item[field] for field in fields}
            self._validate_item(item_id, merged["title"], merged["worktree"], merged["priority"], merged["description"])
            if changes:
                assignments = ", ".join(f"{field} = ?" for field in changes)
                values = [change["to"] for change in changes.values()]
                self.connection.execute(f"UPDATE items SET {assignments} WHERE id = ?", (*values, item_id))
            work_edited: dict[str, dict[str, str]] = {}
            for wid, summary in edits.items():
                unit = self._require_unit(item_id, wid)
                if unit["status"] != "pending":
                    raise TodoError(
                        f"cannot edit {item_id}:{wid}: unit is {unit['status']}; only pending units are"
                        " editable (a done unit's evidence attaches to its summary)"
                    )
                if summary == unit["summary"]:
                    raise TodoError(f"--edit-work {wid} equals the current summary; nothing to update")
                if not 5 <= len(summary) <= 200:
                    raise TodoError(f"work-unit summary must be between 5 and 200 characters: {item_id}:{wid}")
                self.connection.execute(
                    "UPDATE work_units SET summary = ? WHERE item_id = ? AND wid = ?", (summary, item_id, wid)
                )
                work_edited[wid] = {"from": unit["summary"], "to": summary}
            wid_rows = self.connection.execute("SELECT wid FROM work_units WHERE item_id = ?", (item_id,))
            wids = {row["wid"] for row in wid_rows}
            work_added: list[dict[str, Any]] = []
            for unit in adds:
                wid = str(unit.get("id") or unit.get("wid") or "")
                summary = str(unit.get("summary") or "")
                if not WID_RE.fullmatch(wid):
                    raise TodoError(f"invalid work-unit id {wid!r} on {item_id!r}")
                if wid in wids:
                    raise TodoError(f"duplicate work-unit id {item_id}:{wid}")
                if not 5 <= len(summary) <= 200:
                    raise TodoError(f"work-unit summary must be between 5 and 200 characters: {item_id}:{wid}")
                wids.add(wid)
                self.connection.execute(
                    "INSERT INTO work_units (item_id, wid, summary, status) VALUES (?, ?, ?, 'pending')",
                    (item_id, wid, summary),
                )
                work_added.append({"id": wid, "summary": summary})
            for unit, added in zip(adds, work_added):
                needs = [str(needs_wid) for needs_wid in unit.get("needs", ()) or ()]
                for needs_wid in needs:
                    if needs_wid not in wids:
                        raise TodoError(f"work need {item_id}:{added['id']} -> {needs_wid} references a missing unit")
                    if _would_cycle(self._work_edges(item_id), added["id"], needs_wid):
                        raise TodoError(f"work-unit dependency cycle: {item_id}:{added['id']} -> {needs_wid}")
                    self.connection.execute(
                        "INSERT INTO work_needs (item_id, wid, needs_wid) VALUES (?, ?, ?)",
                        (item_id, added["id"], needs_wid),
                    )
                if needs:
                    added["needs"] = needs
            next_seq = self.connection.execute(
                "SELECT COALESCE(max(seq), 0) AS seq FROM verifications WHERE item_id = ?", (item_id,)
            ).fetchone()["seq"]
            verify_added: list[dict[str, Any]] = []
            for entry in verify_adds:
                next_seq += 1
                name = str(entry.get("description") or "(no description)")
                self.connection.execute(
                    "INSERT INTO verifications (item_id, seq, description, command, expected) VALUES (?, ?, ?, ?, ?)",
                    (item_id, next_seq, name, entry.get("command"), entry.get("expected")),
                )
                added_verify: dict[str, Any] = {"seq": next_seq, "name": name, "command": entry.get("command")}
                if entry.get("expected") is not None:
                    added_verify["expected"] = entry["expected"]
                verify_added.append(added_verify)
            verify_dropped: list[dict[str, Any]] = []
            for seq in verify_drops:
                row = self.connection.execute(
                    "SELECT description, command FROM verifications WHERE item_id = ? AND seq = ?", (item_id, seq)
                ).fetchone()
                if row is None:
                    raise TodoError(f"no verification seq={seq} on {item_id!r}")
                self.connection.execute("DELETE FROM verifications WHERE item_id = ? AND seq = ?", (item_id, seq))
                verify_dropped.append({"seq": seq, "name": row["description"], "command": row["command"]})
            existing_scope = {
                (row["kind"], row["path_glob"])
                for row in self.connection.execute(
                    "SELECT kind, path_glob FROM scope_rules WHERE item_id = ?", (item_id,)
                )
            }
            for rule in scope_adds:
                if rule in existing_scope:
                    raise TodoError(f"scope rule already exists on {item_id!r}: {rule[0]}:{rule[1]}")
            for rule in scope_drops:
                if rule not in existing_scope:
                    raise TodoError(f"no scope rule on {item_id!r}: {rule[0]}:{rule[1]}")
            for kind, path_glob in scope_drops:
                self.connection.execute(
                    "DELETE FROM scope_rules WHERE item_id = ? AND kind = ? AND path_glob = ?",
                    (item_id, kind, path_glob),
                )
            for kind, path_glob in scope_adds:
                self.connection.execute(
                    "INSERT INTO scope_rules (item_id, kind, path_glob) VALUES (?, ?, ?)",
                    (item_id, kind, path_glob),
                )
            detail: dict[str, Any] = {}
            if changes:
                detail["changes"] = changes
            if work_added:
                detail["work_added"] = work_added
            if work_edited:
                detail["work_edited"] = work_edited
            if verify_added:
                detail["verify_added"] = verify_added
            if verify_dropped:
                detail["verify_dropped"] = verify_dropped
            if scope_adds:
                detail["scope_added"] = [{"kind": kind, "path_glob": path_glob} for kind, path_glob in scope_adds]
            if scope_drops:
                detail["scope_dropped"] = [{"kind": kind, "path_glob": path_glob} for kind, path_glob in scope_drops]
            if reason is not None:
                detail["reason"] = reason
            self._event("update", item_id, detail)
        return detail

    @staticmethod
    def _normalize_scope_updates(rules: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
        normalized: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for raw_kind, raw_path_glob in rules:
            kind = str(raw_kind)
            path_glob = str(raw_path_glob).strip()
            if kind not in ("only_modify", "do_not_modify"):
                raise TodoError(f"invalid scope rule kind: {kind}")
            if not path_glob:
                raise TodoError("scope path glob must not be empty")
            rule = (kind, path_glob)
            if rule in seen:
                raise TodoError(f"scope rule named more than once: {kind}:{path_glob}")
            seen.add(rule)
            normalized.append(rule)
        return normalized

    def add_work_need(self, item_id: str, wid: str, needs_wid: str) -> None:
        with self.database.transaction():
            self._require_item(item_id)
            self._require_unit(item_id, wid)
            self._require_unit(item_id, needs_wid)
            if _would_cycle(self._work_edges(item_id), wid, needs_wid):
                raise TodoError(f"work-unit dependency cycle: {item_id}:{wid} -> {needs_wid}")
            self.connection.execute(
                "INSERT INTO work_needs (item_id, wid, needs_wid) VALUES (?, ?, ?)", (item_id, wid, needs_wid)
            )
            self._event("work-need", item_id, {"wid": wid, "needs_wid": needs_wid})

    def add_item_dep(self, item_id: str, needs_item: str) -> None:
        with self.database.transaction():
            self._require_item(item_id)
            self._require_item(needs_item)
            if _would_cycle(self._item_dep_edges(), item_id, needs_item):
                raise TodoError(f"item dependency cycle: {item_id} -> {needs_item}")
            self.connection.execute("INSERT INTO item_deps (item_id, needs_item) VALUES (?, ?)", (item_id, needs_item))
            self._event("dependency", item_id, {"needs_item": needs_item})

    def get_item(self, item_id: str) -> dict[str, Any]:
        item = _dict(self._require_item(item_id))
        item["work"] = []
        for row in self.connection.execute("SELECT * FROM work_units WHERE item_id = ? ORDER BY wid", (item_id,)):
            unit = _dict(row)
            unit["needs"] = [
                r["needs_wid"]
                for r in self.connection.execute(
                    "SELECT needs_wid FROM work_needs WHERE item_id = ? AND wid = ? ORDER BY needs_wid",
                    (item_id, row["wid"]),
                )
            ]
            item["work"].append(unit)
        item["deps"] = [
            r["needs_item"]
            for r in self.connection.execute(
                "SELECT needs_item FROM item_deps WHERE item_id = ? ORDER BY needs_item", (item_id,)
            )
        ]
        item["scope"] = [
            _dict(r)
            for r in self.connection.execute(
                "SELECT kind, path_glob FROM scope_rules WHERE item_id = ? ORDER BY kind, path_glob", (item_id,)
            )
        ]
        item["verifications"] = [
            _dict(r)
            for r in self.connection.execute("SELECT * FROM verifications WHERE item_id = ? ORDER BY seq", (item_id,))
        ]
        item["preserves"] = [
            r["behavior"]
            for r in self.connection.execute(
                "SELECT behavior FROM preserves WHERE item_id = ? ORDER BY behavior", (item_id,)
            )
        ]
        item["anti_patterns"] = [
            _dict(r)
            for r in self.connection.execute(
                "SELECT dont, why, instead FROM anti_patterns WHERE item_id = ? ORDER BY dont", (item_id,)
            )
        ]
        item["prior_art"] = [
            _dict(r)
            for r in self.connection.execute(
                "SELECT path, concept, decision FROM prior_art WHERE item_id = ? ORDER BY path, concept", (item_id,)
            )
        ]
        item["deferrals"] = [
            _dict(r)
            for r in self.connection.execute("SELECT * FROM deferrals WHERE from_item = ? ORDER BY id", (item_id,))
        ]
        return item

    def work_order(self, item_id: str) -> dict[str, Any]:
        item = self.get_item(item_id)
        ready, blocked = [], []
        for unit in item["work"]:
            if unit["status"] == "done":
                continue
            unmet = [
                row["needs_wid"]
                for row in self.connection.execute(
                    "SELECT n.needs_wid FROM work_needs n JOIN work_units u ON u.item_id = n.item_id AND u.wid = n.needs_wid "
                    "WHERE n.item_id = ? AND n.wid = ? AND u.status != 'done'",
                    (item_id, unit["wid"]),
                )
            ]
            if unmet:
                blocked.append({**unit, "unmet": unmet})
            else:
                ready.append(unit)
        item["ready_units"] = ready
        item["blocked_units"] = blocked
        return item

    def claim(self, item_id: str, ttl_hours: float = DEFAULT_LEASE_TTL_HOURS) -> dict[str, Any]:
        with self.database.transaction():
            item = self._require_item(item_id)
            if item["state"] not in ("planning", "active"):
                raise TodoError(f"{item_id!r} is {item['state']}; cannot claim")
            holder = item["claimed_by"]
            if holder and holder != self.actor and not _lease_expired(item["claimed_at"], ttl_hours):
                raise TodoError(f"{item_id!r} is claimed by {holder!r}")
            unmet = [
                row["needs_item"]
                for row in self.connection.execute(
                    "SELECT d.needs_item FROM item_deps d JOIN items n ON n.id = d.needs_item "
                    "WHERE d.item_id = ? AND n.state != 'done'",
                    (item_id,),
                )
            ]
            if unmet:
                raise TodoError(f"{item_id!r} has unmet dependencies: {', '.join(unmet)}")
            self.connection.execute(
                "UPDATE items SET claimed_by = ?, claimed_at = ?, state = CASE WHEN state = 'planning' THEN 'active' ELSE state END "
                "WHERE id = ? AND (claimed_by IS NULL OR claimed_by = ? OR claimed_at < ?)",
                (
                    self.actor,
                    utc_now(),
                    item_id,
                    self.actor,
                    (datetime.now(timezone.utc) - timedelta(hours=ttl_hours)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                ),
            )
            if self.connection.execute("SELECT changes() AS n").fetchone()["n"] != 1:
                raise TodoError(f"{item_id!r} was claimed concurrently")
            self._event("claim", item_id, {"previous_holder": holder})
        return self.work_order(item_id)

    def release(self, item_id: str) -> None:
        with self.database.transaction():
            item = self._require_item(item_id)
            if item["claimed_by"] is None:
                return
            if item["claimed_by"] != self.actor:
                raise TodoError(f"{item_id!r} is claimed by {item['claimed_by']!r}; only the holder can release")
            self.connection.execute(
                "UPDATE items SET claimed_by = NULL, claimed_at = NULL WHERE id = ? AND claimed_by = ?",
                (item_id, self.actor),
            )
            if self.connection.execute("SELECT changes() AS n").fetchone()["n"] != 1:
                raise TodoError(f"{item_id!r} was released or claimed concurrently")
            self._event("release", item_id, {"holder": item["claimed_by"]})

    def _require_unit_needs_done(self, item_id: str, wid: str) -> None:
        unmet = [
            row["needs_wid"]
            for row in self.connection.execute(
                "SELECT n.needs_wid FROM work_needs n JOIN work_units u ON u.item_id = n.item_id AND u.wid = n.needs_wid "
                "WHERE n.item_id = ? AND n.wid = ? AND u.status != 'done'",
                (item_id, wid),
            )
        ]
        if unmet:
            raise TodoError(f"{item_id}:{wid} needs unfinished units: {', '.join(unmet)}")

    def start_unit(self, item_id: str, wid: str) -> None:
        with self.database.transaction():
            self._require_item(item_id)
            unit = self._require_unit(item_id, wid)
            if unit["status"] == "done":
                raise TodoError(f"{item_id}:{wid} is already done")
            self._require_unit_needs_done(item_id, wid)
            worktree, branch = _git_location()
            self.connection.execute(
                "UPDATE work_units SET status = 'in_progress', started_at = ?, started_worktree = ?, started_branch = ? "
                "WHERE item_id = ? AND wid = ?",
                (utc_now(), worktree, branch, item_id, wid),
            )
            self._event("start", item_id, {"wid": wid, "worktree": worktree, "branch": branch})

    def done_unit(self, item_id: str, wid: str, evidence: str) -> None:
        if not evidence or not evidence.strip():
            raise TodoError("evidence is required to mark a work unit done")
        with self.database.transaction():
            self._require_item(item_id)
            self._require_unit(item_id, wid)
            self._require_unit_needs_done(item_id, wid)
            worktree, branch = _git_location()
            self.connection.execute(
                "UPDATE work_units SET status = 'done', evidence = ?, started_at = COALESCE(started_at, ?), "
                "started_worktree = COALESCE(started_worktree, ?), started_branch = COALESCE(started_branch, ?) "
                "WHERE item_id = ? AND wid = ?",
                (evidence, utc_now(), worktree, branch, item_id, wid),
            )
            self._event("done", item_id, {"wid": wid, "evidence": evidence})

    def _transition(self, item: Any, target: str) -> None:
        if (item["state"], target) not in TRANSITIONS:
            raise TodoError(f"illegal transition {item['state']!r} -> {target!r} for {item['id']!r}")
        self.connection.execute("UPDATE items SET state = ? WHERE id = ?", (target, item["id"]))

    def defer(self, item_id: str, summary: str, reason: str) -> int:
        if not summary.strip() or not reason.strip():
            raise TodoError("deferral summary and reason are required")
        with self.database.transaction():
            item = self._require_item(item_id)
            if item["state"] in ("done", "dropped"):
                raise TodoError(f"{item_id!r} is terminal; create a planning item instead")
            cursor = self.connection.execute(
                "INSERT INTO deferrals (from_item, summary, reason, created_at) VALUES (?, ?, ?, ?)",
                (item_id, summary, reason, utc_now()),
            )
            deferral_id = int(cursor.lastrowid)
            self._event("defer", item_id, {"deferral_id": deferral_id, "summary": summary})
            return deferral_id

    def promote_deferral(self, deferral_id: int, new_item_id: str, **overrides: Any) -> None:
        with self.database.transaction():
            row = self._require_deferral(deferral_id)
            if row["resolution"] != "open":
                raise TodoError(f"deferral {deferral_id} is already {row['resolution']}")
            parent = self._require_item(row["from_item"])
            title = overrides.get("title") or row["summary"][:200]
            description = overrides.get("description") or f"Promoted from deferral #{deferral_id}: {row['reason']}"
            self._insert_item(
                item_id=new_item_id,
                title=title,
                worktree=overrides.get("worktree") or parent["worktree"],
                priority=overrides.get("priority") or "medium",
                description=description,
            )
            self._event("create", new_item_id, {"title": title, "state": "planning", "promoted_from": deferral_id})
            self.connection.execute(
                "UPDATE deferrals SET resolution = 'promoted', resolved_item = ? WHERE id = ? AND resolution = 'open'",
                (new_item_id, deferral_id),
            )
            self._event("promote", row["from_item"], {"deferral_id": deferral_id, "new_item": new_item_id})

    def dismiss_deferral(self, deferral_id: int, reason: str) -> None:
        if not reason.strip():
            raise TodoError("a dismissal reason is required")
        with self.database.transaction():
            row = self._require_deferral(deferral_id)
            if row["resolution"] != "open":
                raise TodoError(f"deferral {deferral_id} is already {row['resolution']}")
            self.connection.execute(
                "UPDATE deferrals SET resolution = 'dismissed', resolved_reason = ? WHERE id = ?", (reason, deferral_id)
            )
            self._event("dismiss", row["from_item"], {"deferral_id": deferral_id, "reason": reason})

    def _completion_verifications(self, item_id: str) -> list[Any]:
        return list(
            self.connection.execute(
                "SELECT seq, command, expected, last_result FROM verifications WHERE item_id = ? ORDER BY seq",
                (item_id,),
            )
        )

    def _validate_completion_state(self, item_id: str) -> Any:
        item = self._require_item(item_id)
        if item["state"] != "active":
            raise TodoError(f"illegal transition {item['state']!r} -> 'done' for {item_id!r}")
        undone = [
            r["wid"]
            for r in self.connection.execute(
                "SELECT wid FROM work_units WHERE item_id = ? AND status != 'done'", (item_id,)
            )
        ]
        if undone:
            raise TodoError(f"cannot complete {item_id!r}: work units not done: {', '.join(undone)}")
        open_deferrals = [
            r["id"]
            for r in self.connection.execute(
                "SELECT id FROM deferrals WHERE from_item = ? AND resolution = 'open'", (item_id,)
            )
        ]
        if open_deferrals:
            raise TodoError(f"cannot complete {item_id!r}: unresolved deferrals: {', '.join(map(str, open_deferrals))}")
        if item["blocked_reason"]:
            raise TodoError(f"cannot complete {item_id!r} while blocked: {item['blocked_reason']}")
        return item

    def complete(
        self,
        item_id: str,
        pr: int | None = None,
        *,
        verification_override_reason: str | None = None,
    ) -> None:
        override_reason = verification_override_reason.strip() if verification_override_reason is not None else None
        if verification_override_reason is not None and not override_reason:
            raise TodoError("verification override reason must not be empty")

        with self.database.transaction():
            self._validate_completion_state(item_id)
            initial_verifications = self._completion_verifications(item_id)
        verification_definition = [(row["seq"], row["command"], row["expected"]) for row in initial_verifications]

        if override_reason is None:
            for seq, _command, _expected in verification_definition:
                result, _output = self.run_verification(item_id, seq)
                if result != "pass":
                    raise TodoError(
                        f"cannot complete {item_id!r}: verification seq={seq} failed; "
                        f"inspect it with `todo-db verify {item_id}`"
                    )
        elif not verification_definition:
            raise TodoError(f"cannot override verification for {item_id!r}: the item has no verification steps")

        with self.database.transaction():
            item = self._validate_completion_state(item_id)
            current_verifications = self._completion_verifications(item_id)
            current_definition = [(row["seq"], row["command"], row["expected"]) for row in current_verifications]
            if current_definition != verification_definition:
                raise TodoError(
                    f"cannot complete {item_id!r}: verification ladder changed while the completion gate ran; retry"
                )
            if override_reason is None:
                failed = [row["seq"] for row in current_verifications if row["last_result"] != "pass"]
                if failed:
                    raise TodoError(
                        f"cannot complete {item_id!r}: verification results are not passing: "
                        f"{', '.join(map(str, failed))}"
                    )
            self._transition(item, "done")
            self.connection.execute(
                "UPDATE items SET completed_at = ?, completed_pr = ?, claimed_by = NULL, claimed_at = NULL WHERE id = ?",
                (utc_now(), pr, item_id),
            )
            verification_detail: dict[str, Any]
            if override_reason is None:
                verification_detail = {
                    "verification": {"result": "pass", "sequences": [row["seq"] for row in current_verifications]}
                }
            else:
                verification_detail = {
                    "verification_override": {
                        "reason": override_reason,
                        "sequences": [row["seq"] for row in current_verifications],
                    }
                }
            self._event("complete", item_id, {"pr": pr, **verification_detail})

    def drop(self, item_id: str, reason: str) -> None:
        if not reason.strip():
            raise TodoError("a drop reason is required")
        with self.database.transaction():
            item = self._require_item(item_id)
            open_deferrals = self.connection.execute(
                "SELECT count(*) AS n FROM deferrals WHERE from_item = ? AND resolution = 'open'", (item_id,)
            ).fetchone()["n"]
            if open_deferrals:
                raise TodoError(f"cannot drop {item_id!r}: unresolved deferrals")
            self._transition(item, "dropped")
            self.connection.execute("UPDATE items SET claimed_by = NULL, claimed_at = NULL WHERE id = ?", (item_id,))
            self._event("drop", item_id, {"reason": reason})

    def block(self, item_id: str, reason: str) -> None:
        if not reason.strip():
            raise TodoError("a block reason is required")
        with self.database.transaction():
            item = self._require_item(item_id)
            if item["state"] in ("done", "dropped"):
                raise TodoError(f"{item_id!r} is terminal; cannot block")
            self.connection.execute("UPDATE items SET blocked_reason = ? WHERE id = ?", (reason, item_id))
            self._event("block", item_id, {"reason": reason})

    def unblock(self, item_id: str) -> None:
        with self.database.transaction():
            self._require_item(item_id)
            self.connection.execute("UPDATE items SET blocked_reason = NULL WHERE id = ?", (item_id,))
            self._event("unblock", item_id)

    def sweep_stale(self, ttl_hours: float = DEFAULT_LEASE_TTL_HOURS) -> list[str]:
        released: list[str] = []
        with self.database.transaction():
            rows = self.connection.execute(
                "SELECT id, claimed_by, claimed_at FROM items WHERE claimed_by IS NOT NULL"
            ).fetchall()
            for row in rows:
                if _lease_expired(row["claimed_at"], ttl_hours):
                    self.connection.execute(
                        "UPDATE items SET claimed_by = NULL, claimed_at = NULL WHERE id = ?", (row["id"],)
                    )
                    self._event(
                        "sweep", row["id"], {"stale_holder": row["claimed_by"], "claimed_at": row["claimed_at"]}
                    )
                    released.append(row["id"])
        return released

    def ready_items(self, ttl_hours: float = DEFAULT_LEASE_TTL_HOURS) -> list[dict[str, Any]]:
        priority_rank = {priority: index for index, priority in enumerate(PRIORITIES)}
        result = []
        for row in self.connection.execute(
            "SELECT * FROM items WHERE state IN ('planning','active') AND blocked_reason IS NULL"
        ):
            if (
                row["claimed_by"]
                and row["claimed_by"] != self.actor
                and not _lease_expired(row["claimed_at"], ttl_hours)
            ):
                continue
            unmet = self.connection.execute(
                "SELECT count(*) AS n FROM item_deps d JOIN items n ON n.id = d.needs_item WHERE d.item_id = ? AND n.state != 'done'",
                (row["id"],),
            ).fetchone()["n"]
            if not unmet:
                result.append(_dict(row))
        return sorted(result, key=lambda item: (priority_rank[item["priority"]], item["id"]))

    def list_items(self, **filters: str | None) -> list[dict[str, Any]]:
        query = "SELECT id, state, priority, worktree, title, claimed_by FROM items WHERE 1=1"
        params: list[str] = []
        for field in ("state", "worktree", "priority"):
            if filters.get(field):
                query += f" AND {field} = ?"
                params.append(str(filters[field]))
        query += " ORDER BY state, priority, id"
        return [_dict(row) for row in self.connection.execute(query, params)]

    def stats(self) -> dict[str, dict[str, int]]:
        def counts(query: str) -> dict[str, int]:
            return {str(row[0]): int(row[1]) for row in self.connection.execute(query)}

        return {
            "items_by_state": counts("SELECT state, count(*) FROM items GROUP BY state"),
            "open_by_priority": counts(
                "SELECT priority, count(*) FROM items WHERE state IN ('planning','active') GROUP BY priority"
            ),
            "open_by_worktree": counts(
                "SELECT worktree, count(*) FROM items WHERE state IN ('planning','active') GROUP BY worktree"
            ),
            "deferrals_by_resolution": counts("SELECT resolution, count(*) FROM deferrals GROUP BY resolution"),
            "claimed": counts(
                "SELECT claimed_by, count(*) FROM items WHERE claimed_by IS NOT NULL GROUP BY claimed_by"
            ),
        }

    def get_config(self, key: str) -> str:
        if key not in CONFIG_KEYS:
            raise TodoError(f"unknown config key {key!r}")
        row = self.connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else "on"

    def set_config(self, key: str, value: str) -> None:
        if key not in CONFIG_KEYS:
            raise TodoError(f"unknown config key {key!r}")
        if value not in CONFIG_KEYS[key]:
            raise TodoError(f"config {key} accepts {CONFIG_KEYS[key]}")
        with self.database.transaction():
            self.connection.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._event("config", None, {"key": key, "value": value})

    def check_scope(self, item_id: str, files: Iterable[str]) -> list[str]:
        item = self.get_item(item_id)
        only = [rule["path_glob"] for rule in item["scope"] if rule["kind"] == "only_modify"]
        deny = [rule["path_glob"] for rule in item["scope"] if rule["kind"] == "do_not_modify"]
        violations = []
        for path in files:
            if path == ".todo-db" or path.startswith(".todo-db/"):
                continue
            if any(fnmatch.fnmatch(path, glob) for glob in deny):
                violations.append(f"{path}: matches do_not_modify")
            elif only and not any(fnmatch.fnmatch(path, glob) for glob in only):
                violations.append(f"{path}: outside only_modify allowlist")
        return violations

    def lint(self, item_id: str) -> list[str]:
        item = self.get_item(item_id)
        findings: list[str] = []
        if not item["verifications"]:
            findings.append("no verification steps recorded")
        elif not any(entry["command"] for entry in item["verifications"]):
            findings.append("verification steps exist but none has a runnable command")
        for entry in item["verifications"]:
            project = _uv_project_missing_pytest(entry.get("command") or "")
            if project is not None:
                findings.append(
                    f"verification seq {entry['seq']} runs pytest through uv project {project!r}, "
                    "but that environment does not declare or inject pytest; use the repository project "
                    "(`uv run -- ...`) or add an explicit `--with pytest`/project dependency"
                )
        if self.get_config("lint.require_scope_rules") == "on" and item["work"] and not item["scope"]:
            findings.append("has work units but no scope rules (only_modify/do_not_modify)")
        if (
            self.get_config("lint.require_w0_revalidation") == "on"
            and EVIDENCE_PIN_RE.search(item["description"] or "")
            and not any(unit["wid"] == "w0" for unit in item["work"])
        ):
            findings.append("description cites point-in-time evidence but has no w0 re-validation unit")
        if not item["work"] and item["state"] in ("planning", "active"):
            findings.append("no work breakdown")
        absolute_scope = [
            f"{rule['kind']}:{rule['path_glob']}"
            for rule in item["scope"]
            if _is_absolute_scope_glob(rule["path_glob"])
        ]
        if absolute_scope:
            findings.append(
                "scope rules must use repository-relative globs; absolute or home-relative rules cannot match "
                f"repository-relative changed paths: {', '.join(absolute_scope)}"
            )
        constant_tests = _constant_shell_test_sequences(item["verifications"])
        if constant_tests:
            findings.append(
                f"verification seq {constant_tests} applies test -z/-n to a static literal, so its result is "
                "constant; assert on a variable, command substitution, file state, or command exit status"
            )
        return findings

    def run_verification(self, item_id: str, seq: int) -> tuple[str, str]:
        if self.database.is_hosted and os.environ.get("TODO_DB_ALLOW_HOSTED_VERIFY_RUN") != "1":
            raise TodoError(
                "refusing to execute a stored verification command from a hosted database: "
                "commands in a shared database are written by other actors, so running them "
                "locally is a lateral code-execution channel across the trust boundary. "
                f"Inspect the command first (`todo-db verify {item_id}`), then set "
                "TODO_DB_ALLOW_HOSTED_VERIFY_RUN=1 to run it deliberately."
            )
        row = self.connection.execute(
            "SELECT * FROM verifications WHERE item_id = ? AND seq = ?", (item_id, seq)
        ).fetchone()
        if row is None:
            raise TodoError(f"no verification seq={seq} on {item_id!r}")
        if not row["command"]:
            raise TodoError(f"verification seq={seq} on {item_id!r} has no command")
        proc = subprocess.run(row["command"], shell=True, capture_output=True, text=True, check=False)
        output = (proc.stdout or "") + (proc.stderr or "")
        result = "pass" if proc.returncode == 0 else "fail"
        with self.database.transaction():
            self.connection.execute(
                "UPDATE verifications SET last_run = ?, last_result = ? WHERE item_id = ? AND seq = ?",
                (utc_now(), result, item_id, seq),
            )
            self._event("verify", item_id, {"seq": seq, "result": result})
        return result, output

    def export_all(self) -> list[dict[str, Any]]:
        return [self.get_item(row["id"]) for row in self.connection.execute("SELECT id FROM items ORDER BY id")]

    def clear_items(self) -> None:
        with self.database.transaction():
            self.connection.execute("DELETE FROM items")
            self._event("replace", None, {"tables": "tracker"})

    def import_yaml_tree(
        self, todo_dir: Path, done_dir: Path | None = None, *, dry_run: bool = False
    ) -> dict[str, Any]:
        try:
            import yaml
        except ImportError as exc:
            raise TodoError("YAML import requires the `todo-db[legacy]` extra") from exc
        report: dict[str, Any] = {
            "imported": [],
            "skipped": [],
            "warnings": [],
            "counts": {
                "open_items": 0,
                "done_items": 0,
                "deps_resolved": 0,
                "deps_dangling": 0,
                "legacy_tasks_structure": 0,
                "legacy_dependencies_field": 0,
            },
        }
        payloads: list[dict[str, Any]] = []
        for root, archived in ((todo_dir, False), (done_dir, True)):
            if root is None:
                continue
            for path in sorted(Path(root).rglob("*.yaml")):
                if "_indexes" in path.parts:
                    continue
                try:
                    data = yaml.safe_load(path.read_text(encoding="utf-8"))
                except Exception as exc:
                    report["warnings"].append(f"{path}: unparseable YAML: {exc}")
                    continue
                if not isinstance(data, dict):
                    report["skipped"].append(f"{path}: not a mapping")
                    continue
                raw_id = str(data.get("id") or path.stem)
                item_id = raw_id if _slug_valid(raw_id) else _slugify(raw_id)
                if item_id != raw_id:
                    report["warnings"].append(f"{path}: id {raw_id!r} sanitized to {item_id!r}")
                status = str(data.get("status") or "").strip().lower()
                if archived:
                    if status != "completed":
                        report["warnings"].append(
                            f"{path}: archive file has status {status!r} (tree/status drift); imported as done"
                        )
                    state, blocked_reason = "done", None
                elif status == "completed":
                    report["warnings"].append(
                        f"{path}: status Completed inside the open tree (tree/status drift); imported as done"
                    )
                    state, blocked_reason = "done", None
                else:
                    state, blocked_reason = STATUS_MAP.get(status or "not started", ("planning", None))
                    if status and status not in STATUS_MAP:
                        report["warnings"].append(f"{path}: unknown status {status!r}; imported as planning")
                work_raw = data.get("work") or []
                if data.get("tasks") and not data.get("work"):
                    report["counts"]["legacy_tasks_structure"] += 1
                work: list[dict[str, Any]] = []
                invalid_open_work = False
                if isinstance(work_raw, dict):
                    work_raw = list(work_raw.values())
                for index, unit in enumerate(work_raw, start=0):
                    if isinstance(unit, str):
                        work.append(
                            {"id": f"w{index}", "summary": unit, "status": "done" if state == "done" else "pending"}
                        )
                    elif isinstance(unit, dict):
                        wid = str(unit.get("id") or unit.get("wid") or f"w{index}")
                        summary = str(unit.get("summary") or unit.get("title") or unit.get("description") or "")
                        summary = summary.strip() or "(no summary recorded)"
                        if len(summary) < 5:
                            summary = f"(w) {summary}"
                        unit_status = unit.get("status", "done" if state == "done" else "pending")
                        duplicate = any(existing["id"] == wid for existing in work)
                        if not WID_RE.fullmatch(wid) or unit_status not in UNIT_STATUSES or duplicate:
                            if state != "done":
                                report["warnings"].append(f"{path}: invalid work unit {wid!r}; item skipped")
                                invalid_open_work = True
                                break
                            report["warnings"].append(f"{path}: invalid archive work unit {wid!r} dropped")
                            continue
                        work.append(
                            {
                                "id": wid,
                                "summary": summary[:200],
                                "needs": list(unit.get("needs") or []),
                                "status": unit_status,
                                "evidence": unit.get("evidence"),
                                "notes": unit.get("notes"),
                            }
                        )
                if state == "done":
                    surviving = {unit["id"] for unit in work}
                    for unit in work:
                        unit["needs"] = [need for need in unit.get("needs", []) if need in surviving]
                if invalid_open_work:
                    report["skipped"].append(str(path))
                    continue
                if archived:
                    title = str(data.get("title") or "").strip() or f"Archived item {path.stem}"
                    if len(title) < 5:
                        title = f"Archived: {title}"
                    description = str(data.get("description") or "").strip()
                    if len(description) < 10:
                        description = (description + "\n(no description recorded in archive)").strip()
                else:
                    title = str(data.get("title", ""))
                    description = str(data.get("description", ""))
                scope_data = data.get("scope_limit") or data.get("scope") or {}
                scope = (
                    [
                        (kind, str(glob))
                        for kind in ("only_modify", "do_not_modify")
                        for glob in (scope_data.get(kind) or [])
                    ]
                    if isinstance(scope_data, dict)
                    else []
                )
                priority = str(data.get("priority") or "medium").strip().lower()
                if priority not in PRIORITIES:
                    report["warnings"].append(f"{path}: unknown priority {priority!r}; using medium")
                    priority = "medium"
                created = str((data.get("metadata") or {}).get("created_date") or "") or None
                created_at = f"{created}T00:00:00Z" if created and "T" not in created else created
                completed = str(data.get("completed_date") or "") or None
                completed_at = f"{completed}T00:00:00Z" if completed and "T" not in completed else completed
                deps = list((data.get("deps") or {}).get("needs", [])) if isinstance(data.get("deps"), dict) else []
                legacy_dependencies = data.get("dependencies")
                if legacy_dependencies:
                    report["counts"]["legacy_dependencies_field"] += 1
                    if isinstance(legacy_dependencies, dict):
                        deps.extend(Path(str(entry)).stem for entry in legacy_dependencies.get("blocked_by") or [])
                payloads.append(
                    {
                        "item_id": item_id,
                        "title": title[:200],
                        "worktree": str(data.get("worktree") or path.parent.parent.name),
                        "priority": priority,
                        "description": description,
                        "category": data.get("category"),
                        "approach": data.get("approach"),
                        "state": state,
                        "blocked_reason": blocked_reason,
                        "created_at": created_at,
                        "completed_at": completed_at if state == "done" else None,
                        "work": work,
                        "deps": [str(dep) for dep in deps],
                        "scope": scope,
                        "verifications": _coerce_verifications(data.get("verification")),
                        "preserves": [str(value) for value in data.get("must_preserve", []) or []],
                        "anti_patterns": [
                            _parse_anti_pattern(str(value)) for value in data.get("anti_patterns", []) or []
                        ],
                        "prior_art": [_parse_prior_art(str(value)) for value in data.get("prior_art", []) or []],
                        "deferrals": [entry for entry in data.get("deferred", []) or [] if isinstance(entry, dict)],
                        "_archive": archived,
                        "_path": str(path),
                    }
                )
        seen: set[str] = set()
        for payload in payloads:
            base = payload["item_id"] or "imported-item"
            candidate = base
            suffix = 2
            while candidate in seen:
                candidate = f"{base}-archived" if payload["_archive"] and suffix == 2 else f"{base}-{suffix}"
                suffix += 1
            if candidate != base:
                report["warnings"].append(f"{payload['_path']}: duplicate id stored as {candidate}")
            payload["item_id"] = candidate
            seen.add(candidate)
        known = set(seen)
        if not dry_run:
            deferred_deps: list[tuple[str, str, str]] = []
            for payload in payloads:
                raw_deps = payload.pop("deps")
                try:
                    self.create_item(**{key: value for key, value in payload.items() if not key.startswith("_")})
                except Exception as exc:
                    report["skipped"].append(f"{payload['_path']}: {exc}")
                    continue
                report["imported"].append(payload["item_id"])
                report["counts"]["done_items" if payload["_archive"] else "open_items"] += 1
                deferred_deps.extend((payload["item_id"], str(dep), payload["_path"]) for dep in raw_deps)
            seen_deps: set[tuple[str, str]] = set()
            for item_id, dep, path in deferred_deps:
                if (item_id, dep) in seen_deps:
                    continue
                seen_deps.add((item_id, dep))
                if dep not in known:
                    report["counts"]["deps_dangling"] += 1
                    report["warnings"].append(f"{path}: dependency {dep!r} not in import set (dangling); skipped")
                    continue
                try:
                    self.add_item_dep(item_id, dep)
                    report["counts"]["deps_resolved"] += 1
                except TodoError as exc:
                    report["warnings"].append(f"{path}: dependency {dep!r} rejected: {exc}")
        else:
            report["imported"] = [payload["item_id"] for payload in payloads]
            report["counts"]["open_items"] = sum(not payload["_archive"] for payload in payloads)
            report["counts"]["done_items"] = sum(payload["_archive"] for payload in payloads)
        return report
