"""Project-bound database operations shared by local and hosted backends."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from importlib import resources
from typing import Any

from .audit import AUDIT_HASH_VERSION, canonical_json, event_hash, verify_event_chain
from .backends import connect
from .errors import AuditIntegrityError, ProjectIdentityMismatchError, SchemaMismatchError
from .models import CredentialMode, DatabaseConfig, ProjectIdentity


SCHEMA_VERSION = 3
TOOL_VERSION = "0.1.0"
EXPORT_FORMAT_VERSION = 2


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


class TodoDatabase:
    """One project-bound tracker database."""

    def __init__(self, connection: Any, config: DatabaseConfig) -> None:
        self._connection = connection
        self._config = config

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
            database.verify_audit()
            return database
        except BaseException:
            connection.close()
            raise

    @property
    def project_identity(self) -> ProjectIdentity:
        row = self._connection.execute(
            "SELECT project_id, repository FROM project_identity WHERE singleton = 1"
        ).fetchone()
        if row is None:
            raise ProjectIdentityMismatchError("project identity is missing")
        return ProjectIdentity(project_id=row["project_id"], repository=row["repository"])

    @property
    def connection(self) -> Any:
        """Return the sqlite-compatible connection for tracker services."""

        return self._connection

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
        tracker_queries = {
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
        }
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
        table_columns = {
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
            "verifications": ("item_id", "seq", "description", "command", "expected", "last_run", "last_result"),
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
        }
        delete_order = (
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
        insert_order = (
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
        )
        with self.transaction():
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

    def verify_audit(self) -> dict[str, Any]:
        rows = [
            dict(row)
            for row in self._connection.execute(
                "SELECT seq, at, actor, action, detail, prev_hash, event_hash, hash_version FROM events ORDER BY seq"
            )
        ]
        head = self._connection.execute("SELECT head_seq, head_hash FROM audit_head WHERE singleton = 1").fetchone()
        if head is None:
            raise AuditIntegrityError("audit integrity: audit head is missing")
        return verify_event_chain(
            rows,
            identity=self.project_identity,
            head_seq=int(head["head_seq"]),
            head_hash=head["head_hash"],
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
                    raise SchemaMismatchError(
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
                    raise SchemaMismatchError(f"schema migration mismatch: expected {expected_prefix}, got {applied}")
            current = applied[-1][0] if applied else 0
            for version, name, checksum in expected:
                if version <= current:
                    continue
                statements = [
                    statement.strip() for statement in _migration_sql(version).split(";") if statement.strip()
                ]
                for statement in statements:
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
            raise SchemaMismatchError(f"schema migration mismatch: expected {expected_records}, got {actual}")

    def _bind_identity(self) -> None:
        row = self._connection.execute(
            "SELECT project_id, repository FROM project_identity WHERE singleton = 1"
        ).fetchone()
        if row is None:
            with self._write_transaction():
                self._connection.execute(
                    "INSERT INTO project_identity(singleton, project_id, repository) VALUES (1, ?, ?)",
                    (self._config.identity.project_id, self._config.identity.repository),
                )
            return
        self._check_identity()

    def _upgrade_audit_history(self) -> None:
        rows = [
            dict(row)
            for row in self._connection.execute(
                "SELECT seq, at, actor, action, detail, hash_version FROM events ORDER BY seq"
            )
        ]
        if not rows or all(int(row["hash_version"]) == AUDIT_HASH_VERSION for row in rows):
            return
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
        stored = self.project_identity
        if stored != self._config.identity:
            raise ProjectIdentityMismatchError(
                "project identity mismatch: "
                f"database={stored.project_id}@{stored.repository}, "
                f"requested={self._config.identity.project_id}@{self._config.identity.repository}"
            )
