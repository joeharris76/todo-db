"""Canonical JSON task state: index ownership, detail files, and pure operations.

State layout inside a checked-out state revision::

    index.json
    items/<id>.json

``index.json`` is ``{"schema_version": 3, "items": {id: entry}, "batches": {}}`` where each
entry owns exactly ``title``, ``priority``, ``status``, ``claim``, and optional
``needs`` IDs. Detail files carry descriptions, acceptance criteria, links,
and retained task context. Detail files must not contain independently
editable copies of index-owned fields.

Lifecycle: ``open``, ``active``, ``blocked``, ``done``, ``dropped``.
Priorities preserve the historical bands: ``critical``, ``high``,
``medium-high``, ``medium``, ``low``.

A claim is ``{"worker": str, "expires_at": UTC ISO Z, "generation": uuid4hex,
"renewals": int}``. The counter starts at 0 on take and increments on every
renewal or restart re-adoption, so a renewal is always a state change and
the renewal path stays observable.
Ownership plus generation checks protect renew, release, and finish from
stale writers. Claims are cooperative concurrency controls, not
authentication against a malicious repository writer: every writer must run
the publication protocol in :mod:`todo_db.git_backend`, and branch
rewrites/deletion invalidate its assumptions. Clock assumption: writers'
clocks are roughly synchronized (minutes); the default TTL is 24h so small
skew is harmless.

This module is pure (no subprocess, no network): it operates on in-memory
``(index, details)`` snapshots plus atomic local-directory IO. Git
publication lives in :mod:`todo_db.git_backend`.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import hashlib
import fnmatch
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .errors import (
    E_ACTIVE_CLAIMS,
    E_CLAIM_STALE,
    E_CONFLICT,
    E_FINAL_EVIDENCE,
    E_SCHEMA,
    E_STATE,
    TodoError,
)

SCHEMA_VERSION = 3
SUPPORTED_SCHEMA_VERSIONS = (1, 2, SCHEMA_VERSION)
STATUSES = ("open", "active", "blocked", "done", "dropped")
PRIORITIES = ("critical", "high", "medium-high", "medium", "low")
PRIORITY_RANK = {name: rank for rank, name in enumerate(PRIORITIES)}
TERMINAL_STATUSES = ("done", "dropped")

ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
# Keep the slug safe for the item filename while accepting IDs from the
# legacy tracker, whose longest existing IDs are 82 characters.
MAX_ID_LEN = 128
MAX_TITLE_LEN = 200
MAX_WORKER_LEN = 128
DEFAULT_TTL_HOURS = 24.0
MAX_TTL_HOURS = 72.0

INDEX_FILENAME = "index.json"
ITEMS_DIRNAME = "items"

#: Fields owned by the index. Detail files containing any of these are
#: rejected so there is never an ambiguous second copy to merge.
INDEX_OWNED_FIELDS = frozenset({"title", "priority", "status", "claim", "needs"})

# Prepared receipts are detail-owned.  They deliberately do not add a second
# status: an item is still open after its claim is handed back, while the
# receipt records the work that is available to an explicitly registered
# same-batch implementation edge.
PREPARED_RECEIPT_SCHEMA = "prepared_work_v1"
REVISION_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
SCOPE_HASH_RE = re.compile(r"^[0-9a-f]{64}$")

#: Status moves allowed to plain ``update``. Entering ``active`` needs a claim
#: (take); leaving ``active`` releases or closes it (release/finish);
#: terminal states go through finish/drop. Anything else is rejected so the
#: claim protocol cannot be bypassed.
UPDATE_STATUSES = frozenset({"open", "blocked"})


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(raw: str) -> datetime:
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, AttributeError) as exc:
        raise TodoError(f"invalid timestamp {raw!r}: expected UTC ISO-8601 Z", code=E_STATE) from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def new_generation() -> str:
    return uuid4().hex


def claim_is_live(claim: dict[str, Any] | None, now: datetime | None = None) -> bool:
    if not claim:
        return False
    try:
        expires = parse_ts(str(claim.get("expires_at", "")))
    except TodoError:
        return False
    moment = now or datetime.now(timezone.utc)
    return expires > moment


def validate_id(item_id: str) -> None:
    if not isinstance(item_id, str) or not item_id:
        raise TodoError(f"invalid task ID {item_id!r}: must be a non-empty string", code=E_STATE)
    if len(item_id) > MAX_ID_LEN or not ID_RE.fullmatch(item_id):
        raise TodoError(
            f"invalid task ID {item_id!r}: use 1-{MAX_ID_LEN} chars of "
            "[a-z0-9-], starting and ending alphanumeric",
            code=E_STATE,
        )


def validate_worker(worker: str) -> str:
    if not isinstance(worker, str) or not worker.strip():
        raise TodoError("worker identity must be a non-empty string", code=E_STATE)
    cleaned = worker.strip()
    if len(cleaned) > MAX_WORKER_LEN:
        raise TodoError(f"worker identity exceeds {MAX_WORKER_LEN} chars", code=E_STATE)
    # Worker identities land in commit trailers, where a newline would forge
    # protocol lines. Single line, no control characters.
    if any(ord(char) < 32 for char in cleaned):
        raise TodoError("worker identity must be a single line without control characters", code=E_STATE)
    return cleaned


def empty_index() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "items": {}, "batches": {}}


#: The exact index-entry schema. Anything else is rejected so detail files
#: can never shadow index-owned fields and index entries can never shadow
#: detail-owned fields: authority is exact on both sides.
ENTRY_KEYS = frozenset({"title", "priority", "status", "claim", "needs"})


def _check_entry_shape(item_id: str, entry: Any) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise TodoError(f"index entry {item_id!r} must be an object", code=E_STATE)
    unknown = set(entry) - ENTRY_KEYS
    if unknown:
        raise TodoError(
            f"index entry {item_id!r} has unexpected keys: {', '.join(sorted(unknown))}",
            code=E_STATE,
        )
    for key in ("title", "priority", "status"):
        if key not in entry:
            raise TodoError(f"index entry {item_id!r} is missing {key!r}", code=E_STATE)
    title = entry["title"]
    if not isinstance(title, str) or not title.strip() or len(title) > MAX_TITLE_LEN:
        raise TodoError(
            f"index entry {item_id!r} has an invalid title: 1-{MAX_TITLE_LEN} chars required",
            code=E_STATE,
        )
    if entry["priority"] not in PRIORITIES:
        raise TodoError(f"index entry {item_id!r} has unknown priority {entry['priority']!r}", code=E_STATE)
    if entry["status"] not in STATUSES:
        raise TodoError(f"index entry {item_id!r} has unknown status {entry['status']!r}", code=E_STATE)
    needs = entry.get("needs", [])
    if not isinstance(needs, list) or any(not isinstance(dep, str) for dep in needs):
        raise TodoError(f"index entry {item_id!r} has invalid needs: expected a list of IDs", code=E_STATE)
    claim = entry.get("claim")
    if claim is not None:
        if not isinstance(claim, dict):
            raise TodoError(f"index entry {item_id!r} has an invalid claim", code=E_STATE)
        for key in ("worker", "expires_at", "generation"):
            if key not in claim:
                raise TodoError(f"index entry {item_id!r} claim is missing {key!r}", code=E_STATE)
        validate_worker(str(claim["worker"]))
        parse_ts(str(claim["expires_at"]))
        generation = str(claim["generation"])
        if not re.fullmatch(r"[0-9a-f]{32}", generation):
            raise TodoError(f"index entry {item_id!r} claim has an invalid generation", code=E_STATE)
        renewals = claim.get("renewals", 0)
        if not isinstance(renewals, int) or isinstance(renewals, bool) or renewals < 0:
            raise TodoError(f"index entry {item_id!r} claim has an invalid renewals counter", code=E_STATE)
    return entry


def _validate_revision(value: Any, label: str) -> str:
    revision = str(value) if isinstance(value, str) else ""
    if not REVISION_RE.fullmatch(revision):
        raise TodoError(f"{label} must be a full lowercase Git revision", code=E_STATE)
    return revision


def scope_digest(scope: dict[str, list[str]]) -> str:
    """Return the canonical digest for an ordered member scope manifest."""
    blob = json.dumps(scope, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _validate_batch_record(batch_id: str, batch: Any) -> dict[str, Any]:
    if not isinstance(batch, dict):
        raise TodoError(f"batch {batch_id!r} must be an object", code=E_STATE)
    required = (
        "project_id", "repository", "owner", "owner_generation", "integration_branch",
        "integration_worktree", "start_head", "members", "scope", "scope_hash",
        "delivery_boundary", "terminal_outcome",
    )
    for key in required:
        if key not in batch:
            raise TodoError(f"batch {batch_id!r} is missing {key!r}", code=E_STATE)
    validate_id(batch_id)
    for key in ("project_id", "repository", "owner", "integration_branch", "delivery_boundary", "terminal_outcome"):
        if not isinstance(batch[key], str) or not batch[key].strip() or any(ord(ch) < 32 for ch in batch[key]):
            raise TodoError(f"batch {batch_id!r} has invalid {key}", code=E_STATE)
    generation = batch["owner_generation"]
    if not isinstance(generation, str) or not re.fullmatch(r"[0-9a-f]{32}", generation):
        raise TodoError(f"batch {batch_id!r} has an invalid owner generation", code=E_STATE)
    worktree = batch["integration_worktree"]
    if not isinstance(worktree, str) or not os.path.isabs(worktree):
        raise TodoError(f"batch {batch_id!r} requires an absolute integration_worktree", code=E_STATE)
    _validate_revision(batch["start_head"], "batch start_head")
    members = batch["members"]
    if not isinstance(members, list) or not members or any(not isinstance(member, str) for member in members):
        raise TodoError(f"batch {batch_id!r} requires ordered member IDs", code=E_STATE)
    if len(set(members)) != len(members):
        raise TodoError(f"batch {batch_id!r} has duplicate members", code=E_STATE)
    for member in members:
        validate_id(member)
    scope = batch["scope"]
    if not isinstance(scope, dict) or set(scope) != set(members):
        raise TodoError(f"batch {batch_id!r} scope must cover exactly its members", code=E_STATE)
    for member in members:
        paths = scope[member]
        if not isinstance(paths, list) or not paths or any(not isinstance(path, str) or not path for path in paths):
            raise TodoError(f"batch {batch_id!r} has invalid scope for {member!r}", code=E_STATE)
    digest = batch["scope_hash"]
    if not isinstance(digest, str) or not SCOPE_HASH_RE.fullmatch(digest) or digest != scope_digest(scope):
        raise TodoError(f"batch {batch_id!r} scope_hash does not match its frozen scope", code=E_STATE)
    if "integration_head" in batch:
        _validate_revision(batch["integration_head"], "batch integration_head")
    lifecycle = batch.get("lifecycle", "active")
    if lifecycle not in ("active", "aborted", "completed"):
        raise TodoError(f"batch {batch_id!r} has invalid lifecycle", code=E_STATE)
    accepted_members = batch.get("accepted_members", {})
    if not isinstance(accepted_members, dict) or set(accepted_members) - set(members):
        raise TodoError(f"batch {batch_id!r} has invalid accepted member heads", code=E_STATE)
    for member, head in accepted_members.items():
        _validate_revision(head, f"batch accepted head for {member!r}")
    integrated_members = batch.get("integrated_members", {})
    if not isinstance(integrated_members, dict) or set(integrated_members) - set(members):
        raise TodoError(f"batch {batch_id!r} has invalid integrated member evidence", code=E_STATE)
    for member, evidence in integrated_members.items():
        if not isinstance(evidence, dict):
            raise TodoError(f"batch {batch_id!r} has invalid integration evidence for {member!r}", code=E_STATE)
        _validate_revision(evidence.get("accepted_head"), f"integrated accepted head for {member!r}")
        _validate_revision(evidence.get("integration_head"), f"integrated head for {member!r}")
    final_pr = batch.get("final_pr")
    if final_pr is not None:
        if not isinstance(final_pr, dict):
            raise TodoError(f"batch {batch_id!r} final_pr must be an object", code=E_STATE)
        if not isinstance(final_pr.get("number"), int) or final_pr["number"] <= 0:
            raise TodoError(f"batch {batch_id!r} final_pr has an invalid number", code=E_STATE)
        if not isinstance(final_pr.get("node_id"), str) or not final_pr["node_id"].strip():
            raise TodoError(f"batch {batch_id!r} final_pr has no node_id", code=E_STATE)
        _validate_revision(final_pr.get("head"), "final_pr head")
    return batch


def _validate_prepared_receipt(item_id: str, prepared: Any) -> dict[str, Any]:
    if not isinstance(prepared, dict):
        raise TodoError(f"prepared receipt for {item_id!r} must be an object", code=E_STATE)
    if prepared.get("schema") != PREPARED_RECEIPT_SCHEMA:
        raise TodoError(f"prepared receipt for {item_id!r} has an unknown schema", code=E_SCHEMA)
    for key in (
        "batch_id", "member_id", "owner_generation", "source_worktree", "source_revision",
        "source_base", "accepted_head", "integration_head", "scope_hash", "changed_files", "verification",
    ):
        if key not in prepared:
            raise TodoError(f"prepared receipt for {item_id!r} is missing {key!r}", code=E_STATE)
    validate_id(prepared["batch_id"])
    validate_id(prepared["member_id"])
    generation = prepared["owner_generation"]
    if not isinstance(generation, str) or not re.fullmatch(r"[0-9a-f]{32}", generation):
        raise TodoError(f"prepared receipt for {item_id!r} has an invalid owner generation", code=E_STATE)
    worktree = prepared["source_worktree"]
    if not isinstance(worktree, str) or not worktree or not os.path.isabs(worktree):
        raise TodoError(f"prepared receipt for {item_id!r} requires an absolute source_worktree", code=E_STATE)
    revision = _validate_revision(prepared["source_revision"], "source_revision")
    _validate_revision(prepared["source_base"], "source_base")
    accepted_head = _validate_revision(prepared["accepted_head"], "accepted_head")
    _validate_revision(prepared["integration_head"], "integration_head")
    if accepted_head != revision:
        raise TodoError(f"prepared receipt for {item_id!r} does not bind the accepted worker revision", code=E_STATE)
    scope_hash = prepared["scope_hash"]
    if not isinstance(scope_hash, str) or not SCOPE_HASH_RE.fullmatch(scope_hash):
        raise TodoError(f"prepared receipt for {item_id!r} has an invalid scope_hash", code=E_STATE)
    changed_files = prepared["changed_files"]
    if not isinstance(changed_files, list) or any(not isinstance(path, str) or not path for path in changed_files):
        raise TodoError(f"prepared receipt for {item_id!r} has invalid changed files", code=E_STATE)
    if changed_files != sorted(set(changed_files)):
        raise TodoError(f"prepared receipt for {item_id!r} changed files are not canonical", code=E_STATE)
    verification = prepared["verification"]
    if not isinstance(verification, dict):
        raise TodoError(f"prepared receipt for {item_id!r} has invalid verification evidence", code=E_STATE)
    if verification.get("status") != "passed":
        raise TodoError(f"prepared receipt for {item_id!r} requires passed verification", code=E_STATE)
    if verification.get("revision") != revision:
        raise TodoError(f"prepared receipt for {item_id!r} verification drifted from source_revision", code=E_STATE)
    if verification.get("clean") is not True:
        raise TodoError(f"prepared receipt for {item_id!r} requires a clean source checkout", code=E_STATE)
    suite = verification.get("suite")
    command = verification.get("command")
    if not isinstance(suite, str) or not suite.strip():
        raise TodoError(f"prepared receipt for {item_id!r} requires a bounded verification suite", code=E_STATE)
    if not isinstance(command, list) or not command or any(not isinstance(part, str) or not part for part in command):
        raise TodoError(f"prepared receipt for {item_id!r} requires a non-empty verification command", code=E_STATE)
    return prepared


def _validate_final_evidence(item_id: str, evidence: Any) -> dict[str, Any]:
    if not isinstance(evidence, dict) or evidence.get("status") != "passed":
        raise TodoError(f"prepared task {item_id!r} requires passed final-tree evidence", code=E_FINAL_EVIDENCE)
    for key in (
        "batch_id", "project_id", "repository", "tree_worktree", "tree_revision", "integration_branch",
        "integration_head", "scope_hash", "suite",
    ):
        if not isinstance(evidence.get(key), str) or not evidence[key].strip():
            raise TodoError(f"final evidence for {item_id!r} requires {key}", code=E_FINAL_EVIDENCE)
    if not os.path.isabs(evidence["tree_worktree"]):
        raise TodoError(f"final evidence for {item_id!r} requires an absolute tree_worktree", code=E_FINAL_EVIDENCE)
    _validate_revision(evidence["tree_revision"], "tree_revision")
    _validate_revision(evidence["integration_head"], "integration_head")
    if evidence["tree_revision"] != evidence["integration_head"]:
        raise TodoError(f"final evidence for {item_id!r} has inconsistent tree/head", code=E_FINAL_EVIDENCE)
    if not isinstance(evidence["member_heads"], dict) or not isinstance(evidence["member_ranges"], dict):
        raise TodoError(f"final evidence for {item_id!r} has invalid member mappings", code=E_FINAL_EVIDENCE)
    if not isinstance(evidence["changed_files"], list) or any(not isinstance(path, str) for path in evidence["changed_files"]):
        raise TodoError(f"final evidence for {item_id!r} has invalid changed_files", code=E_FINAL_EVIDENCE)
    if not isinstance(evidence["final_pr"], dict):
        raise TodoError(f"final evidence for {item_id!r} has no final PR identity", code=E_FINAL_EVIDENCE)
    if not isinstance(evidence["final_pr"].get("number"), int) or not isinstance(evidence["final_pr"].get("node_id"), str):
        raise TodoError(f"final evidence for {item_id!r} has an invalid final PR identity", code=E_FINAL_EVIDENCE)
    _validate_revision(evidence["final_pr"].get("head"), "final_pr head")
    if evidence.get("clean") is not True:
        raise TodoError(f"final evidence for {item_id!r} requires a clean final tree", code=E_FINAL_EVIDENCE)
    return evidence


def _validate_batch_final_evidence(snapshot: Snapshot, item_id: str, evidence: dict[str, Any]) -> dict[str, Any]:
    """Validate final evidence against every prepared member and its registry."""
    detail = snapshot.details[item_id]
    batch_meta = detail.get("batch") or {}
    batch_id = batch_meta.get("batch_id")
    batch = snapshot.index.get("batches", {}).get(batch_id)
    if batch is None or evidence.get("batch_id") != batch_id:
        raise TodoError(f"final evidence for {item_id!r} names a foreign batch", code=E_FINAL_EVIDENCE)
    if evidence.get("integration_branch") != batch["integration_branch"]:
        raise TodoError(f"final evidence for {item_id!r} names a foreign integration branch", code=E_FINAL_EVIDENCE)
    if evidence.get("scope_hash") != batch["scope_hash"]:
        raise TodoError(f"final evidence for {item_id!r} has a foreign scope", code=E_FINAL_EVIDENCE)
    if evidence.get("project_id") != batch["project_id"] or evidence.get("repository") != batch["repository"]:
        raise TodoError(f"final evidence for {item_id!r} names a foreign repository", code=E_FINAL_EVIDENCE)
    if evidence.get("integration_head") != batch.get("integration_head"):
        raise TodoError(f"final evidence for {item_id!r} is not at the registered integration head", code=E_FINAL_EVIDENCE)
    final_pr = batch.get("final_pr")
    if final_pr is None or evidence.get("final_pr") != final_pr:
        raise TodoError(f"final evidence for {item_id!r} is not bound to the declared final PR", code=E_FINAL_EVIDENCE)
    if final_pr["head"] != evidence["integration_head"]:
        raise TodoError(f"final evidence for {item_id!r} is not at the final PR head", code=E_FINAL_EVIDENCE)
    members = batch["members"]
    heads = evidence["member_heads"]
    ranges = evidence["member_ranges"]
    if set(heads) != set(members) or set(ranges) != set(members):
        raise TodoError(f"final evidence for {item_id!r} does not map every declared member", code=E_FINAL_EVIDENCE)
    allowed: list[str] = []
    for member in members:
        member_detail = snapshot.details.get(member, {})
        receipt = member_detail.get("prepared")
        if not isinstance(receipt, dict) or receipt.get("batch_id") != batch_id:
            raise TodoError(f"member {member!r} lacks a valid prepared receipt", code=E_FINAL_EVIDENCE)
        if heads[member] != receipt["accepted_head"]:
            raise TodoError(f"member {member!r} final head does not match its prepared head", code=E_FINAL_EVIDENCE)
        member_range = ranges[member]
        if not isinstance(member_range, dict) or member_range.get("base") != receipt["source_base"] or member_range.get("head") != heads[member]:
            raise TodoError(f"member {member!r} range is not bound to its prepared receipt", code=E_FINAL_EVIDENCE)
        allowed.extend(batch["scope"][member])
    for path in evidence["changed_files"]:
        if not any(fnmatch.fnmatch(path, pattern) for pattern in allowed):
            raise TodoError(f"final evidence includes unauthorized file {path!r}", code=E_FINAL_EVIDENCE)
    # The union check is intentionally independent of member_ranges: a caller
    # cannot hide an out-of-scope file by assigning it to a different member.
    union = [pattern for member in members for pattern in batch["scope"][member]]
    if any(not any(fnmatch.fnmatch(path, pattern) for pattern in union) for path in evidence["changed_files"]):
        raise TodoError(f"final evidence for {item_id!r} exceeds the frozen batch scope", code=E_FINAL_EVIDENCE)
    return evidence


def _validate_batch_detail(item_id: str, detail: dict[str, Any], needs: list[str]) -> None:
    batch = detail.get("batch")
    if batch is None:
        return
    if not isinstance(batch, dict):
        raise TodoError(f"batch metadata for {item_id!r} must be an object", code=E_STATE)
    for key in ("batch_id", "member_id", "implementation_dependencies"):
        if key not in batch:
            raise TodoError(f"batch metadata for {item_id!r} is missing {key!r}", code=E_STATE)
    validate_id(batch["batch_id"])
    validate_id(batch["member_id"])
    deps = batch["implementation_dependencies"]
    if not isinstance(deps, list) or any(not isinstance(dep, str) for dep in deps):
        raise TodoError(f"batch metadata for {item_id!r} has invalid implementation dependencies", code=E_STATE)
    if len(set(deps)) != len(deps) or any(dep not in needs for dep in deps):
        raise TodoError(f"batch metadata for {item_id!r} must name unique task dependencies", code=E_STATE)
    if deps and batch["member_id"] in deps:
        raise TodoError(f"batch metadata for {item_id!r} cannot depend on itself", code=E_STATE)


def _check_cycles(items: dict[str, Any]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str, trail: list[str]) -> None:
        if node in visited:
            return
        if node in visiting:
            cycle = " -> ".join([*trail, node])
            raise TodoError(f"dependency cycle detected: {cycle}", code=E_STATE)
        visiting.add(node)
        for dep in items[node].get("needs", []):
            visit(dep, [*trail, node])
        visiting.discard(node)
        visited.add(node)

    for item_id in items:
        visit(item_id, [])


def validate_snapshot(index: Any, details: dict[str, Any]) -> dict[str, Any]:
    """Validate index/detail correspondence. Returns the index on success."""
    if not isinstance(index, dict):
        raise TodoError("index.json must contain a JSON object", code=E_STATE)
    version = index.get("schema_version")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise TodoError(
            f"unsupported index schema version {version!r}: this package reads {SUPPORTED_SCHEMA_VERSIONS}",
            code=E_SCHEMA,
        )
    items = index.get("items")
    if not isinstance(items, dict):
        raise TodoError("index.json items must be an object keyed by ID", code=E_STATE)
    batches = index.get("batches", {})
    if version >= 3:
        if not isinstance(batches, dict):
            raise TodoError("index.json batches must be an object keyed by batch ID", code=E_STATE)
        for batch_id, batch in batches.items():
            _validate_batch_record(batch_id, batch)
    elif batches not in ({}, None):
        raise TodoError("batch registry requires index schema version 3", code=E_SCHEMA)
    for item_id, entry in items.items():
        validate_id(item_id)
        _check_entry_shape(item_id, entry)
    for item_id, entry in items.items():
        for dep in entry.get("needs", []):
            validate_id(dep)
            if dep == item_id:
                raise TodoError(f"task {item_id!r} cannot depend on itself", code=E_STATE)
            if dep not in items:
                raise TodoError(f"task {item_id!r} needs unknown task {dep!r}", code=E_STATE)
        if entry["status"] in TERMINAL_STATUSES and entry.get("claim") is not None:
            raise TodoError(f"task {item_id!r} is {entry['status']} but still carries a claim", code=E_STATE)
        if entry["status"] in ("open", "blocked") and entry.get("claim") is not None:
            raise TodoError(
                f"task {item_id!r} is {entry['status']} but carries a claim; "
                "only active tasks hold claims",
                code=E_STATE,
            )
    _check_cycles(items)
    if not isinstance(details, dict):
        raise TodoError("details must be a mapping of ID to detail object", code=E_STATE)
    missing = [item_id for item_id in items if item_id not in details]
    if missing:
        raise TodoError(f"missing detail files for: {', '.join(sorted(missing))}", code=E_STATE)
    orphaned = [item_id for item_id in details if item_id not in items]
    if orphaned:
        raise TodoError(f"orphaned detail files without index entries: {', '.join(sorted(orphaned))}", code=E_STATE)
    for item_id, detail in details.items():
        if not isinstance(detail, dict):
            raise TodoError(f"detail for {item_id!r} must be a JSON object", code=E_STATE)
        copies = INDEX_OWNED_FIELDS.intersection(detail)
        if copies:
            raise TodoError(
                f"detail for {item_id!r} must not copy index-owned fields: {', '.join(sorted(copies))}",
                code=E_STATE,
            )
        for key in ("acceptance", "links"):
            if key in detail and (
                not isinstance(detail[key], list) or any(not isinstance(v, str) for v in detail[key])
            ):
                raise TodoError(f"detail for {item_id!r}: {key!r} must be a list of strings", code=E_STATE)
        for key in ("description", "context"):
            if key in detail and not isinstance(detail[key], str):
                raise TodoError(f"detail for {item_id!r}: {key!r} must be a string", code=E_STATE)
        if "prepared" in detail:
            if version < 3:
                raise TodoError("registered prepared receipts require index schema version 3", code=E_SCHEMA)
            _validate_prepared_receipt(item_id, detail["prepared"])
        if "batch" in detail and version < 2:
            raise TodoError("batch metadata requires index schema version 2", code=E_SCHEMA)
        if "final_evidence" in detail:
            if version < 3:
                raise TodoError("final evidence requires index schema version 3", code=E_SCHEMA)
            _validate_final_evidence(item_id, detail["final_evidence"])
        _validate_batch_detail(item_id, detail, list(items[item_id].get("needs", [])))

    # A prepared receipt is useful only to an explicitly registered member of
    # the same batch.  Reject duplicate member identities and cross-batch
    # receipt/metadata pairs before they can affect readiness.
    members: dict[tuple[str, str], str] = {}
    for item_id, detail in details.items():
        batch = detail.get("batch")
        prepared = detail.get("prepared")
        if batch is not None:
            identity = (batch["batch_id"], batch["member_id"])
            previous = members.get(identity)
            if previous is not None and previous != item_id:
                raise TodoError(f"duplicate batch member {identity!r}: {previous!r} and {item_id!r}", code=E_STATE)
            members[identity] = item_id
        if prepared is not None:
            if version < 3:
                raise TodoError("prepared receipts require index schema version 3", code=E_SCHEMA)
            batch = detail.get("batch")
            if batch is None or (batch["batch_id"], batch["member_id"]) != (
                prepared["batch_id"], prepared["member_id"]
            ):
                raise TodoError(f"prepared receipt for {item_id!r} does not match batch metadata", code=E_STATE)
            record = batches.get(prepared["batch_id"])
            if record is None:
                raise TodoError(f"prepared receipt for {item_id!r} names an unknown batch", code=E_STATE)
            if prepared["member_id"] not in record["members"]:
                raise TodoError(f"prepared receipt for {item_id!r} names an unregistered member", code=E_STATE)
            if prepared["scope_hash"] != record["scope_hash"]:
                raise TodoError(f"prepared receipt for {item_id!r} has a foreign scope", code=E_STATE)
            if prepared["owner_generation"] != record["owner_generation"]:
                raise TodoError(f"prepared receipt for {item_id!r} has a stale owner generation", code=E_CLAIM_STALE)
            if record.get("accepted_members", {}).get(prepared["member_id"]) != prepared["accepted_head"]:
                raise TodoError(f"prepared receipt for {item_id!r} is not the registered accepted head", code=E_STATE)
            integrated = record.get("integrated_members", {}).get(prepared["member_id"])
            if not isinstance(integrated, dict) or integrated.get("accepted_head") != prepared["accepted_head"]:
                raise TodoError(f"prepared receipt for {item_id!r} lacks integration ancestry evidence", code=E_STATE)
        batch = detail.get("batch")
        if batch is not None and version >= 3:
            record = batches.get(batch["batch_id"])
            if record is None:
                raise TodoError(f"item {item_id!r} names an unknown batch", code=E_STATE)
            if batch["member_id"] != item_id or batch["member_id"] not in record["members"]:
                raise TodoError(f"item {item_id!r} is not a declared member of its batch", code=E_STATE)
    return index


@dataclass
class Snapshot:
    index: dict[str, Any]
    details: dict[str, dict[str, Any]] = field(default_factory=dict)


def _read_state_file(path: Path, label: str) -> Any:
    # State checkouts are Git content, but a hostile or corrupted worktree
    # could smuggle a symlink here; never follow one out of the checkout.
    if path.is_symlink():
        raise TodoError(f"refusing to follow symlink at {label}", code=E_STATE)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TodoError(f"missing {label} in {path.parent}: is this a state checkout?", code=E_STATE) from exc
    except json.JSONDecodeError as exc:
        raise TodoError(f"invalid {label}: {exc}", code=E_STATE) from exc


def load_snapshot(state_dir: str | Path) -> Snapshot:
    """Load and validate one local state directory."""
    root = Path(state_dir)
    index = _read_state_file(root / INDEX_FILENAME, INDEX_FILENAME)
    items_dir = root / ITEMS_DIRNAME
    if items_dir.is_symlink():
        raise TodoError("refusing to follow symlinked items directory", code=E_STATE)
    items_dir = root / ITEMS_DIRNAME
    details: dict[str, dict[str, Any]] = {}
    if isinstance(index, dict) and isinstance(index.get("items"), dict):
        for item_id in index["items"]:
            try:
                validate_id(item_id)
            except TodoError:
                continue
            path = items_dir / f"{item_id}.json"
            details[item_id] = _read_state_file(path, f"detail file for {item_id!r}")
        if items_dir.is_dir():
            for path in sorted(items_dir.glob("*.json")):
                if path.name[:-5] not in index["items"]:
                    details[path.name[:-5]] = _read_state_file(path, f"detail file {path.name}")
    validate_snapshot(index, details)
    return Snapshot(index=index, details=details)


def save_snapshot(state_dir: str | Path, snapshot: Snapshot) -> None:
    """Atomically persist a snapshot after validation.

    Each file is written to a temporary sibling and renamed, so a partial
    local write never leaves a half-updated file behind. Published state
    still comes only from accepted Git commits; this guards the scratch
    copy readers and writers share on one machine.
    """
    validate_snapshot(snapshot.index, snapshot.details)
    root = Path(state_dir)
    items_dir = root / ITEMS_DIRNAME
    items_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write(root / INDEX_FILENAME, snapshot.index)
    for item_id, detail in snapshot.details.items():
        _atomic_write(items_dir / f"{item_id}.json", detail)
    wanted = {f"{item_id}.json" for item_id in snapshot.details}
    for path in items_dir.glob("*.json"):
        if path.name not in wanted:
            path.unlink()


def _atomic_write(path: Path, payload: Any) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def entry_brief(item_id: str, entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item_id,
        "title": entry["title"],
        "priority": entry["priority"],
        "status": entry["status"],
    }


def is_ready(item_id: str, snapshot: Snapshot, now: datetime | None = None) -> bool:
    entry = snapshot.index["items"].get(item_id)
    if entry is None or entry["status"] != "open":
        return False
    if claim_is_live(entry.get("claim"), now):
        return False
    detail = snapshot.details.get(item_id, {})
    batch = detail.get("batch")
    implementation_deps = set(batch.get("implementation_dependencies", [])) if isinstance(batch, dict) else set()
    batch_record = snapshot.index.get("batches", {}).get(batch.get("batch_id")) if isinstance(batch, dict) else None
    for dep in entry.get("needs", []):
        dep_entry = snapshot.index["items"][dep]
        if dep_entry["status"] == "done":
            continue
        if dep not in implementation_deps:
            return False
        dep_detail = snapshot.details.get(dep, {})
        receipt = dep_detail.get("prepared")
        if (
            not isinstance(receipt, dict)
            or batch_record is None
            or receipt.get("batch_id") != batch.get("batch_id")
            or receipt.get("member_id") != dep
            or receipt.get("owner_generation") != batch_record.get("owner_generation")
            or receipt.get("scope_hash") != batch_record.get("scope_hash")
            or batch_record.get("accepted_members", {}).get(dep) != receipt.get("accepted_head")
            or batch_record.get("integrated_members", {}).get(dep, {}).get("accepted_head") != receipt.get("accepted_head")
            or batch_record.get("integrated_members", {}).get(dep, {}).get("integration_head") != batch_record.get("integration_head")
        ):
            return False
        if dep_detail.get("batch", {}).get("batch_id") != batch.get("batch_id"):
            return False
        if dep_entry.get("claim") is not None:
            return False
    return True


def downstream_unlocks(item_id: str, snapshot: Snapshot) -> int:
    return sum(
        1
        for other_id, entry in snapshot.index["items"].items()
        if other_id != item_id
        and item_id in entry.get("needs", [])
        and entry["status"] not in TERMINAL_STATUSES
    )


def order_key(item: tuple[str, dict[str, Any]]) -> tuple[int, str]:
    item_id, entry = item
    return (PRIORITY_RANK[entry["priority"]], item_id)


def query_items(
    snapshot: Snapshot,
    *,
    status: str | None = None,
    priority: str | None = None,
    text: str | None = None,
    ready_only: bool = False,
    now: datetime | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    moment = now or datetime.now(timezone.utc)
    if status is not None and status not in STATUSES:
        raise TodoError(f"unknown status {status!r}", code=E_STATE)
    if priority is not None and priority not in PRIORITIES:
        raise TodoError(f"unknown priority {priority!r}", code=E_STATE)
    needle = text.lower() if text else None
    rows: list[tuple[str, dict[str, Any]]] = []
    for item_id, entry in snapshot.index["items"].items():
        if status is not None and entry["status"] != status:
            continue
        if priority is not None and entry["priority"] != priority:
            continue
        if needle and needle not in item_id.lower() and needle not in str(entry["title"]).lower():
            continue
        if ready_only and not is_ready(item_id, snapshot, moment):
            continue
        rows.append((item_id, entry))
    rows.sort(key=order_key)
    return rows


def op_create(
    snapshot: Snapshot,
    *,
    item_id: str,
    title: str,
    priority: str = "medium",
    description: str = "",
    needs: list[str] | tuple[str, ...] = (),
    acceptance: list[str] | tuple[str, ...] = (),
    links: list[str] | tuple[str, ...] = (),
    context: str = "",
    batch: dict[str, Any] | None = None,
) -> str:
    validate_id(item_id)
    items = snapshot.index["items"]
    if item_id in items:
        raise TodoError(f"duplicate task ID {item_id!r}", code=E_STATE)
    entry = _check_entry_shape(
        item_id,
        {
            "title": title,
            "priority": priority,
            "status": "open",
            "claim": None,
            "needs": list(needs),
        },
    )
    for dep in entry.get("needs", []):
        validate_id(dep)
        if dep not in items:
            raise TodoError(f"task {item_id!r} needs unknown task {dep!r}", code=E_STATE)
    items[item_id] = entry
    previous_version = snapshot.index["schema_version"]
    if batch is not None and previous_version < SCHEMA_VERSION:
        snapshot.index["schema_version"] = SCHEMA_VERSION
    detail: dict[str, Any] = {}
    if description:
        detail["description"] = description
    if acceptance:
        detail["acceptance"] = list(acceptance)
    if links:
        detail["links"] = list(links)
    if context:
        detail["context"] = context
    if batch is not None:
        detail["batch"] = json.loads(json.dumps(batch))
    snapshot.details[item_id] = detail
    try:
        validate_snapshot(snapshot.index, snapshot.details)
    except TodoError:
        del items[item_id]
        del snapshot.details[item_id]
        snapshot.index["schema_version"] = previous_version
        raise
    return item_id


def _invalidate_prepared_dependents(snapshot: Snapshot, root_id: str) -> list[str]:
    """Invalidate prepared/final evidence downstream of a changed member."""
    invalidated: list[str] = []
    queue = [root_id]
    seen = {root_id}
    while queue:
        predecessor = queue.pop(0)
        for item_id, detail in snapshot.details.items():
            batch = detail.get("batch")
            deps = batch.get("implementation_dependencies", []) if isinstance(batch, dict) else []
            if predecessor not in deps or item_id in seen:
                continue
            if "prepared" not in detail and "final_evidence" not in detail:
                continue
            detail.pop("prepared", None)
            detail.pop("final_evidence", None)
            invalidated.append(item_id)
            seen.add(item_id)
            queue.append(item_id)
    return invalidated


def op_update(
    snapshot: Snapshot,
    item_id: str,
    *,
    title: str | None = None,
    priority: str | None = None,
    description: str | None = None,
    needs: list[str] | tuple[str, ...] | None = None,
    acceptance: list[str] | tuple[str, ...] | None = None,
    links: list[str] | tuple[str, ...] | None = None,
    context: str | None = None,
    status: str | None = None,
    batch: dict[str, Any] | None = None,
) -> dict[str, Any]:
    items = snapshot.index["items"]
    if item_id not in items:
        raise TodoError(f"unknown task {item_id!r}", code=E_STATE)
    existing_batch = snapshot.details[item_id].get("batch")
    if isinstance(existing_batch, dict):
        batch_record = snapshot.index.get("batches", {}).get(existing_batch.get("batch_id"))
        if isinstance(batch_record, dict) and batch_record.get("lifecycle", "active") != "active":
            raise TodoError(f"batch {existing_batch['batch_id']!r} is no longer active", code=E_STATE)
    entry = items[item_id]
    previous = json.loads(json.dumps(entry))
    previous_details = json.loads(json.dumps(snapshot.details))
    previous_version = snapshot.index["schema_version"]
    changed: list[str] = []
    if title is not None:
        entry["title"] = title
        changed.append("title")
    if priority is not None:
        entry["priority"] = priority
        changed.append("priority")
    if needs is not None:
        entry["needs"] = list(needs)
        changed.append("needs")
    if status is not None:
        if status not in STATUSES:
            raise TodoError(f"unknown status {status!r}", code=E_STATE)
        if status != entry["status"]:
            # Plain updates may only park and unpark work. Entering active
            # needs a claim (take); leaving active releases or closes it
            # (release/finish); terminal states go through finish/drop.
            # Bypassing those paths would strand or fabricate claims.
            if {entry["status"], status} != UPDATE_STATUSES:
                raise TodoError(
                    f"cannot move {item_id!r} from {entry['status']!r} to {status!r} via update; "
                    "use take/release/finish/drop",
                    code=E_STATE,
                )
            if claim_is_live(entry.get("claim")):
                raise TodoError(
                    f"cannot move claimed task {item_id!r} via update; release it first",
                    code=E_STATE,
                )
            entry["status"] = status
            changed.append("status")
    detail = snapshot.details[item_id]
    if description is not None:
        if description:
            detail["description"] = description
        else:
            detail.pop("description", None)
        changed.append("description")
    if acceptance is not None:
        if list(acceptance):
            detail["acceptance"] = list(acceptance)
        else:
            detail.pop("acceptance", None)
        changed.append("acceptance")
    if links is not None:
        if list(links):
            detail["links"] = list(links)
        else:
            detail.pop("links", None)
        changed.append("links")
    if context is not None:
        if context:
            detail["context"] = context
        else:
            detail.pop("context", None)
        changed.append("context")
    if batch is not None:
        if snapshot.index["schema_version"] < SCHEMA_VERSION:
            snapshot.index["schema_version"] = SCHEMA_VERSION
        detail["batch"] = json.loads(json.dumps(batch))
        changed.append("batch")
    invalidated: list[str] = []
    if changed and "prepared" in detail:
        # A prepared receipt binds the exact member content and declared
        # edge. Any later edit invalidates it instead of allowing a stale
        # receipt to unlock a dependent member.
        detail.pop("prepared", None)
        detail.pop("final_evidence", None)
        changed.append("prepared_receipt")
    if changed:
        invalidated = _invalidate_prepared_dependents(snapshot, item_id)
        if invalidated:
            changed.append("downstream_prepared_receipts")
    try:
        validate_snapshot(snapshot.index, snapshot.details)
    except TodoError:
        items[item_id] = previous
        snapshot.details.clear()
        snapshot.details.update(previous_details)
        snapshot.index["schema_version"] = previous_version
        raise
    return {"id": item_id, "changed": changed, "invalidated": invalidated}


def op_register_batch(
    snapshot: Snapshot,
    *,
    batch_id: str,
    project_id: str,
    repository: str,
    owner: str,
    owner_generation: str,
    integration_branch: str,
    integration_worktree: str,
    start_head: str,
    members: list[str] | tuple[str, ...],
    scope: dict[str, list[str]],
    scope_hash: str,
    delivery_boundary: str,
    terminal_outcome: str,
) -> dict[str, Any]:
    """Register one immutable batch contract before member preparation."""
    previous_version = snapshot.index["schema_version"]
    if snapshot.index["schema_version"] < SCHEMA_VERSION:
        snapshot.index["schema_version"] = SCHEMA_VERSION
    batches = snapshot.index.setdefault("batches", {})
    if batch_id in batches:
        raise TodoError(f"batch {batch_id!r} is already registered; refusing duplicate enrollment", code=E_CONFLICT)
    record = {
        "project_id": project_id,
        "repository": repository,
        "owner": validate_worker(owner),
        "owner_generation": owner_generation,
        "integration_branch": integration_branch,
        "integration_worktree": integration_worktree,
        "start_head": start_head,
        "integration_head": start_head,
        "accepted_members": {},
        "integrated_members": {},
        "members": list(members),
        "scope": json.loads(json.dumps(scope)),
        "scope_hash": scope_hash,
        "delivery_boundary": delivery_boundary,
        "terminal_outcome": terminal_outcome,
        "lifecycle": "active",
    }
    batches[batch_id] = record
    try:
        validate_snapshot(snapshot.index, snapshot.details)
    except TodoError:
        del batches[batch_id]
        snapshot.index["schema_version"] = previous_version
        raise
    return {"batch_id": batch_id, "members": list(members), "start_head": start_head}


def op_bind_batch_pr(
    snapshot: Snapshot,
    batch_id: str,
    worker: str,
    owner_generation: str,
    *,
    number: int,
    node_id: str,
    head: str,
) -> dict[str, Any]:
    """Bind the single declared final PR identity to a batch exactly once."""
    worker = validate_worker(worker)
    try:
        batch = snapshot.index["batches"][batch_id]
    except KeyError:
        raise TodoError(f"unknown batch {batch_id!r}", code=E_STATE) from None
    if batch["owner"] != worker or batch["owner_generation"] != owner_generation:
        raise TodoError(f"stale batch owner for {batch_id!r}", code=E_CLAIM_STALE)
    if batch.get("lifecycle", "active") != "active":
        raise TodoError(f"batch {batch_id!r} is no longer active", code=E_STATE)
    if batch.get("final_pr") is not None:
        raise TodoError(f"batch {batch_id!r} already has a final PR binding", code=E_CONFLICT)
    final_pr = {"number": number, "node_id": node_id, "head": head}
    _validate_batch_record(batch_id, {**batch, "final_pr": final_pr})
    if head != batch.get("integration_head"):
        raise TodoError(f"final PR head is not the registered integration head for {batch_id!r}", code=E_CONFLICT)
    batch["final_pr"] = final_pr
    validate_snapshot(snapshot.index, snapshot.details)
    return {"batch_id": batch_id, "final_pr": final_pr}


def op_abort_batch(snapshot: Snapshot, batch_id: str, worker: str, owner_generation: str) -> dict[str, Any]:
    """Abort an owned batch without manufacturing terminal member completion."""
    worker = validate_worker(worker)
    try:
        batch = snapshot.index["batches"][batch_id]
    except KeyError:
        raise TodoError(f"unknown batch {batch_id!r}", code=E_STATE) from None
    if batch["owner"] != worker or batch["owner_generation"] != owner_generation:
        raise TodoError(f"stale batch owner for {batch_id!r}", code=E_CLAIM_STALE)
    lifecycle = batch.get("lifecycle", "active")
    if lifecycle == "completed":
        raise TodoError(f"completed batch {batch_id!r} cannot be aborted", code=E_STATE)
    if lifecycle == "aborted":
        return {"batch_id": batch_id, "lifecycle": "aborted", "idempotent": True}
    if any(
        claim_is_live(snapshot.index["items"].get(member, {}).get("claim"))
        for member in batch["members"]
    ):
        raise TodoError(f"batch {batch_id!r} has a live member claim; release it before abort", code=E_ACTIVE_CLAIMS)
    if any(snapshot.index["items"].get(member, {}).get("status") == "done" for member in batch["members"]):
        raise TodoError(f"batch {batch_id!r} has completed members; resume closeout instead of aborting", code=E_STATE)
    for member in batch["members"]:
        detail = snapshot.details.get(member, {})
        detail.pop("prepared", None)
        detail.pop("final_evidence", None)
        # Keep the aborted registry as an audit record, but detach every
        # recoverable member so it returns to the ordinary claim/finish
        # lifecycle. Otherwise the archived lifecycle would strand the item
        # behind the batch-specific guards in prepare, update, and finish.
        detail.pop("batch", None)
        entry = snapshot.index["items"].get(member)
        if entry is not None:
            entry["claim"] = None
            if entry["status"] in ("open", "blocked", "active"):
                entry["status"] = "open"
    batch["accepted_members"] = {}
    batch["integrated_members"] = {}
    batch["lifecycle"] = "aborted"
    validate_snapshot(snapshot.index, snapshot.details)
    return {"batch_id": batch_id, "lifecycle": "aborted", "idempotent": False}


def _require_entry(snapshot: Snapshot, item_id: str) -> dict[str, Any]:
    try:
        return snapshot.index["items"][item_id]
    except KeyError:
        raise TodoError(f"unknown task {item_id!r}", code=E_STATE) from None


def op_take(
    snapshot: Snapshot,
    item_id: str,
    worker: str,
    *,
    ttl_hours: float = DEFAULT_TTL_HOURS,
    now: datetime | None = None,
) -> dict[str, Any]:
    worker = validate_worker(worker)
    entry = _require_entry(snapshot, item_id)
    moment = now or datetime.now(timezone.utc)
    if not 0 < ttl_hours <= MAX_TTL_HOURS:
        raise TodoError(f"ttl must be within (0, {MAX_TTL_HOURS}] hours", code=E_STATE)
    claim = entry.get("claim")
    if claim_is_live(claim, moment):
        if claim["worker"] == worker:
            # Restart re-adoption rotates the generation: the returning
            # worker resumes with fresh credentials, and any earlier
            # process image holding the old generation goes stale instead
            # of sharing control of the claim.
            claim["generation"] = new_generation()
            claim["expires_at"] = (moment + timedelta(hours=ttl_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
            claim["renewals"] = int(claim.get("renewals", 0)) + 1
            entry["status"] = "active"
            validate_snapshot(snapshot.index, snapshot.details)
            return {"id": item_id, "claim": dict(claim), "adopted": True}
        raise TodoError(f"task {item_id!r} is claimed by {claim['worker']!r}", code=E_CONFLICT)
    if entry["status"] not in ("open", "blocked", "active"):
        raise TodoError(f"task {item_id!r} is {entry['status']} and cannot be taken", code=E_STATE)
    fresh = {
        "worker": worker,
        "expires_at": (moment + timedelta(hours=ttl_hours)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generation": new_generation(),
        "renewals": 0,
    }
    entry["claim"] = fresh
    entry["status"] = "active"
    validate_snapshot(snapshot.index, snapshot.details)
    return {"id": item_id, "claim": fresh, "adopted": False}


def op_renew(
    snapshot: Snapshot,
    item_id: str,
    worker: str,
    generation: str,
    *,
    ttl_hours: float = DEFAULT_TTL_HOURS,
    now: datetime | None = None,
) -> dict[str, Any]:
    worker = validate_worker(worker)
    entry = _require_entry(snapshot, item_id)
    moment = now or datetime.now(timezone.utc)
    claim = entry.get("claim")
    if (
        not claim
        or claim.get("worker") != worker
        or claim.get("generation") != generation
    ):
        raise TodoError(f"stale claim for {item_id!r}: re-read with show and retry", code=E_CLAIM_STALE)
    if not 0 < ttl_hours <= MAX_TTL_HOURS:
        raise TodoError(f"ttl must be within (0, {MAX_TTL_HOURS}] hours", code=E_STATE)
    claim["expires_at"] = (moment + timedelta(hours=ttl_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    # The counter guarantees a renewal is always a state change, even when
    # the second-resolution expiry string is unchanged, and it makes the
    # bounded renewal path observable.
    claim["renewals"] = int(claim.get("renewals", 0)) + 1
    validate_snapshot(snapshot.index, snapshot.details)
    return {"id": item_id, "claim": dict(claim)}


def op_release(
    snapshot: Snapshot, item_id: str, worker: str, generation: str
) -> dict[str, Any]:
    worker = validate_worker(worker)
    entry = _require_entry(snapshot, item_id)
    claim = entry.get("claim")
    if (
        not claim
        or claim.get("worker") != worker
        or claim.get("generation") != generation
    ):
        raise TodoError(f"stale claim for {item_id!r}: cannot release another holder's task", code=E_CLAIM_STALE)
    entry["claim"] = None
    if entry["status"] == "active":
        entry["status"] = "open"
    validate_snapshot(snapshot.index, snapshot.details)
    return {"id": item_id, "status": entry["status"]}


def op_prepare(
    snapshot: Snapshot,
    item_id: str,
    worker: str,
    generation: str,
    *,
    batch_id: str,
    member_id: str,
    source_worktree: str,
    source_revision: str,
    source_base: str,
    accepted_head: str,
    integration_head: str,
    scope_hash: str,
    verification: dict[str, Any],
    changed_files: list[str] | tuple[str, ...] = (),
    implementation_dependencies: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    """Persist prepared evidence and release the member claim.

    Preparation is intentionally not completion.  The item returns to ``open``
    with no claim, and only an explicitly registered same-batch implementation
    edge may use its receipt for readiness.  The service validates the actual
    Git checkout before calling this pure operation; this function validates
    the durable shape and claim authority again on every publication retry.
    """
    worker = validate_worker(worker)
    entry = _require_entry(snapshot, item_id)
    claim = entry.get("claim")
    if not claim or claim.get("worker") != worker or claim.get("generation") != generation:
        raise TodoError(f"stale claim for {item_id!r}: only the holder can prepare", code=E_CLAIM_STALE)
    if entry["status"] != "active":
        raise TodoError(f"task {item_id!r} must be active before it can be prepared", code=E_STATE)
    validate_id(batch_id)
    validate_id(member_id)
    validate_id(item_id)
    source_worktree = str(source_worktree)
    source_revision = _validate_revision(source_revision, "source_revision")
    source_base = _validate_revision(source_base, "source_base")
    accepted_head = _validate_revision(accepted_head, "accepted_head")
    integration_head = _validate_revision(integration_head, "integration_head")
    if accepted_head != source_revision:
        raise TodoError("prepared accepted head must identify the exact source revision", code=E_STATE)
    if not isinstance(scope_hash, str) or not SCOPE_HASH_RE.fullmatch(scope_hash):
        raise TodoError("scope_hash must be a full SHA-256 digest", code=E_STATE)
    changed_files = sorted(set(changed_files))
    deps = list(implementation_dependencies)
    if len(set(deps)) != len(deps) or any(dep not in entry.get("needs", []) for dep in deps):
        raise TodoError(
            f"prepared implementation dependencies for {item_id!r} must be unique members of needs",
            code=E_STATE,
        )
    detail = snapshot.details[item_id]
    batch = detail.get("batch")
    batch_record = snapshot.index.get("batches", {}).get(batch_id)
    if not isinstance(batch_record, dict) or (batch.get("batch_id"), batch.get("member_id")) != (batch_id, member_id):
        raise TodoError(
            f"task {item_id!r} must be explicitly registered as batch member {member_id!r} before prepare",
            code=E_STATE,
        )
    if batch_record.get("lifecycle", "active") != "active":
        raise TodoError(f"batch {batch_id!r} is no longer active", code=E_STATE)
    registered_deps = list(batch.get("implementation_dependencies", []))
    if registered_deps != deps:
        raise TodoError("prepare dependencies differ from registered batch metadata", code=E_STATE)
    if scope_hash != batch_record["scope_hash"]:
        raise TodoError("prepared source does not match the registered batch scope", code=E_STATE)
    if member_id not in batch_record["members"]:
        raise TodoError("prepared member is not declared in the batch", code=E_STATE)
    previous_detail = json.loads(json.dumps(detail))
    previous_batch_record = json.loads(json.dumps(batch_record))
    previous_index_version = snapshot.index["schema_version"]
    if detail.get("prepared") is not None:
        raise TodoError(f"task {item_id!r} already has prepared evidence", code=E_STATE)
    if snapshot.index["schema_version"] < SCHEMA_VERSION:
        snapshot.index["schema_version"] = SCHEMA_VERSION
    detail["prepared"] = {
        "schema": PREPARED_RECEIPT_SCHEMA,
        "batch_id": batch_id,
        "member_id": member_id,
        "owner_generation": batch_record["owner_generation"],
        "source_worktree": source_worktree,
        "source_revision": source_revision,
        "source_base": source_base,
        "accepted_head": accepted_head,
        "integration_head": integration_head,
        "scope_hash": scope_hash,
        "changed_files": changed_files,
        "verification": json.loads(json.dumps(verification)),
    }
    # Advancing the registered integration head is part of the same state
    # transaction as the receipt.  The service has already proved that this
    # exact clean checkout is the integration worktree at this head, and the
    # map lets pure readiness checks distinguish an accepted member from a
    # merely caller-asserted receipt.
    batch_record["integration_head"] = integration_head
    accepted_members = batch_record.setdefault("accepted_members", {})
    accepted_members[member_id] = accepted_head
    integrated_members = batch_record.setdefault("integrated_members", {})
    for integrated_member, integrated_head in accepted_members.items():
        integrated_members[integrated_member] = {
            "accepted_head": integrated_head,
            "integration_head": integration_head,
        }
    entry["claim"] = None
    entry["status"] = "open"
    try:
        validate_snapshot(snapshot.index, snapshot.details)
    except TodoError:
        snapshot.index["schema_version"] = previous_index_version
        snapshot.details[item_id] = previous_detail
        snapshot.index["batches"][batch_id] = previous_batch_record
        raise
    return {
        "id": item_id,
        "status": "open",
        "prepared": {
            "batch_id": batch_id,
            "member_id": member_id,
            "source_worktree": source_worktree,
            "source_revision": source_revision,
        },
    }


def op_finish(
    snapshot: Snapshot,
    item_id: str,
    worker: str,
    generation: str,
    final_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Close a task, requiring final-tree evidence for prepared work."""
    worker = validate_worker(worker)
    entry = _require_entry(snapshot, item_id)
    claim = entry.get("claim")
    if (
        not claim
        or claim.get("worker") != worker
        or claim.get("generation") != generation
    ):
        raise TodoError(f"stale claim for {item_id!r}: only the holder can finish", code=E_CLAIM_STALE)
    detail = snapshot.details[item_id]
    batch = snapshot.index.get("batches", {}).get((detail.get("batch") or {}).get("batch_id"))
    if isinstance(batch, dict) and batch.get("lifecycle", "active") != "active":
        raise TodoError(f"batch {(detail.get('batch') or {}).get('batch_id')!r} is no longer active", code=E_STATE)
    previous_detail = json.loads(json.dumps(detail))
    previous_entry = json.loads(json.dumps(entry))
    previous_batch = json.loads(json.dumps(batch)) if isinstance(batch, dict) else None
    if isinstance(batch, dict) and detail.get("prepared") is None:
        raise TodoError(
            f"registered batch member {item_id!r} must be prepared before finish",
            code=E_FINAL_EVIDENCE,
        )
    if detail.get("prepared") is not None:
        if final_evidence is None:
            raise TodoError(
                f"prepared task {item_id!r} needs final-tree evidence before finish",
                code=E_FINAL_EVIDENCE,
            )
        detail["final_evidence"] = _validate_batch_final_evidence(
            snapshot, item_id, _validate_final_evidence(item_id, final_evidence)
        )
    entry["claim"] = None
    entry["status"] = "done"
    if isinstance(batch, dict) and all(
        snapshot.index["items"].get(member, {}).get("status") == "done" for member in batch["members"]
    ):
        batch["lifecycle"] = "completed"
    try:
        validate_snapshot(snapshot.index, snapshot.details)
    except TodoError:
        snapshot.details[item_id] = previous_detail
        snapshot.index["items"][item_id] = previous_entry
        if previous_batch is not None:
            snapshot.index["batches"][batch["batch_id"]] = previous_batch
        raise
    return {"id": item_id, "status": "done"}


def op_drop(
    snapshot: Snapshot,
    item_id: str,
    worker: str | None = None,
    generation: str | None = None,
) -> dict[str, Any]:
    """Abandon a task. A live claim needs its holder's generation, exactly
    like release/finish: a stale process image must not terminally drop work
    another process re-adopted. Unclaimed tasks drop without credentials."""
    entry = _require_entry(snapshot, item_id)
    if entry["status"] in TERMINAL_STATUSES:
        raise TodoError(f"task {item_id!r} is already {entry['status']}", code=E_STATE)
    claim = entry.get("claim")
    if claim_is_live(claim):
        if (
            worker is None
            or generation is None
            or claim.get("worker") != validate_worker(worker)
            or claim.get("generation") != generation
        ):
            holder = claim["worker"] if claim else "?"
            raise TodoError(
                f"stale claim for {item_id!r}: held by {holder!r}; only the holder can drop",
                code=E_CLAIM_STALE,
            )
    entry["claim"] = None
    entry["status"] = "dropped"
    validate_snapshot(snapshot.index, snapshot.details)
    return {"id": item_id, "status": "dropped"}


def assert_quiescent(snapshot: Snapshot, now: datetime | None = None) -> None:
    """Refuse cutover/migration while live claims exist."""
    moment = now or datetime.now(timezone.utc)
    holders = sorted(
        {entry["claim"]["worker"] for entry in snapshot.index["items"].values() if claim_is_live(entry.get("claim"), moment)}
    )
    if holders:
        raise TodoError(
            f"refusing: live claims held by {', '.join(holders)}; "
            "wait for release/finish or expiry before cutover",
            code=E_ACTIVE_CLAIMS,
        )


def ready_rows(snapshot: Snapshot, now: datetime | None = None) -> list[tuple[str, dict[str, Any]]]:
    return query_items(snapshot, ready_only=True, now=now)
