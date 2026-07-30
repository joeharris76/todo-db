from __future__ import annotations

import json
import hashlib
from importlib import resources
import sqlite3
from pathlib import Path

import pytest

from todo_db.database import SCHEMA_VERSION


def _open_database(path: Path):
    from todo_db import DatabaseConfig, ProjectIdentity, TodoDatabase

    return TodoDatabase.open(
        DatabaseConfig(
            path=path,
            identity=ProjectIdentity(project_id="project-test", repository="https://example.test/project"),
        )
    )


def test_audit_chain_includes_sequence_and_verifies(tmp_path: Path) -> None:
    db = _open_database(tmp_path / "todo.sqlite")
    db.record_event(actor="test", action="first", detail={"value": 1})
    db.record_event(actor="test", action="second", detail={"value": 2})

    exported = db.export()
    db.verify_audit()

    assert exported["integrity"]["algorithm"] == "sha256-chain-v2"
    assert exported["integrity"]["event_count"] == 2
    assert exported["events"][0]["seq"] == 1
    assert exported["events"][1]["prev_hash"] == exported["events"][0]["event_hash"]
    db.close()


def test_audit_verification_rejects_tampered_event_detail(tmp_path: Path) -> None:
    from todo_db.errors import AuditIntegrityError

    path = tmp_path / "todo.sqlite"
    db = _open_database(path)
    db.record_event(actor="test", action="probe", detail={"value": 7})
    db.close()

    raw = sqlite3.connect(path)
    raw.execute("UPDATE events SET detail = ? WHERE seq = 1", (json.dumps({"value": 8}),))
    raw.commit()
    raw.close()

    with pytest.raises(AuditIntegrityError, match="audit integrity"):
        _open_database(path)


def test_schema_v1_event_history_is_migrated_to_hash_chain_v2(tmp_path: Path) -> None:
    from todo_db import DatabaseConfig, ProjectIdentity, TodoDatabase

    path = tmp_path / "todo.sqlite"
    migration = resources.files("todo_db.migrations").joinpath("001_initial.sql").read_text(encoding="utf-8")
    checksum = hashlib.sha256(migration.encode("utf-8")).hexdigest()
    raw = sqlite3.connect(path)
    raw.executescript(migration)
    raw.execute(
        "INSERT INTO schema_migrations(version, name, checksum, applied_at, tool_version) VALUES (1, 'initial', ?, '2026-01-01T00:00:00Z', '0.1.0')",
        (checksum,),
    )
    raw.execute(
        "INSERT INTO project_identity(singleton, project_id, repository) VALUES (1, 'project-test', 'https://example.test/project')"
    )
    raw.execute(
        "INSERT INTO events(seq, at, actor, action, detail, prev_hash, event_hash) VALUES (1, '2026-01-01T00:00:00Z', 'legacy', 'probe', '{\"value\":1}', NULL, 'legacy-hash')"
    )
    raw.commit()
    raw.close()

    db = TodoDatabase.open(
        DatabaseConfig(
            path=path,
            identity=ProjectIdentity(project_id="project-test", repository="https://example.test/project"),
        )
    )
    assert db.schema_version == SCHEMA_VERSION
    assert db.verify_audit()["event_count"] == 1
    db.close()


def test_signed_export_manifest_round_trip_and_tamper_detection(tmp_path: Path) -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from todo_db.audit import sign_export, verify_signed_export

    db = _open_database(tmp_path / "todo.sqlite")
    db.record_event(actor="test", action="probe", detail={"value": 7})
    exported = db.export()
    private_key = Ed25519PrivateKey.generate()

    signed = sign_export(exported, private_key)
    verified = verify_signed_export(exported, signed, private_key.public_key())

    assert verified["event_head"] == exported["integrity"]["head_hash"]
    exported["metadata"]["tampered"] = "yes"
    with pytest.raises(Exception, match="signature|manifest"):
        verify_signed_export(exported, signed, private_key.public_key())
    db.close()
