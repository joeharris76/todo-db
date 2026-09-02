"""The findings domain: blind-spot capture, triage, and promotion.

Capture is credential-free: `create` and `candidates` touch only the local
drafts directory. `sync` is the sole credentialed landing step. Landed
findings move through an explicit disposition machine, and every mutation
commits atomically with a hash-chained audit event plus an append-only
`finding_events` provenance row.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .database import TodoDatabase
from .errors import TodoError
from .tracker import TodoTracker, _slugify, default_actor, utc_now


DISPOSITIONS = ("open", "actionable", "actioned", "dismissed", "promoted")
DISPOSITION_REASON_REQUIRED = frozenset({"actionable", "dismissed"})
TERMINAL_DISPOSITIONS = frozenset({"actioned", "dismissed", "promoted"})
DISPOSITION_TRANSITIONS = frozenset(
    {
        ("open", "actionable"),
        ("open", "actioned"),
        ("open", "dismissed"),
        ("open", "promoted"),
        ("actionable", "actioned"),
        ("actionable", "dismissed"),
        ("actionable", "promoted"),
    }
)
LINK_KINDS = ("promoted-to", "informs", "resolved-by", "related-finding", "duplicate-of")
STATUS_TO_DISPOSITION = {
    "open": "open",
    "actionable": "actionable",
    "actioned": "actioned",
    "dismissed": "dismissed",
    "merged-to-todo": "promoted",
}
FINDING_KINDS = frozenset({"framework-gap", "bug-class", "missed-axis", "scope-creep", "assumption", "other"})
ALLOWED_STATUSES = frozenset(STATUS_TO_DISPOSITION)
REQUIRED_FIELDS = frozenset({"id", "date", "status", "finding_kind", "review_context"})
OPTIONAL_FIELDS = frozenset(
    {"related_paths", "suggested_sweep", "todo_id", "observed_sha", "evidence", "urgency", "breadth", "confidence"}
)
ALLOWED_FIELDS = REQUIRED_FIELDS | OPTIONAL_FIELDS
EVIDENCE_KEYS = frozenset({"path", "pattern", "note", "line_start", "line_end"})

FINDING_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{6}-[a-z0-9]+(?:-[a-z0-9]+)*$")
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---(?:\n|\Z)(.*)\Z", re.DOTALL)
REQUIRED_BODY_HEADINGS = ("## Finding", "## Why this matters", "## Suggested next steps")

GATE_ATTESTATION = "class-not-instance"
GATE_TEXT = (
    "defect gate: a finding is a *class* blind spot (a gap in what a review looks for), never a single "
    "defect. A concrete bug belongs in the tracker as a TODO item, not here. Attest with --gate class-not-instance."
)
SYNCED_SUFFIX = ".synced"
SYNCED_PRUNE_DAYS = 30

_FINDING_COLUMNS = (
    "id",
    "date",
    "finding_kind",
    "review_context",
    "observed_sha",
    "title",
    "finding_text",
    "why_matters",
    "next_steps",
    "disposition",
    "disposition_reason",
    "urgency",
    "breadth",
    "confidence",
    "reconsider_after",
    "created_at",
    "imported_from",
)
_CONTENT_FIELDS = (
    "date",
    "finding_kind",
    "review_context",
    "observed_sha",
    "title",
    "finding_text",
    "why_matters",
    "next_steps",
    "disposition",
)


def default_drafts_dir(project_id: str) -> Path:
    override = os.environ.get("TODO_DB_FINDING_DRAFTS_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".todo-db" / "finding-drafts" / project_id


def unsynced_drafts(drafts_dir: str | Path) -> list[Path]:
    directory = Path(drafts_dir).expanduser()
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.glob("*.md") if path.name != "README.md")


def count_unsynced_drafts(drafts_dir: str | Path) -> int:
    return len(unsynced_drafts(drafts_dir))


def _yaml():
    try:
        import yaml
    except ImportError as exc:
        raise TodoError("finding drafts require the `todo-db[findings]` extra") from exc
    return yaml


def _str_or_none(value: Any) -> str | None:
    return None if value is None else str(value)


def _parse_frontmatter(text: str) -> tuple[dict[str, Any] | None, str, list[str]]:
    yaml = _yaml()
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.startswith("---\n"):
        return None, "", ["draft must begin with a YAML frontmatter '---' line"]
    match = FRONTMATTER_RE.match(text)
    if not match:
        return None, "", ["unterminated frontmatter block (no closing '---')"]
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        return None, "", [f"frontmatter is not valid YAML: {exc}"]
    if not isinstance(data, dict):
        return None, "", ["frontmatter must be a YAML mapping"]
    return data, match.group(2), []


def _check_date(data: dict[str, Any]) -> list[str]:
    value = data.get("date")
    if value is None or isinstance(value, date):
        return []
    try:
        date.fromisoformat(str(value))
    except ValueError:
        return [f"date {value!r} is not ISO YYYY-MM-DD"]
    return []


def _check_enums(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    status = data.get("status")
    if status is not None and (not isinstance(status, str) or status not in ALLOWED_STATUSES):
        errors.append(f"status {status!r} not in {sorted(ALLOWED_STATUSES)}")
    kind = data.get("finding_kind")
    if kind is not None and (not isinstance(kind, str) or kind not in FINDING_KINDS):
        errors.append(f"finding_kind {kind!r} not in {sorted(FINDING_KINDS)}")
    context = data.get("review_context")
    if "review_context" in data and (not isinstance(context, str) or not context.strip()):
        errors.append("review_context must be a non-empty string")
    return errors


def _check_evidence(data: dict[str, Any]) -> list[str]:
    entries = data.get("evidence")
    if entries is None:
        return []
    if not isinstance(entries, list):
        return ["evidence must be a list of {path, pattern, note, line_start, line_end} mappings (or null)"]
    errors: list[str] = []
    for position, entry in enumerate(entries):
        where = f"evidence[{position}]"
        if not isinstance(entry, dict):
            errors.append(f"{where} must be a mapping")
            continue
        unknown = sorted(set(entry) - EVIDENCE_KEYS)
        if unknown:
            errors.append(f"{where} has unknown key(s): {unknown}")
        path = entry.get("path")
        if not (isinstance(path, str) and path.strip()):
            errors.append(f"{where}.path is required and must be a non-empty string")
        for key in ("line_start", "line_end"):
            value = entry.get(key)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
                errors.append(f"{where}.{key} must be an integer >= 1 (or null)")
        start, end = entry.get("line_start"), entry.get("line_end")
        if isinstance(start, int) and isinstance(end, int) and start > end:
            errors.append(f"{where}.line_start must not exceed line_end")
    return errors


def _check_optional(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for name in ("urgency", "breadth", "confidence"):
        value = data.get(name)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            errors.append(f"{name} must be a string or integer (or null)")
        elif isinstance(value, str) and not value.strip():
            errors.append(f"{name} must be non-empty when present")
    sha = data.get("observed_sha")
    if "observed_sha" in data and sha is not None and (not isinstance(sha, str) or not sha.strip()):
        errors.append("observed_sha must be a non-empty string (or null)")
    paths = data.get("related_paths")
    if (
        "related_paths" in data
        and paths is not None
        and (not isinstance(paths, list) or not all(isinstance(entry, str) for entry in paths))
    ):
        errors.append("related_paths must be a list of strings (or null)")
    sweep = data.get("suggested_sweep")
    if "suggested_sweep" in data and sweep is not None and (not isinstance(sweep, str) or "\n" in sweep):
        errors.append("suggested_sweep must be a one-line string (or null)")
    todo_id = data.get("todo_id")
    if "todo_id" in data and todo_id is not None and (not isinstance(todo_id, str) or not todo_id.strip()):
        errors.append("todo_id must be a non-empty string (or null)")
    if data.get("status") == "merged-to-todo" and not (isinstance(todo_id, str) and todo_id.strip()):
        errors.append("status 'merged-to-todo' requires a non-empty todo_id")
    return errors


def _check_body(body: str) -> list[str]:
    lines = [line.strip() for line in body.splitlines()]
    errors: list[str] = []
    if not any(line.startswith("# ") and line[2:].strip() for line in lines):
        errors.append("body must include a top-level '# <title>' heading")
    errors.extend(
        f"body must include {heading!r} section" for heading in REQUIRED_BODY_HEADINGS if heading not in lines
    )
    return errors


def validate_draft(path: Path) -> list[str]:
    if not path.exists():
        return [f"file not found: {path}"]
    if path.suffix != ".md":
        return [f"not a markdown file: {path}"]
    if path.name == "README.md":
        return []
    data, body, errors = _parse_frontmatter(path.read_text(encoding="utf-8"))
    if data is None:
        return errors
    errors.extend(f"unknown field {name!r}" for name in sorted(set(data) - ALLOWED_FIELDS))
    errors.extend(f"missing required field {name!r}" for name in sorted(REQUIRED_FIELDS - data.keys()))
    stem = path.stem
    if "id" in data and str(data["id"]) != stem:
        errors.append(f"id {data['id']!r} must equal filename stem {stem!r}")
    if not FINDING_ID_RE.match(stem):
        errors.append(f"filename stem {stem!r} does not match YYYY-MM-DD-HHMMSS-<kebab-slug>")
    errors.extend(_check_date(data))
    errors.extend(_check_enums(data))
    errors.extend(_check_evidence(data))
    errors.extend(_check_optional(data))
    errors.extend(_check_body(body))
    return errors


def _split_body(body: str) -> tuple[str, dict[str, str]]:
    title = ""
    sections: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        if current is not None:
            sections[current] = "\n".join(buffer).strip()

    for line in body.replace("\r\n", "\n").replace("\r", "\n").splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            flush()
            current = stripped[3:].strip()
            buffer = []
        elif stripped.startswith("# ") and not title:
            title = stripped[2:].strip()
        else:
            buffer.append(line)
    flush()
    return title, sections


def parse_draft(path: Path) -> dict[str, Any]:
    errors = validate_draft(path)
    if errors:
        raise TodoError(f"draft {path.name} failed validation: " + "; ".join(errors))
    data, body, _ = _parse_frontmatter(path.read_text(encoding="utf-8"))
    assert data is not None
    title, sections = _split_body(body)
    status = str(data.get("status", "open"))
    disposition = STATUS_TO_DISPOSITION.get(status, "open")
    triage_log = sections.get("Triage log", "").strip()
    reason: str | None = None
    if disposition in DISPOSITION_REASON_REQUIRED:
        reason = triage_log or f"synced from draft {path.stem} (status {status})"
    return {
        "id": path.stem,
        "date": str(data["date"]),
        "finding_kind": str(data["finding_kind"]),
        "review_context": str(data["review_context"]),
        "observed_sha": _str_or_none(data.get("observed_sha")),
        "title": title,
        "finding_text": sections.get("Finding", "").strip(),
        "why_matters": sections.get("Why this matters", "").strip(),
        "next_steps": sections.get("Suggested next steps", "").strip(),
        "disposition": disposition,
        "disposition_reason": reason,
        "urgency": _str_or_none(data.get("urgency")),
        "breadth": _str_or_none(data.get("breadth")),
        "confidence": _str_or_none(data.get("confidence")),
        "evidence": list(data.get("evidence") or []),
    }


def create_draft(
    *,
    title: str,
    finding_kind: str,
    review_context: str,
    gate: str | None,
    drafts_dir: Path,
    fixed_by: str | None = None,
    slug: str | None = None,
    finding: str | None = None,
    why: str | None = None,
    next_steps: str | None = None,
    observed_sha: str | None = None,
) -> Path:
    """Write a draft finding file; never the database."""

    if gate != GATE_ATTESTATION:
        raise TodoError("create requires --gate class-not-instance (the class-not-instance attestation)")
    if finding_kind not in FINDING_KINDS:
        raise TodoError(f"finding_kind {finding_kind!r} not in {sorted(FINDING_KINDS)}")
    if finding_kind == "bug-class" and not (fixed_by and fixed_by.strip()):
        raise TodoError("finding_kind 'bug-class' requires --fixed-by <ref> (a bug-class finding needs a landed fix)")
    stem_slug = _slugify(slug or title)
    if not stem_slug:
        raise TodoError("could not derive a slug; pass --slug <kebab-slug>")
    directory = Path(drafts_dir).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S")
    stem = f"{timestamp}-{stem_slug}"
    path = directory / f"{stem}.md"
    suffix = 2
    while path.exists():
        path = directory / f"{stem}-{suffix}.md"
        suffix += 1
    front = [
        f"id: {path.stem}",
        f"date: {timestamp[:10]}",
        "status: open",
        f"finding_kind: {finding_kind}",
        f"review_context: {json.dumps(review_context)}",
    ]
    if observed_sha:
        front.append(f"observed_sha: {observed_sha}")
    lines = ["---", *front, "---", "", f"# {title}", "", "## Finding", finding or "<verbatim review text>"]
    if finding_kind == "bug-class":
        lines += ["", f"Fixed by: {fixed_by}"]
    lines += ["", "## Why this matters", why or "<one short paragraph on the lesson, not the instance>"]
    lines += ["", "## Suggested next steps", next_steps or "- [ ] <concrete action>", ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    errors = validate_draft(path)
    if errors:
        path.unlink()
        raise TodoError("generated draft failed validation (not written): " + "; ".join(errors))
    return path


def _content_matches(existing: dict[str, Any], fields: dict[str, Any]) -> bool:
    return all((existing.get(name) or None) == (fields.get(name) or None) for name in _CONTENT_FIELDS)


def _mark_synced(path: Path) -> None:
    try:
        path.replace(path.with_name(path.name + SYNCED_SUFFIX))
    except OSError:  # pragma: no cover - best-effort; next sync is idempotent
        pass


def _prune_synced(drafts_dir: Path) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=SYNCED_PRUNE_DAYS)
    pruned = 0
    for path in sorted(drafts_dir.glob("*" + SYNCED_SUFFIX)):
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            if mtime < cutoff:
                path.unlink()
                pruned += 1
        except OSError:  # pragma: no cover
            continue
    return pruned


class FindingsTracker:
    """Transactional finding operations over one :class:`TodoDatabase`."""

    def __init__(self, database: TodoDatabase, actor: str | None = None) -> None:
        self.database = database
        self.actor = actor or default_actor()

    @property
    def connection(self) -> Any:
        return self.database.connection

    def _event(self, action: str, finding_id: str | None, detail: dict[str, Any] | None = None) -> None:
        event = dict(detail or {})
        if finding_id is not None:
            event["finding_id"] = finding_id
        self.database.append_event(actor=self.actor, action=action, detail=event)

    def _log(self, finding_id: str | None, action: str, detail: dict[str, Any] | None = None) -> None:
        self.connection.execute(
            "INSERT INTO finding_events (at, actor, finding_id, action, detail) VALUES (?, ?, ?, ?, ?)",
            (utc_now(), self.actor, finding_id, action, json.dumps(detail or {}, sort_keys=True)),
        )

    def _fetch(self, finding_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM findings WHERE id = ?", (finding_id,)).fetchone()
        return dict(row) if row is not None else None

    def _require_finding(self, finding_id: str) -> dict[str, Any]:
        finding = self._fetch(finding_id)
        if finding is None:
            raise TodoError(f"no such finding: {finding_id}")
        return finding

    def get_finding(self, finding_id: str) -> dict[str, Any]:
        finding = self._require_finding(finding_id)
        finding["evidence"] = [
            dict(row)
            for row in self.connection.execute(
                "SELECT path, pattern, line_start, line_end, note FROM finding_evidence "
                "WHERE finding_id = ? ORDER BY id",
                (finding_id,),
            )
        ]
        finding["links"] = [
            dict(row)
            for row in self.connection.execute(
                "SELECT kind, target_item, target_finding, note FROM finding_links WHERE finding_id = ? ORDER BY id",
                (finding_id,),
            )
        ]
        finding["events"] = [
            dict(row)
            for row in self.connection.execute(
                "SELECT seq, at, actor, action, detail FROM finding_events WHERE finding_id = ? ORDER BY seq",
                (finding_id,),
            )
        ]
        return finding

    def list_findings(self, *, disposition: str | None = None, rank: str | None = None) -> list[dict[str, Any]]:
        """Default order is disposition then age; ranking by the optional triage
        judgement fields is opt-in so missing values never launder a guess into
        a deterministic-looking order."""

        query = "SELECT id, disposition, finding_kind, urgency, breadth, confidence, created_at, title FROM findings"
        params: list[Any] = []
        if disposition:
            query += " WHERE disposition = ?"
            params.append(disposition)
        if rank in ("urgency", "breadth", "confidence"):
            query += f" ORDER BY ({rank} IS NULL), {rank} DESC, created_at, id"
        else:
            query += " ORDER BY disposition, created_at, id"
        return [dict(row) for row in self.connection.execute(query, params)]

    def open_count(self) -> int:
        row = self.connection.execute("SELECT count(*) AS n FROM findings WHERE disposition = 'open'").fetchone()
        return int(row["n"])

    def stats(self) -> dict[str, dict[str, int]]:
        return {
            "findings_by_disposition": {
                str(row[0]): int(row[1])
                for row in self.connection.execute("SELECT disposition, count(*) FROM findings GROUP BY disposition")
            }
        }

    def _insert_finding(self, fields: dict[str, Any], *, imported_from: str | None = None) -> None:
        finding_id = str(fields["id"])
        if not FINDING_ID_RE.match(finding_id):
            raise TodoError(f"invalid finding id {finding_id!r} (expect YYYY-MM-DD-HHMMSS-<kebab-slug>)")
        disposition = fields.get("disposition", "open")
        if disposition not in DISPOSITIONS:
            raise TodoError(f"invalid disposition {disposition!r}; one of {', '.join(DISPOSITIONS)}")
        reason = fields.get("disposition_reason")
        if disposition in DISPOSITION_REASON_REQUIRED and not (reason and str(reason).strip()):
            raise TodoError(f"disposition {disposition!r} requires a reason (finding {finding_id!r})")
        for section in ("title", "finding_text", "why_matters", "next_steps"):
            if not str(fields.get(section) or "").strip():
                raise TodoError(f"finding {finding_id!r} is missing body section {section!r}")
        row = {name: fields.get(name) for name in _FINDING_COLUMNS}
        row["id"] = finding_id
        row["disposition"] = disposition
        row["created_at"] = fields.get("created_at") or utc_now()
        row["imported_from"] = fields.get("imported_from") or imported_from
        placeholders = ", ".join("?" for _ in _FINDING_COLUMNS)
        try:
            self.connection.execute(
                f"INSERT INTO findings ({', '.join(_FINDING_COLUMNS)}) VALUES ({placeholders})",
                tuple(row[name] for name in _FINDING_COLUMNS),
            )
        except sqlite3.IntegrityError as exc:
            raise TodoError(f"cannot insert finding {finding_id!r}: {exc}") from exc
        for entry in fields.get("evidence") or []:
            if not isinstance(entry, dict):
                raise TodoError(f"finding {finding_id!r} evidence entry must be a mapping, got {entry!r}")
            path = entry.get("path")
            if not (isinstance(path, str) and path.strip()):
                raise TodoError(f"finding {finding_id!r} evidence entry needs a non-empty path")
            self.connection.execute(
                "INSERT INTO finding_evidence (finding_id, path, pattern, line_start, line_end, note) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    finding_id,
                    path,
                    entry.get("pattern"),
                    entry.get("line_start"),
                    entry.get("line_end"),
                    entry.get("note"),
                ),
            )

    def sync_drafts(self, drafts_dir: str | Path) -> dict[str, Any]:
        """Insert-if-absent every draft under ``drafts_dir`` by filename-stem id.

        Idempotent: an identical already-landed finding is skipped and its draft
        marked synced. Same-id/different-content is a loud error naming the id,
        never a merge.
        """

        directory = Path(drafts_dir).expanduser()
        result: dict[str, Any] = {"synced": [], "skipped": [], "conflicts": [], "pruned": 0}
        if not directory.is_dir():
            return result
        for path in sorted(directory.glob("*.md")):
            if path.name == "README.md":
                continue
            fields = parse_draft(path)
            existing = self._fetch(fields["id"])
            if existing is not None:
                if _content_matches(existing, fields):
                    result["skipped"].append(fields["id"])
                    _mark_synced(path)
                else:
                    result["conflicts"].append(fields["id"])
                continue
            with self.database.transaction():
                self._insert_finding(fields)
                self._log(fields["id"], "sync", {"disposition": fields["disposition"]})
                self._event("finding-sync", fields["id"], {"disposition": fields["disposition"]})
            _mark_synced(path)
            result["synced"].append(fields["id"])
        result["pruned"] = _prune_synced(directory)
        if result["conflicts"]:
            raise TodoError(
                "sync conflict; same filename-stem id, different content: "
                + ", ".join(result["conflicts"])
                + ". The database copy and the draft disagree; resolve by hand (findings are never merged)."
            )
        return result

    def _set_disposition(self, finding_id: str, new: str, reason: str | None) -> None:
        finding = self._require_finding(finding_id)
        current = finding["disposition"]
        if current == new:
            raise TodoError(f"finding {finding_id!r} is already {new}")
        if current in TERMINAL_DISPOSITIONS:
            raise TodoError(f"finding {finding_id!r} is {current} (terminal); no further transition")
        if (current, new) not in DISPOSITION_TRANSITIONS:
            raise TodoError(f"illegal disposition transition {current!r} -> {new!r} for {finding_id!r}")
        if new in DISPOSITION_REASON_REQUIRED and not (reason and reason.strip()):
            raise TodoError(f"disposition {new!r} requires a --reason")
        self.connection.execute(
            "UPDATE findings SET disposition = ?, disposition_reason = ? WHERE id = ? AND disposition = ?",
            (new, reason, finding_id, current),
        )
        if self.connection.execute("SELECT changes() AS n").fetchone()["n"] != 1:
            raise TodoError(f"finding {finding_id!r} disposition changed concurrently")
        self._log(finding_id, "disposition", {"from": current, "to": new, "reason": reason})
        self._event("finding-disposition", finding_id, {"from": current, "to": new, "reason": reason})

    def dismiss(self, finding_id: str, reason: str) -> None:
        with self.database.transaction():
            self._set_disposition(finding_id, "dismissed", reason)

    def triage(
        self,
        finding_id: str,
        *,
        urgency: str | None = None,
        breadth: str | None = None,
        confidence: str | None = None,
        reconsider_after: str | None = None,
        disposition: str | None = None,
        reason: str | None = None,
    ) -> None:
        updates = [
            (name, value)
            for name, value in (
                ("urgency", urgency),
                ("breadth", breadth),
                ("confidence", confidence),
                ("reconsider_after", reconsider_after),
            )
            if value is not None
        ]
        if not updates and not disposition:
            raise TodoError(
                "triage needs at least one of --urgency/--breadth/--confidence/--reconsider-after/--disposition"
            )
        with self.database.transaction():
            self._require_finding(finding_id)
            if updates:
                assignments = ", ".join(f"{name} = ?" for name, _ in updates)
                self.connection.execute(
                    f"UPDATE findings SET {assignments} WHERE id = ?",
                    (*[value for _, value in updates], finding_id),
                )
                self._log(finding_id, "triage", dict(updates))
                self._event("finding-triage", finding_id, dict(updates))
            if disposition:
                self._set_disposition(finding_id, disposition, reason)

    def link(
        self,
        finding_id: str,
        *,
        kind: str,
        target_item: str | None = None,
        target_finding: str | None = None,
        note: str | None = None,
    ) -> None:
        if kind not in LINK_KINDS:
            raise TodoError(f"invalid link kind {kind!r}; one of {', '.join(LINK_KINDS)}")
        if kind == "promoted-to":
            raise TodoError("'promoted-to' links are created by `finding promote`, not `link`")
        if bool(target_item) == bool(target_finding):
            raise TodoError("link needs exactly one of --to-item / --to-finding")
        with self.database.transaction():
            self._require_finding(finding_id)
            if (
                target_item
                and self.connection.execute("SELECT 1 FROM items WHERE id = ?", (target_item,)).fetchone() is None
            ):
                raise TodoError(f"link target item {target_item!r} does not exist")
            if target_finding:
                self._require_finding(target_finding)
            self.connection.execute(
                "INSERT INTO finding_links (finding_id, kind, target_item, target_finding, note) VALUES (?, ?, ?, ?, ?)",
                (finding_id, kind, target_item, target_finding, note),
            )
            detail = {"kind": kind, "target_item": target_item, "target_finding": target_finding}
            self._log(finding_id, "link", detail)
            self._event("finding-link", finding_id, detail)

    def promote(
        self,
        finding_id: str,
        new_item_id: str,
        *,
        title: str | None = None,
        priority: str = "medium",
        worktree: str | None = None,
        description: str | None = None,
    ) -> None:
        """Create a planning item from a finding, link it, and flip the finding
        to ``promoted`` in one transaction; a payload failure rolls back with no
        orphan link and no disposition flip."""

        tracker = TodoTracker(self.database, actor=self.actor)
        with self.database.transaction():
            finding = self._require_finding(finding_id)
            if finding["disposition"] in TERMINAL_DISPOSITIONS:
                raise TodoError(f"finding {finding_id!r} is {finding['disposition']} (terminal); cannot promote")
            item_title = title or f"Promoted finding {finding_id}"[:200]
            tracker._insert_item(
                item_id=new_item_id,
                title=item_title,
                worktree=worktree or "main",
                priority=priority,
                description=description
                or f"Promoted from finding {finding_id}. Run `finding_show(id='{finding_id}')` for the review prose.",
            )
            tracker._event(
                "create", new_item_id, {"title": item_title, "state": "planning", "promoted_from_finding": finding_id}
            )
            self.connection.execute(
                "INSERT INTO finding_links (finding_id, kind, target_item, note) VALUES (?, 'promoted-to', ?, ?)",
                (finding_id, new_item_id, f"promoted by {self.actor}"),
            )
            self.connection.execute(
                "UPDATE findings SET disposition = 'promoted' WHERE id = ? AND disposition IN ('open','actionable')",
                (finding_id,),
            )
            if self.connection.execute("SELECT changes() AS n").fetchone()["n"] != 1:
                raise TodoError(f"finding {finding_id!r} disposition changed concurrently")
            self._log(finding_id, "promote", {"new_item": new_item_id})
            self._event("finding-promote", finding_id, {"new_item": new_item_id})
