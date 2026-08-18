from __future__ import annotations

import hashlib
from importlib import resources
import json
from pathlib import Path
import sqlite3

import pytest

from todo_db.database import SCHEMA_VERSION
from todo_db.errors import AuditIntegrityError


def _open_database(path: Path):
    from todo_db import DatabaseConfig, ProjectIdentity, TodoDatabase

    return TodoDatabase.open(
        DatabaseConfig(
            path=path,
            identity=ProjectIdentity(project_id="project-test", repository="https://example.test/project"),
        )
    )


def test_audit_chain_verifies_valid_sequence_and_integrity(tmp_path: Path) -> None:
    db = _open_database(tmp_path / "todo.sqlite")
    db.record_event(actor="test", action="first", detail={"value": 1})
    db.record_event(actor="test", action="second", detail={"value": 2})

    exported = db.export()
    result = db.verify_audit()

    assert set(result.keys()) == {"algorithm", "event_count", "head_seq", "head_hash"}
    assert result["algorithm"] == "sha256-chain-v2"
    assert result["event_count"] == 2
    assert result["head_seq"] == 2
    assert result["head_hash"] is not None

    assert exported["integrity"]["algorithm"] == "sha256-chain-v2"
    assert exported["integrity"]["event_count"] == 2
    assert exported["events"][0]["seq"] == 1
    assert exported["events"][1]["prev_hash"] == exported["events"][0]["event_hash"]
    db.close()


def test_audit_verification_rejects_tampered_event_detail(tmp_path: Path) -> None:
    path = tmp_path / "todo.sqlite"
    db = _open_database(path)
    db.record_event(actor="test", action="probe", detail={"value": 7})
    db.close()

    raw = sqlite3.connect(path)
    raw.execute("UPDATE events SET detail = ? WHERE seq = 1", (json.dumps({"value": 8}),))
    raw.commit()
    raw.close()

    db_reopened = _open_database(path)
    with pytest.raises(AuditIntegrityError, match="audit integrity"):
        db_reopened.verify_audit()
    db_reopened.close()


def test_audit_head_check_on_open_detects_head_mismatch_and_missing_head(tmp_path: Path) -> None:
    path = tmp_path / "todo.sqlite"
    db = _open_database(path)
    db.record_event(actor="test", action="first", detail={"value": 1})
    db.close()

    # Tamper with audit_head hash
    raw = sqlite3.connect(path)
    raw.execute("UPDATE audit_head SET head_hash = 'fake-hash' WHERE singleton = 1")
    raw.commit()
    raw.close()

    with pytest.raises(AuditIntegrityError, match="head mismatch"):
        _open_database(path)

    # Delete audit_head - in read-only mode, missing head is detected
    from todo_db import CredentialMode, DatabaseConfig, ProjectIdentity, TodoDatabase

    raw = sqlite3.connect(path)
    raw.execute("DELETE FROM audit_head")
    raw.commit()
    raw.close()

    with pytest.raises(AuditIntegrityError, match="audit head is missing"):
        TodoDatabase.open(
            DatabaseConfig(
                path=path,
                identity=ProjectIdentity(project_id="project-test", repository="https://example.test/project"),
                credential_mode=CredentialMode.READ_ONLY,
            )
        )


def test_audit_open_policy_env_variable_enables_full_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "todo.sqlite"
    db = _open_database(path)
    db.record_event(actor="test", action="first", detail={"value": 1})
    db.record_event(actor="test", action="second", detail={"value": 2})
    db.close()

    # Tamper with event 1 detail without changing audit_head
    raw = sqlite3.connect(path)
    raw.execute("UPDATE events SET detail = ? WHERE seq = 1", (json.dumps({"value": 99}),))
    raw.commit()
    raw.close()

    # Default head policy opens successfully
    monkeypatch.delenv("TODO_DB_AUDIT_OPEN_POLICY", raising=False)
    db_head = _open_database(path)
    db_head.close()

    # Full policy detects mid-chain tampering on open
    monkeypatch.setenv("TODO_DB_AUDIT_OPEN_POLICY", "full")
    with pytest.raises(AuditIntegrityError, match="audit integrity: event hash mismatch"):
        _open_database(path)


def test_audit_chain_verification_detects_all_tamper_classes(tmp_path: Path) -> None:
    from todo_db.audit import verify_event_chain
    from todo_db.models import ProjectIdentity

    identity = ProjectIdentity(project_id="test", repository="repo")

    # 1. Malformed seq (gap)
    rows_gap = [{"seq": 2, "at": "t", "actor": "a", "action": "b", "detail": {}, "prev_hash": None, "event_hash": "h"}]
    with pytest.raises(AuditIntegrityError, match="expected sequence 1, got 2"):
        verify_event_chain(rows_gap, identity=identity, head_seq=2, head_hash="h")

    # 2. Bad hash version
    rows_bad_ver = [
        {
            "seq": 1,
            "at": "t",
            "actor": "a",
            "action": "b",
            "detail": {},
            "prev_hash": None,
            "event_hash": "h",
            "hash_version": 999,
        }
    ]
    with pytest.raises(AuditIntegrityError, match="unsupported event hash version"):
        verify_event_chain(rows_bad_ver, identity=identity, head_seq=1, head_hash="h")

    # 3. Broken predecessor link
    rows_broken_pred = [
        {"seq": 1, "at": "t", "actor": "a", "action": "b", "detail": {}, "prev_hash": "unexpected", "event_hash": "h"}
    ]
    with pytest.raises(AuditIntegrityError, match="broken predecessor link"):
        verify_event_chain(rows_broken_pred, identity=identity, head_seq=1, head_hash="h")


def test_audit_history_upgrades_legacy_hash_version_transparently(tmp_path: Path) -> None:
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


def test_signed_export_manifest_authenticates_valid_export_and_detects_tamper(tmp_path: Path) -> None:
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


def test_verify_audit_returns_exact_four_key_shape_and_handles_empty_history(tmp_path: Path) -> None:
    db = _open_database(tmp_path / "empty.sqlite")
    result = db.verify_audit()

    assert set(result.keys()) == {"algorithm", "event_count", "head_seq", "head_hash"}
    assert result["algorithm"] == "sha256-chain-v2"
    assert result["event_count"] == 0
    assert result["head_seq"] == 0
    assert result["head_hash"] is None
    db.close()


def test_verify_audit_rejects_missing_audit_head(tmp_path: Path) -> None:
    path = tmp_path / "missing_head.sqlite"
    db = _open_database(path)
    db.close()

    raw = sqlite3.connect(path)
    raw.execute("DELETE FROM audit_head")
    raw.commit()
    raw.close()

    raw_conn = sqlite3.connect(path)
    raw_conn.row_factory = sqlite3.Row
    from todo_db import DatabaseConfig, ProjectIdentity, TodoDatabase

    unverified_db = TodoDatabase(
        raw_conn,
        DatabaseConfig(
            path=path,
            identity=ProjectIdentity(project_id="project-test", repository="https://example.test/project"),
        ),
    )
    with pytest.raises(AuditIntegrityError, match="audit head is missing"):
        unverified_db.verify_audit()
    raw_conn.close()
