"""Per-batch claim slots, settled-batch context edits, receipt validation and payload files."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from test_batch_v2 import BATCH, SCOPE, Flow, _bind, _final, _finish, _pure, _verification, h

from tractable import store
from tractable.errors import TodoError
from tractable.service import MAX_PAYLOAD_FILE_BYTES, TrackerService, resolve_payload


def test_leaders_of_different_slots_hold_claims_concurrently(tmp_path: Path) -> None:
    flow = Flow(tmp_path, ["a", "b"], SCOPE)
    leader = flow.leader
    assert leader.create_item("free", "Free")["ok"]
    assert leader.create_item("free2", "Free two")["ok"]
    member = leader.take("a")
    assert member["ok"], member
    ordinary = leader.take("free")
    assert ordinary["ok"], ordinary
    second_ordinary = leader.take("free2")
    assert not second_ordinary["ok"] and second_ordinary["code"] == "E_MULTIPLE_CLAIMS"
    second_member = leader.take("b")
    assert not second_member["ok"] and second_member["code"] == "E_MULTIPLE_CLAIMS"
    assert any("'a'" in step for step in second_member["recovery"])
    assert leader.release("a", member["data"]["claim"]["generation"])["ok"]
    assert leader.take("b")["ok"]


def test_claim_slot_is_per_active_batch() -> None:
    snap = _pure(["a"])
    store.op_create(snap, item_id="free", title="Free")
    store.op_take(snap, "a", "leader")
    svc = TrackerService.__new__(TrackerService)
    svc.worker = "leader"
    now = datetime.now(timezone.utc)
    assert svc._held_claim(snap, now, "free") is None
    assert svc._held_claim(snap, now, "a") == "a"


def _settled(members: list[str]) -> store.Snapshot:
    snap = _pure(members)
    for member in members:
        _prep_member(snap, member)
    _bind(snap)
    for member in members:
        _finish(snap, member, _final(snap, members))
    assert snap.index["batches"][BATCH]["lifecycle"] == "completed"
    return snap


def _prep_member(snap: store.Snapshot, member: str, **overrides: Any) -> None:
    generation = store.op_take(snap, member, "leader")["claim"]["generation"]
    accepted = h(f"acc-{member}")
    verification = {**_verification(accepted), **overrides}
    store.op_prepare(
        snap, member, "leader", generation, batch_id=BATCH, member_id=member, source_worktree="/tmp/src",
        source_revision=accepted, source_base=h("start"), accepted_head=accepted,
        integration_head=h(f"head-{member}"), scope_hash=snap.index["batches"][BATCH]["scope_hash"],
        verification=verification,
    )


def test_context_append_is_allowed_on_done_members_of_a_completed_batch() -> None:
    snap = _settled(["a"])
    out = store.op_update(snap, "a", context_append="post-merge evidence")
    assert out["changed"] == ["context"]
    assert snap.details["a"]["context"] == "post-merge evidence"
    assert "prepared" in snap.details["a"]
    store.op_update(snap, "a", context="replaced")
    assert snap.details["a"]["context"] == "replaced"


def test_other_edits_stay_refused_on_a_completed_batch() -> None:
    snap = _settled(["a"])
    for kwargs in ({"description": "late"}, {"title": "late"}, {"description": "late", "context_append": "n"}):
        with pytest.raises(TodoError) as exc:
            store.op_update(snap, "a", **kwargs)
        assert exc.value.code == "E_STATE"
    assert "prepared" in snap.details["a"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"review": None},
        {"review": {}},
        {"review": {"outcome": ""}},
        {"review": {"outcome": "looks-fine"}},
        {"review": "clear"},
        {"command": []},
        {"command": "pytest -q"},
        {"command": ["pytest", ""]},
        {"command": ["pytest -q"]},
        {"command": ["pytest", "tests/a.py\n"]},
        {"command": ["pytest", 3]},
        {"suite": " "},
        {"clean": False},
        {"status": "failed"},
    ],
)
def test_prepare_rejects_malformed_receipts(overrides: dict[str, Any]) -> None:
    snap = _pure(["a"])
    with pytest.raises(TodoError) as exc:
        _prep_member(snap, "a", **overrides)
    assert exc.value.code == "E_STATE"
    assert "prepared" not in snap.details["a"]
    assert snap.index["items"]["a"]["claim"] is not None


def test_prepare_requires_the_review_key() -> None:
    snap = _pure(["a"])
    generation = store.op_take(snap, "a", "leader")["claim"]["generation"]
    accepted = h("acc-a")
    verification = _verification(accepted)
    del verification["review"]
    with pytest.raises(TodoError):
        store.op_prepare(
            snap, "a", "leader", generation, batch_id=BATCH, member_id="a", source_worktree="/tmp/src",
            source_revision=accepted, source_base=h("start"), accepted_head=accepted,
            integration_head=h("head-a"), scope_hash=snap.index["batches"][BATCH]["scope_hash"],
            verification=verification,
        )


@pytest.mark.parametrize("outcome", ["clear", "not-reviewed", "override"])
def test_prepare_accepts_each_review_outcome(outcome: str) -> None:
    snap = _pure(["a"])
    _prep_member(snap, "a", review={"outcome": outcome, "trusted_sha": h("t")})
    assert snap.details["a"]["prepared"]["verification"]["review"]["outcome"] == outcome


def test_payload_file_is_read_as_the_object(tmp_path: Path) -> None:
    payload = {"status": "passed", "command": ["pytest", *[f"tests/test_{i}.py::{'x' * 40}" for i in range(500)]]}
    path = tmp_path / "verification.json"
    path.write_text(json.dumps(payload))
    assert resolve_payload("verification", None, str(path)) == (payload, None)
    assert resolve_payload("verification", payload, None) == (payload, None)


def test_payload_sources_are_exclusive_and_validated(tmp_path: Path) -> None:
    good = tmp_path / "good.json"
    good.write_text("{}")
    cases = {
        "both": ({"a": 1}, str(good)),
        "neither": (None, None),
        "relative": (None, "good.json"),
        "missing": (None, str(tmp_path / "missing.json")),
    }
    for name, (inline, path) in cases.items():
        payload, refusal = resolve_payload("verification", inline, path)
        assert payload is None and refusal["ok"] is False and refusal["code"] == "E_STATE", name
    not_json = tmp_path / "bad.json"
    not_json.write_text("{")
    listing = tmp_path / "list.json"
    listing.write_text("[]")
    big = tmp_path / "big.json"
    big.write_text(" " * (MAX_PAYLOAD_FILE_BYTES + 1))
    for path in (not_json, listing, big):
        payload, refusal = resolve_payload("verification", None, str(path))
        assert payload is None and refusal["code"] == "E_STATE", path.name
