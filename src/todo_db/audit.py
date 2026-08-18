"""Portable audit-chain and export-manifest integrity primitives."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any, Mapping

from .errors import AuditIntegrityError
from .models import ProjectIdentity


AUDIT_HASH_VERSION = 2
AUDIT_HASH_ALGORITHM = "sha256-chain-v2"
SIGNATURE_ALGORITHM = "ed25519"


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def event_hash(
    *,
    identity: ProjectIdentity,
    seq: int,
    at: str,
    actor: str,
    action: str,
    detail: Any,
    prev_hash: str | None,
) -> str:
    body = {
        "algorithm": AUDIT_HASH_ALGORITHM,
        "hash_version": AUDIT_HASH_VERSION,
        "project": {"project_id": identity.project_id, "repository": identity.repository},
        "seq": seq,
        "at": at,
        "actor": actor,
        "action": action,
        "detail": detail,
        "prev_hash": prev_hash,
    }
    return hashlib.sha256(canonical_bytes(body)).hexdigest()


def verify_event_chain(
    rows: list[Mapping[str, Any]],
    *,
    identity: ProjectIdentity,
    head_seq: int,
    head_hash: str | None,
) -> dict[str, Any]:
    previous_hash: str | None = None
    expected_seq = 1
    for row in rows:
        seq = int(row["seq"])
        if seq != expected_seq:
            raise AuditIntegrityError(f"audit integrity: expected sequence {expected_seq}, got {seq}")
        if int(row.get("hash_version", AUDIT_HASH_VERSION)) != AUDIT_HASH_VERSION:
            raise AuditIntegrityError(f"audit integrity: unsupported event hash version at seq {seq}")
        detail = row["detail"]
        if isinstance(detail, str):
            try:
                detail = json.loads(detail)
            except json.JSONDecodeError as exc:
                raise AuditIntegrityError(f"audit integrity: invalid detail JSON at seq {seq}") from exc
        if row["prev_hash"] != previous_hash:
            raise AuditIntegrityError(f"audit integrity: broken predecessor link at seq {seq}")
        expected_hash = event_hash(
            identity=identity,
            seq=seq,
            at=str(row["at"]),
            actor=str(row["actor"]),
            action=str(row["action"]),
            detail=detail,
            prev_hash=previous_hash,
        )
        if row["event_hash"] != expected_hash:
            raise AuditIntegrityError(f"audit integrity: event hash mismatch at seq {seq}")
        previous_hash = expected_hash
        expected_seq += 1

    actual_seq = expected_seq - 1
    if head_seq != actual_seq or head_hash != previous_hash:
        raise AuditIntegrityError(
            f"audit integrity: head mismatch; expected ({actual_seq}, {previous_hash}), got ({head_seq}, {head_hash})"
        )
    return {
        "algorithm": AUDIT_HASH_ALGORITHM,
        "event_count": actual_seq,
        "head_seq": actual_seq,
        "head_hash": previous_hash,
    }


def verify_audit_head(
    head_seq: int,
    head_hash: str | None,
    last_event: Mapping[str, Any] | None,
    event_count: int | None = None,
) -> dict[str, Any]:
    if last_event is None or (event_count is not None and event_count == 0):
        if head_seq != 0 or head_hash is not None:
            raise AuditIntegrityError(
                f"audit integrity: head mismatch; expected (0, None), got ({head_seq}, {head_hash})"
            )
        return {
            "algorithm": AUDIT_HASH_ALGORITHM,
            "event_count": 0,
            "head_seq": 0,
            "head_hash": None,
        }
    actual_seq = int(last_event["seq"])
    actual_hash = str(last_event["event_hash"])
    count = event_count if event_count is not None else actual_seq
    if head_seq != actual_seq or head_hash != actual_hash or head_seq != count:
        raise AuditIntegrityError(
            f"audit integrity: head mismatch; expected ({count}, {actual_hash}), got ({head_seq}, {head_hash})"
        )
    return {
        "algorithm": AUDIT_HASH_ALGORITHM,
        "event_count": count,
        "head_seq": actual_seq,
        "head_hash": actual_hash,
    }


def _ed25519():
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
    except ImportError as exc:  # pragma: no cover - exercised in minimal installs
        raise AuditIntegrityError("signed exports require the `todo-db[audit]` extra") from exc
    return Ed25519PrivateKey, Ed25519PublicKey


def _public_key_bytes(public_key: Any) -> bytes:
    from cryptography.hazmat.primitives import serialization

    return public_key.public_bytes(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)


def _export_manifest(export: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "algorithm": SIGNATURE_ALGORITHM,
        "format_version": export["format_version"],
        "project": export["project"],
        "schema_version": export["schema"]["version"],
        "event_count": export["integrity"]["event_count"],
        "event_head": export["integrity"]["head_hash"],
        "export_sha256": hashlib.sha256(canonical_bytes(export)).hexdigest(),
    }


def sign_export(export: Mapping[str, Any], private_key: Any) -> dict[str, Any]:
    """Sign an export manifest; the export itself remains JSON-serializable."""

    _ed25519()
    manifest = _export_manifest(export)
    public_key = private_key.public_key()
    return {
        "algorithm": SIGNATURE_ALGORITHM,
        "public_key": base64.b64encode(_public_key_bytes(public_key)).decode("ascii"),
        "manifest": manifest,
        "signature": base64.b64encode(private_key.sign(canonical_bytes(manifest))).decode("ascii"),
    }


def verify_signed_export(
    export: Mapping[str, Any], signed: Mapping[str, Any], public_key: Any | None = None
) -> dict[str, Any]:
    """Verify a signed manifest against the exact export payload."""

    _, Ed25519PublicKey = _ed25519()
    manifest = _export_manifest(export)
    if signed.get("algorithm") != SIGNATURE_ALGORITHM or signed.get("manifest") != manifest:
        raise AuditIntegrityError("export manifest does not match the export payload")
    try:
        key = public_key or Ed25519PublicKey.from_public_bytes(base64.b64decode(signed["public_key"]))
        key.verify(base64.b64decode(signed["signature"]), canonical_bytes(manifest))
    except Exception as exc:
        raise AuditIntegrityError("export signature verification failed") from exc
    return manifest
