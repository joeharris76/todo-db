"""Project-bound database operations shared by local and hosted backends."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from importlib import metadata, resources
from pathlib import Path
from typing import Any

from .audit import (
    AUDIT_HASH_ALGORITHM,
    AUDIT_HASH_VERSION,
    canonical_json,
    event_hash,
    verify_audit_head,
    verify_event_chain,
)
from .backends import connect
from .errors import (
    AuditIntegrityError,
    ProjectIdentityMismatchError,
    SchemaBehindError,
    SchemaDivergedError,
    SchemaMismatchError,
)
from .models import CredentialMode, DatabaseConfig, ProjectIdentity


SCHEMA_VERSION = 7
try:
    TOOL_VERSION = metadata.version("todo-db")
except metadata.PackageNotFoundError:  # running from an unbuilt source tree
    TOOL_VERSION = "0+unknown"
EXPORT_FORMAT_VERSION = 2

# Tables a legacy snapshot MAY carry beyond restore_legacy's required set. The
# distinction matters: anything in neither set is rejected rather than ignored,
# because ignoring it means restoring a snapshot that carries a domain this
# importer cannot load would report success while silently discarding it.
OPTIONAL_LEGACY_TABLES = frozenset(
    {
        "findings",
        "finding_evidence",
        "finding_links",
        "finding_events",
        "finding_sections",
    }
)

# The four registries below describe the same set of tracker tables from four
# angles: how to read each one, what columns it has, and the order to delete
# and insert them under the foreign keys. They were method-local literals and
# had to be kept in step by hand; adding finding_sections in PR #1 missed two
# of them, and the restore reported success with the table empty. Hoisted here
# so a test can assert they agree -- see tests/test_contracts.py.

EXPORT_TABLE_QUERIES = {
    "items": "SELECT * FROM items ORDER BY id",
    "work_units": "SELECT * FROM work_units ORDER BY item_id, wid",
    "work_needs": "SELECT * FROM work_needs ORDER BY item_id, wid, needs_wid",
    "item_deps": "SELECT * FROM item_deps ORDER BY item_id, needs_item",
    "scope_rules": "SELECT * FROM scope_rules ORDER BY item_id, kind, path_glob",
    "verifications": "SELECT * FROM verifications ORDER BY item_id, seq",
    "preserves": "SELECT * FROM preserves ORDER BY item_id, behavior",
    "anti_patterns": "SELECT * FROM anti_patterns ORDER BY item_id, dont",
    "prior_art": "SELECT * FROM prior_art ORDER BY item_id, path, concept",
    "deferrals": "SELECT * FROM deferrals ORDER BY from_item, id",
    "meta": "SELECT * FROM meta ORDER BY key",
    "findings": "SELECT * FROM findings ORDER BY id",
    "finding_evidence": "SELECT * FROM finding_evidence ORDER BY id",
    "finding_links": "SELECT * FROM finding_links ORDER BY id",
    "finding_events": "SELECT * FROM finding_events ORDER BY seq",
    "finding_sections": "SELECT * FROM finding_sections ORDER BY finding_id, position",
}

RESTORE_TABLE_COLUMNS = {
    "items": (
        "id",
        "title",
        "worktree",
        "priority",
        "state",
        "blocked_reason",
        "category",
        "description",
        "approach",
        "claimed_by",
        "claimed_at",
        "claimed_session",
        "claim_token",
        "claimed_branch",
        "claimed_worktree",
        "git_baseline",
        "created_at",
        "completed_at",
        "completed_pr",
    ),
    "work_units": (
        "item_id",
        "wid",
        "summary",
        "status",
        "evidence",
        "notes",
        "started_at",
        "started_worktree",
        "started_branch",
    ),
    "work_needs": ("item_id", "wid", "needs_wid"),
    "item_deps": ("item_id", "needs_item"),
    "scope_rules": ("item_id", "kind", "path_glob"),
    "verifications": (
        "item_id",
        "seq",
        "description",
        "command",
        "expected",
        "last_run",
        "last_result",
        "workspace_fingerprint",
    ),
    "preserves": ("item_id", "behavior"),
    "anti_patterns": ("item_id", "dont", "why", "instead"),
    "prior_art": ("item_id", "path", "concept", "decision"),
    "deferrals": (
        "id",
        "from_item",
        "summary",
        "reason",
        "resolution",
        "resolved_item",
        "resolved_reason",
        "created_at",
    ),
    "meta": ("key", "value"),
    "findings": (
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
        "related_paths",
        "suggested_sweep",
    ),
    "finding_evidence": ("id", "finding_id", "path", "pattern", "line_start", "line_end", "note"),
    "finding_links": ("id", "finding_id", "kind", "target_item", "target_finding", "note"),
    "finding_events": ("seq", "at", "actor", "finding_id", "action", "detail"),
    "finding_sections": ("id", "finding_id", "position", "heading", "text"),
}

RESTORE_DELETE_ORDER = (
    "finding_evidence",
    "finding_links",
    "finding_events",
    "finding_sections",
    "findings",
    "work_needs",
    "item_deps",
    "scope_rules",
    "verifications",
    "preserves",
    "anti_patterns",
    "prior_art",
    "deferrals",
    "work_units",
    "items",
    "meta",
)

RESTORE_INSERT_ORDER = (
    "items",
    "work_units",
    "work_needs",
    "item_deps",
    "scope_rules",
    "verifications",
    "preserves",
    "anti_patterns",
    "prior_art",
    "deferrals",
    "meta",
    "findings",
    "finding_evidence",
    "finding_links",
    "finding_events",
    # After findings: sections reference findings(id).
    "finding_sections",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _migration_files() -> list[tuple[int, str, str]]:
    root = resources.files("todo_db.migrations")
    migrations: list[tuple[int, str, str]] = []
    for resource in root.iterdir():
        if resource.suffix != ".sql":
            continue
        version_text, _, name = resource.name.partition("_")
        if not version_text.isdigit():
            raise RuntimeError(f"migration filename must start with a numeric version: {resource.name}")
        sql = resource.read_text(encoding="utf-8")
        checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        migrations.append((int(version_text), name.removesuffix(".sql"), checksum))
    return sorted(migrations, key=lambda record: record[0])


def _migration_resource(version: int):
    root = resources.files("todo_db.migrations")
    for resource in root.iterdir():
        if resource.suffix != ".sql":
            continue
        version_text, _, _ = resource.name.partition("_")
        if version_text.isdigit() and int(version_text) == version:
            return resource
    raise RuntimeError(f"missing packaged migration version: {version}")


def _migration_sql(version: int) -> str:
    return _migration_resource(version).read_text(encoding="utf-8")


def _split_statements(sql: str) -> list[str]:
    """Split migration SQL on `;`, honoring quotes, comments, and BEGIN/CASE...END bodies."""

    statements: list[str] = []
    buffer: list[str] = []
    depth = 0
    index, length = 0, len(sql)
    while index < length:
        char = sql[index]
        if char == "[":
            # SQLite bracket-quoted identifier: `CREATE TABLE [my; table]` must not split on the `;` inside.
            end = sql.find("]", index + 1)
            if end == -1:
                buffer.append(sql[index:])
                break
            while end + 1 < length and sql[end + 1] == "]":
                nxt = sql.find("]", end + 2)
                if nxt == -1:
                    end = length - 1
                    break
                end = nxt
            buffer.append(sql[index : end + 1])
            index = end + 1
        elif char in ("'", '"', "`"):
            end = index + 1
            while end < length:
                if sql[end] == char and not (end + 1 < length and sql[end + 1] == char):
                    break
                end += 2 if sql[end] == char else 1
            buffer.append(sql[index : end + 1])
            index = end + 1
        elif sql.startswith("--", index):
            end = sql.find("\n", index)
            index = length if end == -1 else end
        elif sql.startswith("/*", index):
            end = sql.find("*/", index)
            index = length if end == -1 else end + 2
            buffer.append(" ")
        elif char.isalpha() or char == "_":
            end = index
            while end < length and (sql[end].isalnum() or sql[end] == "_"):
                end += 1
            word = sql[index:end].upper()
            if word in ("BEGIN", "CASE"):
                depth += 1
            elif word == "END":
                depth = max(depth - 1, 0)
            buffer.append(sql[index:end])
            index = end
        elif char == ";" and depth == 0:
            statement = "".join(buffer).strip()
            if statement:
                statements.append(statement)
            buffer = []
            index += 1
        else:
            buffer.append(char)
            index += 1
    tail = "".join(buffer).strip()
    if tail:
        statements.append(tail)
    return statements


class TodoDatabase:
    """One project-bound tracker database."""

    def __init__(self, connection: Any, config: DatabaseConfig) -> None:
        self._connection = connection
        self._config = config
        self._project_identity: ProjectIdentity | None = None

    @classmethod
    def open(cls, config: DatabaseConfig) -> "TodoDatabase":
        connection = connect(config)
        database = cls(connection, config)
        try:
            if config.credential_mode is CredentialMode.READ_ONLY:
                database._check_schema()
                database._check_identity()
            else:
                database._migrate()
                database._bind_identity()
                database._upgrade_audit_history()
                database._ensure_audit_head()
            policy = os.environ.get("TODO_DB_AUDIT_OPEN_POLICY", "head").lower()
            if policy == "full":
                database.verify_audit()
            else:
                database._check_audit_head()
            return database
        except BaseException:
            connection.close()
            raise

    @property
    def project_identity(self) -> ProjectIdentity:
        if self._project_identity is None:
            row = self._connection.execute(
                "SELECT project_id, repository FROM project_identity WHERE singleton = 1"
            ).fetchone()
            if row is None:
                raise ProjectIdentityMismatchError("project identity is missing")
            self._project_identity = ProjectIdentity(project_id=row["project_id"], repository=row["repository"])
        return self._project_identity

    @property
    def connection(self) -> Any:
        """Return the sqlite-compatible connection for tracker services."""

        return self._connection

    @property
    def is_hosted(self) -> bool:
        """True for hosted databases and legacy replica artifacts kept behind safety gates."""
        if self._config.is_hosted:
            return True
        path = Path(self._config.path)
        if path.is_file():
            if path.name.startswith("replica.") or path.name == "replica.db":
                return True
            parent = path.parent
            if (
                (parent / f"{path.name}-info").exists()
                or (parent / f"{path.name}-durable").exists()
                or (parent / f"{path.name}.replica-info").exists()
            ):
                return True
        return False

    @property
    def schema_version(self) -> int:
        row = self._connection.execute("SELECT max(version) AS version FROM schema_migrations").fetchone()
        return int(row["version"] or 0)

    def get_metadata(self, key: str) -> str | None:
        row = self._connection.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_metadata(self, key: str, value: str) -> None:
        self._assert_writable()
        with self._write_transaction():
            self._connection.execute(
                "INSERT INTO metadata(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def record_event(self, *, actor: str, action: str, detail: dict[str, Any]) -> int:
        self._assert_writable()
        with self.transaction():
            return self.append_event(actor=actor, action=action, detail=detail)

    def append_event(self, *, actor: str, action: str, detail: dict[str, Any]) -> int:
        """Append an audit event inside the caller's active transaction."""

        self._assert_writable()
        detail_json = canonical_json(detail)
        at = _utc_now()
        previous = self._connection.execute("SELECT event_hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        previous_hash = previous["event_hash"] if previous else None
        next_seq = self._connection.execute("SELECT COALESCE(max(seq), 0) + 1 AS seq FROM events").fetchone()["seq"]
        seq = int(next_seq)
        digest = event_hash(
            identity=self.project_identity,
            seq=seq,
            at=at,
            actor=actor,
            action=action,
            detail=detail,
            prev_hash=previous_hash,
        )
        cursor = self._connection.execute(
            "INSERT INTO events(seq, at, actor, action, detail, prev_hash, event_hash, hash_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (seq, at, actor, action, detail_json, previous_hash, digest, AUDIT_HASH_VERSION),
        )
        self._set_audit_head(seq, digest)
        return int(cursor.lastrowid or seq)

    def export(self) -> dict[str, Any]:
        """Return a canonical, metadata- and event-inclusive export envelope."""

        integrity = self.verify_audit()
        migration_rows = [
            dict(row)
            for row in self._connection.execute(
                "SELECT version, name, checksum, applied_at, tool_version FROM schema_migrations ORDER BY version"
            )
        ]
        identity = self.project_identity
        metadata_rows = [dict(row) for row in self._connection.execute("SELECT key, value FROM metadata ORDER BY key")]
        event_rows = []
        for row in self._connection.execute(
            "SELECT seq, at, actor, action, detail, prev_hash, event_hash, hash_version FROM events ORDER BY seq"
        ):
            event = dict(row)
            event["detail"] = json.loads(event["detail"])
            event_rows.append(event)
        identity_row = {
            "singleton": 1,
            "project_id": identity.project_id,
            "repository": identity.repository,
        }
        audit_head = dict(
            self._connection.execute(
                "SELECT singleton, head_seq, head_hash FROM audit_head WHERE singleton = 1"
            ).fetchone()
        )
        tracker_queries = EXPORT_TABLE_QUERIES
        tables = {
            "project_identity": [identity_row],
            "metadata": metadata_rows,
            "events": event_rows,
            "audit_head": [audit_head],
            "schema_migrations": migration_rows,
        }
        for table, query in tracker_queries.items():
            tables[table] = [dict(row) for row in self._connection.execute(query)]
        return {
            "format_version": EXPORT_FORMAT_VERSION,
            "project": {
                "project_id": identity.project_id,
                "repository": identity.repository,
            },
            "schema": {"version": self.schema_version, "migrations": migration_rows},
            "integrity": integrity,
            "metadata": {row["key"]: row["value"] for row in metadata_rows},
            "events": event_rows,
            "tables": tables,
        }

    def restore(self, exported: dict[str, Any]) -> None:
        """Replace tracker state from a verified export envelope."""

        self._assert_writable()
        if exported.get("format_version") != EXPORT_FORMAT_VERSION:
            raise SchemaMismatchError("unsupported export format version")
        if exported.get("schema", {}).get("version") != self.schema_version:
            raise SchemaMismatchError("export schema version does not match the installed schema")
        exported_migrations = [dict(row) for row in exported.get("schema", {}).get("migrations", [])]
        installed_migrations = [
            dict(row)
            for row in self._connection.execute(
                "SELECT version, name, checksum, applied_at, tool_version FROM schema_migrations ORDER BY version"
            )
        ]

        def migration_identity(row: dict[str, Any]) -> tuple[int, str, str]:
            return (int(row["version"]), str(row["name"]), str(row["checksum"]))

        if [migration_identity(row) for row in exported_migrations] != [
            migration_identity(row) for row in installed_migrations
        ]:
            raise SchemaMismatchError("export migration history does not match the installed schema")
        project = exported.get("project") or {}
        if project != {
            "project_id": self.project_identity.project_id,
            "repository": self.project_identity.repository,
        }:
            raise ProjectIdentityMismatchError("export project identity does not match the target database")
        events = [dict(event) for event in exported.get("events", [])]
        integrity = exported.get("integrity") or {}
        verify_event_chain(
            events,
            identity=self.project_identity,
            head_seq=int(integrity.get("head_seq", integrity.get("event_count", 0))),
            head_hash=integrity.get("head_hash"),
        )
        tables = exported.get("tables") or {}
        if [dict(row) for row in tables.get("schema_migrations", [])] != exported_migrations:
            raise SchemaMismatchError("export schema migration records disagree with the table payload")
        table_columns = RESTORE_TABLE_COLUMNS
        delete_order = RESTORE_DELETE_ORDER
        insert_order = RESTORE_INSERT_ORDER
        with self.transaction():
            for row in exported_migrations:
                self._connection.execute(
                    "UPDATE schema_migrations SET applied_at = ?, tool_version = ? WHERE version = ?",
                    (row["applied_at"], row["tool_version"], row["version"]),
                )
            for table in delete_order:
                self._connection.execute(f"DELETE FROM {table}")
            self._connection.execute("DELETE FROM metadata")
            self._connection.execute("DELETE FROM events")
            for row in tables.get("metadata", []):
                self._connection.execute("INSERT INTO metadata(key, value) VALUES (?, ?)", (row["key"], row["value"]))
            for event in events:
                detail = event["detail"] if isinstance(event["detail"], dict) else json.loads(event["detail"])
                self._connection.execute(
                    "INSERT INTO events(seq, at, actor, action, detail, prev_hash, event_hash, hash_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        event["seq"],
                        event["at"],
                        event["actor"],
                        event["action"],
                        canonical_json(detail),
                        event["prev_hash"],
                        event["event_hash"],
                        event.get("hash_version", AUDIT_HASH_VERSION),
                    ),
                )
            for table in insert_order:
                columns = table_columns[table]
                for row in tables.get(table, []):
                    values = [row.get(column) for column in columns]
                    placeholders = ", ".join("?" for _ in columns)
                    self._connection.execute(
                        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
                        values,
                    )
            head = tables.get("audit_head") or [
                {"head_seq": integrity.get("head_seq", 0), "head_hash": integrity.get("head_hash")}
            ]
            self._connection.execute(
                "UPDATE audit_head SET head_seq = ?, head_hash = ? WHERE singleton = 1",
                (head[0]["head_seq"], head[0]["head_hash"]),
            )
        self.verify_audit()

    def restore_legacy(self, snapshot: dict[str, Any]) -> None:
        """Replace tracker state from a BenchBox legacy-schema snapshot.

        Tracker rows and legacy event provenance are preserved. Legacy events
        are deterministically re-hashed under the standalone audit contract;
        the destination's packaged migration history remains authoritative.
        """

        self._assert_writable()
        base = self.export()
        nested_fields = {
            "work",
            "deps",
            "scope",
            "verifications",
            "preserves",
            "anti_patterns",
            "prior_art",
            "deferrals",
        }
        tracker_tables = (
            "work_units",
            "work_needs",
            "item_deps",
            "scope_rules",
            "verifications",
            "preserves",
            "anti_patterns",
            "prior_art",
            "deferrals",
            "meta",
        )
        required_tables = {"items", "events", *tracker_tables}
        missing_tables = sorted(required_tables - snapshot.keys())
        if missing_tables:
            raise SchemaMismatchError(f"legacy snapshot is missing required tables: {', '.join(missing_tables)}")
        # Reject what we cannot load, rather than ignoring it. Silently dropping
        # an unrecognised table is the dangerous failure: the restore reports
        # success and the data is simply gone, with nothing to notice it by.
        unexpected_tables = sorted(snapshot.keys() - required_tables - OPTIONAL_LEGACY_TABLES)
        if unexpected_tables:
            raise SchemaMismatchError(
                "legacy snapshot carries tables this importer cannot load: "
                f"{', '.join(unexpected_tables)}. Restoring would discard them silently. "
                "Teach restore_legacy to load them, or drop them from the snapshot deliberately."
            )
        tables = dict(base["tables"])
        tables["metadata"] = []
        tables["items"] = [
            {key: value for key, value in dict(item).items() if key not in nested_fields}
            for item in snapshot.get("items", [])
        ]
        for table in tracker_tables:
            tables[table] = [dict(row) for row in snapshot.get(table, [])]
        # Findings are optional in a legacy snapshot, but `--replace` means
        # replace: an absent table becomes empty rather than leaving whatever
        # the destination held, so a restore never yields a hybrid of two
        # trackers. Same `.get(table, [])` semantics as the required tables.
        for table in sorted(OPTIONAL_LEGACY_TABLES):
            tables[table] = [dict(row) for row in snapshot.get(table, [])]

        events: list[dict[str, Any]] = []
        previous_hash: str | None = None
        for expected_seq, legacy_event in enumerate(snapshot.get("events", []), start=1):
            row = dict(legacy_event)
            seq = int(row["seq"])
            if seq != expected_seq:
                raise AuditIntegrityError(f"legacy audit provenance: expected sequence {expected_seq}, got {seq}")
            raw_detail = row.get("detail")
            detail = json.loads(raw_detail) if isinstance(raw_detail, str) else dict(raw_detail or {})
            if row.get("item_id") is not None:
                detail["item_id"] = row["item_id"]
            digest = event_hash(
                identity=self.project_identity,
                seq=seq,
                at=str(row["at"]),
                actor=str(row["actor"]),
                action=str(row["action"]),
                detail=detail,
                prev_hash=previous_hash,
            )
            events.append(
                {
                    "seq": seq,
                    "at": row["at"],
                    "actor": row["actor"],
                    "action": row["action"],
                    "detail": detail,
                    "prev_hash": previous_hash,
                    "event_hash": digest,
                    "hash_version": AUDIT_HASH_VERSION,
                }
            )
            previous_hash = digest

        integrity = {
            "algorithm": AUDIT_HASH_ALGORITHM,
            "event_count": len(events),
            "head_seq": len(events),
            "head_hash": previous_hash,
        }
        tables["events"] = events
        tables["audit_head"] = [{"singleton": 1, "head_seq": len(events), "head_hash": previous_hash}]
        converted = {
            **base,
            "integrity": integrity,
            "events": events,
            "tables": tables,
        }
        self.restore(converted)

    def verify_audit(self) -> dict[str, Any]:
        head = self._connection.execute("SELECT head_seq, head_hash FROM audit_head WHERE singleton = 1").fetchone()
        if head is None:
            raise AuditIntegrityError("audit integrity: audit head is missing")
        rows = [
            dict(row)
            for row in self._connection.execute(
                "SELECT seq, at, actor, action, detail, prev_hash, event_hash, hash_version FROM events ORDER BY seq"
            )
        ]
        return verify_event_chain(
            rows,
            identity=self.project_identity,
            head_seq=int(head["head_seq"]),
            head_hash=head["head_hash"],
        )

    def _check_audit_head(self) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT h.head_seq, h.head_hash, "
            "(SELECT count(*) FROM events) AS event_count, "
            "(SELECT seq FROM events ORDER BY seq DESC LIMIT 1) AS last_seq, "
            "(SELECT event_hash FROM events ORDER BY seq DESC LIMIT 1) AS last_hash "
            "FROM audit_head h WHERE h.singleton = 1"
        ).fetchone()
        if row is None:
            raise AuditIntegrityError("audit integrity: audit head is missing")
        last_event = {"seq": row["last_seq"], "event_hash": row["last_hash"]} if row["last_seq"] is not None else None
        return verify_audit_head(
            head_seq=int(row["head_seq"]),
            head_hash=row["head_hash"],
            last_event=last_event,
            event_count=int(row["event_count"]),
        )

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "TodoDatabase":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _assert_writable(self) -> None:
        if self._config.credential_mode is CredentialMode.READ_ONLY:
            raise sqlite3.OperationalError("attempt to write a readonly database")

    @contextmanager
    def transaction(self):
        """Run a writable operation atomically on either backend."""

        self._assert_writable()
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._connection.commit()
        except BaseException:
            self._connection.rollback()
            raise

    _write_transaction = transaction

    def _migrate(self) -> None:
        expected = _migration_files()
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            table_exists = self._connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            ).fetchone()
            if table_exists is None:
                existing_tables = {
                    row["name"] for row in self._connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                foreign_tracker_tables = existing_tables & {"items", "work_units", "item_deps", "meta"}
                if foreign_tracker_tables:
                    raise SchemaDivergedError(
                        "database contains a different tracker schema "
                        f"({', '.join(sorted(foreign_tracker_tables))}); use a dedicated todo-db path"
                    )
                applied: list[tuple[int, str, str]] = []
            else:
                rows = self._connection.execute(
                    "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
                ).fetchall()
                applied = [(row["version"], row["name"], row["checksum"]) for row in rows]
                expected_prefix = [record[:3] for record in expected[: len(applied)]]
                if applied != expected_prefix:
                    raise SchemaDivergedError(
                        f"E_SCHEMA_DIVERGED: schema migration mismatch: expected {expected_prefix}, got {applied}; database migrations differ from codebase"
                    )
            current = applied[-1][0] if applied else 0
            for version, name, checksum in expected:
                if version <= current:
                    continue
                for statement in _split_statements(_migration_sql(version)):
                    self._connection.execute(statement)
                self._connection.execute(
                    "INSERT INTO schema_migrations(version, name, checksum, applied_at, tool_version) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (version, name, checksum, _utc_now(), TOOL_VERSION),
                )
                current = version
            self._connection.commit()
        except BaseException:
            self._connection.rollback()
            raise
        self._check_schema()

    def _check_schema(self) -> None:
        rows = self._connection.execute(
            "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
        expected = _migration_files()
        actual = [(row["version"], row["name"], row["checksum"]) for row in rows]
        expected_records = [(version, name, checksum) for version, name, checksum in expected]
        if actual != expected_records:
            expected_prefix = expected_records[: len(actual)]
            if actual == expected_prefix and len(actual) < len(expected_records):
                curr_ver = actual[-1][0] if actual else 0
                exp_ver = expected_records[-1][0]
                raise SchemaBehindError(
                    f"E_SCHEMA_BEHIND: database schema version {curr_ver} is behind expected version {exp_ver}; "
                    "re-open with write credentials to apply pending migrations"
                )
            raise SchemaDivergedError(
                f"E_SCHEMA_DIVERGED: schema migration mismatch: expected {expected_records}, got {actual}"
            )

    def _bind_identity(self) -> None:
        row = self._connection.execute(
            "SELECT project_id, repository FROM project_identity WHERE singleton = 1"
        ).fetchone()
        if row is None:
            if self._config.identity is None:
                raise ProjectIdentityMismatchError(
                    "database has no bound project identity and none was supplied; "
                    "bind one first with `todo-db init --project-id <id> --repository <url>` "
                    "(or scaffold the repo with `todo-db init-project`)"
                )
            with self._write_transaction():
                self._connection.execute(
                    "INSERT INTO project_identity(singleton, project_id, repository) VALUES (1, ?, ?)",
                    (self._config.identity.project_id, self._config.identity.repository),
                )
                self._project_identity = self._config.identity
            return
        self._check_identity()

    def _upgrade_audit_history(self) -> None:
        needs_upgrade = self._connection.execute(
            "SELECT 1 FROM events WHERE hash_version != ? LIMIT 1", (AUDIT_HASH_VERSION,)
        ).fetchone()
        if needs_upgrade is None:
            return
        head = self._connection.execute("SELECT head_seq, head_hash FROM audit_head WHERE singleton = 1").fetchone()
        if head is not None and int(head["head_seq"]) > 0:
            raise AuditIntegrityError("audit integrity: invalid hash_version in existing audit chain")

        rows = [
            dict(row)
            for row in self._connection.execute(
                "SELECT seq, at, actor, action, detail, prev_hash, event_hash, hash_version FROM events ORDER BY seq"
            )
        ]
        if not rows:
            return

        expected_seq = 1
        for row in rows:
            if int(row["seq"]) != expected_seq:
                raise AuditIntegrityError(f"audit integrity: expected sequence {expected_seq}, got {row['seq']}")
            expected_seq += 1
            try:
                json.loads(row["detail"])
            except Exception as exc:
                raise AuditIntegrityError(f"audit integrity: invalid detail JSON at seq {row['seq']}") from exc

        identity = self.project_identity
        with self._write_transaction():
            previous_hash: str | None = None
            for row in rows:
                detail = json.loads(row["detail"])
                digest = event_hash(
                    identity=identity,
                    seq=int(row["seq"]),
                    at=row["at"],
                    actor=row["actor"],
                    action=row["action"],
                    detail=detail,
                    prev_hash=previous_hash,
                )
                self._connection.execute(
                    "UPDATE events SET prev_hash = ?, event_hash = ?, hash_version = ? WHERE seq = ?",
                    (previous_hash, digest, AUDIT_HASH_VERSION, row["seq"]),
                )
                previous_hash = digest
            self._set_audit_head(int(rows[-1]["seq"]), previous_hash)

    def _ensure_audit_head(self) -> None:
        row = self._connection.execute("SELECT singleton FROM audit_head WHERE singleton = 1").fetchone()
        if row is None:
            with self._write_transaction():
                self._set_audit_head(0, None)

    def _set_audit_head(self, seq: int, digest: str | None) -> None:
        self._connection.execute(
            "INSERT INTO audit_head(singleton, head_seq, head_hash) VALUES (1, ?, ?) "
            "ON CONFLICT(singleton) DO UPDATE SET head_seq = excluded.head_seq, head_hash = excluded.head_hash",
            (seq, digest),
        )

    def _check_identity(self) -> None:
        if self._config.identity is None:
            # No caller-supplied identity: reads and writes proceed under the
            # identity already bound in the database.  The mismatch guard only
            # enforces when the caller asserts an identity.
            return
        stored = self.project_identity
        if stored != self._config.identity:
            raise ProjectIdentityMismatchError(
                "project identity mismatch: "
                f"database={stored.project_id}@{stored.repository}, "
                f"requested={self._config.identity.project_id}@{self._config.identity.repository}"
            )
