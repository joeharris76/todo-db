"""Session-state fields: blocked reasons, decisions, acceptance progress, size caps, credential refusal."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import anyio
import mcp.types as types
import pytest
from mcp.shared.memory import create_connected_server_and_client_session as connect

from todo_db import git_backend, store
from todo_db.errors import TodoError
from todo_db.git_backend import StateRef, bootstrap
from todo_db.mcp.server import build_parser, build_server, resolve_launch_config
from todo_db.service import TrackerService


def _snap() -> store.Snapshot:
    snap = store.Snapshot(index=store.empty_index(), details={})
    store.op_create(snap, item_id="alpha", title="Alpha task")
    return snap


def _decide(snap: store.Snapshot, item_id: str = "alpha", **kwargs):
    kwargs.setdefault("actor", "w1")
    kwargs.setdefault("session", "session-a")
    return store.op_record_decision(snap, item_id, **kwargs)


def _code(excinfo: pytest.ExceptionInfo) -> str | None:
    return excinfo.value.code


def _svc(tmp_path: Path, session_id: str | None = "session-a") -> TrackerService:
    remote = tmp_path / "state.git"
    subprocess.run(["git", "init", "--quiet", "--bare", str(remote)], check=True)
    ref = git_backend.StateRef(remote=str(remote), branch="todo-state")
    git_backend.bootstrap(ref)
    return TrackerService(
        ref=ref,
        cache_dir=tmp_path / "cache",
        worker="w1",
        session_id=session_id,
        client_name="pytest",
    )


# -- blocked reason --


def test_blocking_requires_a_reason_and_unblock_clears_it() -> None:
    snap = _snap()
    with pytest.raises(TodoError) as excinfo:
        store.op_update(snap, "alpha", status="blocked")
    assert _code(excinfo) == "E_STATE"
    with pytest.raises(TodoError):
        store.op_update(snap, "alpha", status="blocked", reason="   ")
    assert snap.index["items"]["alpha"]["status"] == "open"
    store.op_update(snap, "alpha", status="blocked", reason="waiting on\nthe vendor")
    assert snap.details["alpha"]["blocked_reason"] == "waiting on\nthe vendor"
    out = store.op_update(snap, "alpha", status="blocked", reason="waiting on legal")
    assert snap.details["alpha"]["blocked_reason"] == "waiting on legal"
    assert out["changed"] == ["blocked_reason"]
    store.op_update(snap, "alpha", status="open")
    assert "blocked_reason" not in snap.details["alpha"]


def test_reason_needs_status_blocked_and_is_bounded() -> None:
    snap = _snap()
    with pytest.raises(TodoError):
        store.op_update(snap, "alpha", reason="no status")
    with pytest.raises(TodoError):
        store.op_update(snap, "alpha", status="open", reason="wrong status")
    with pytest.raises(TodoError):
        store.op_update(snap, "alpha", status="blocked", reason="x" * 1001)
    with pytest.raises(TodoError):
        store.op_update(snap, "alpha", status="blocked", reason="bad\x00control")
    store.op_update(snap, "alpha", status="blocked", reason="x" * 1000)


def test_stale_reason_from_older_client_is_tolerated_then_cleared() -> None:
    snap = _snap()
    # An older client unblocked the task without knowing the reason key.
    snap.details["alpha"]["blocked_reason"] = "old reason"
    store.validate_snapshot(snap.index, snap.details)
    # A blocked task with no reason is also valid older-client data.
    store.op_create(snap, item_id="beta", title="Beta")
    snap.index["items"]["beta"]["status"] = "blocked"
    store.validate_snapshot(snap.index, snap.details)
    out = store.op_update(snap, "alpha", title="Alpha v2")
    assert "blocked_reason" not in snap.details["alpha"]
    assert "blocked_reason" in out["changed"]
    with pytest.raises(TodoError):
        snap.details["alpha"]["blocked_reason"] = 42
        store.validate_snapshot(snap.index, snap.details)


def test_take_refuses_blocked_without_override_and_clears_reason() -> None:
    snap = _snap()
    store.op_update(snap, "alpha", status="blocked", reason="waiting on infra")
    with pytest.raises(TodoError) as excinfo:
        store.op_take(snap, "alpha", "w1")
    assert _code(excinfo) == "E_STATE"
    assert "waiting on infra" in str(excinfo.value)
    assert "override_blocked" in str(excinfo.value)
    first = store.op_take(snap, "alpha", "w1", override_blocked=True)["claim"]["generation"]
    assert snap.index["items"]["alpha"]["status"] == "active"
    assert "blocked_reason" not in snap.details["alpha"]
    # Re-adoption by the same worker is unchanged.
    again = store.op_take(snap, "alpha", "w1")
    assert again["adopted"] is True and again["claim"]["generation"] != first


# -- decisions and questions --


def test_decision_needs_source_and_question_answer_pairs() -> None:
    snap = _snap()
    with pytest.raises(TodoError):
        _decide(snap, text="Use JSON")
    question = _decide(snap, kind="question", text="Which format?")
    assert question["id"] == 1 and "source" not in question and question["actor"] == "w1"
    assert question["session"] == "session-a"
    assert store.open_questions(snap, "alpha") == [1]
    with pytest.raises(TodoError):
        _decide(snap, kind="question", text="Q?", answers=1)
    with pytest.raises(TodoError):
        _decide(snap, text="Answer", source="2026-10-17 chat", answers=99)
    answer = _decide(snap, text="Use JSON", source="2026-10-17 chat with owner", answers=1)
    assert answer["id"] == 2 and answer["answers"] == 1
    assert store.open_questions(snap, "alpha") == []
    with pytest.raises(TodoError) as excinfo:
        _decide(snap, text="Use YAML", source="later", answers=1)
    assert "already answered" in str(excinfo.value)
    with pytest.raises(TodoError):
        _decide(snap, text="Points at a decision", source="s", answers=2)
    with pytest.raises(TodoError):
        _decide(snap, kind="note", text="unknown kind", source="s")
    with pytest.raises(TodoError):
        _decide(snap, text="one\nline", source="two\nlines")
    store.validate_snapshot(snap.index, snap.details)


def test_decisions_refused_on_terminal_items_and_need_no_claim() -> None:
    snap = _snap()
    _decide(snap, text="Ship it", source="standup")
    take = store.op_take(snap, "alpha", "w1")
    _decide(snap, text="Ship it today", source="standup")
    store.op_finish(snap, "alpha", "w1", take["claim"]["generation"])
    with pytest.raises(TodoError) as excinfo:
        _decide(snap, text="Too late", source="standup")
    assert _code(excinfo) == "E_STATE"


def test_decision_rows_survive_updates_and_reject_tampering() -> None:
    snap = _snap()
    _decide(snap, kind="question", text="Q?")
    rows = json.loads(json.dumps(snap.details["alpha"]["decisions"]))
    store.op_update(snap, "alpha", title="New", description="d", acceptance=["a"], context="c")
    assert snap.details["alpha"]["decisions"] == rows
    with pytest.raises(TypeError):
        store.op_update(snap, "alpha", decisions=[])  # type: ignore[call-arg]
    snap.details["alpha"]["decisions"].append(dict(rows[0]))
    with pytest.raises(TodoError):
        store.validate_snapshot(snap.index, snap.details)


def test_open_question_blocks_readiness_and_finish() -> None:
    snap = _snap()
    assert store.is_ready("alpha", snap)
    _decide(snap, kind="question", text="Q?")
    assert not store.is_ready("alpha", snap)
    take = store.op_take(snap, "alpha", "w1")
    with pytest.raises(TodoError) as excinfo:
        store.op_finish(snap, "alpha", "w1", take["claim"]["generation"])
    assert _code(excinfo) == "E_OPEN_QUESTIONS"
    _decide(snap, text="Answer", source="email 2026-10-17", answers=1)
    store.op_release(snap, "alpha", "w1", take["claim"]["generation"])
    assert store.is_ready("alpha", snap)
    take = store.op_take(snap, "alpha", "w1")
    assert store.op_finish(snap, "alpha", "w1", take["claim"]["generation"])["status"] == "done"


def test_record_decision_service_appends_session_and_show_pages(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    assert svc.create_item("alpha", "Alpha task", acceptance=["one", "two"])["ok"]
    asked = svc.record_decision("alpha", "Which format?", kind="question")
    assert asked["ok"], asked
    assert asked["data"]["decision"] == {"id": 1, "kind": "question"}
    assert asked["data"]["open_questions"] == [1]
    shown = svc.show_item("alpha")["data"]
    assert shown["open_questions"] == [1] and shown["ready"] is False
    assert shown["decisions"][0]["text"] == "Which format?"
    assert shown["acceptance_met"] == [] and shown["next_step"] == "one"
    took = svc.take("alpha")
    refused = svc.finish("alpha", took["data"]["claim"]["generation"])
    assert not refused["ok"] and refused["code"] == "E_OPEN_QUESTIONS"
    answered = svc.record_decision("alpha", "JSON", source="chat 2026-10-17", answers=1)
    assert answered["ok"] and answered["data"]["open_questions"] == []
    assert svc.update_item("alpha", acceptance_met=[0])["ok"]
    shown = svc.show_item("alpha")["data"]
    assert shown["acceptance_met"] == [0] and shown["next_step"] == "two"
    assert "open_questions" not in shown
    snap = git_backend.read(svc.ref, tmp_path / "cache").snapshot
    ops = [entry["op"] for entry in snap.details["alpha"]["sessions"]]
    assert ops == ["create", "decide", "take", "decide", "update"]
    assert snap.details["alpha"]["decisions"][1]["actor"] == "w1"
    assert snap.details["alpha"]["decisions"][1]["session"] == "session-a"
    assert svc.finish("alpha", took["data"]["claim"]["generation"])["ok"]


def test_large_decision_log_pages_by_row(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    assert svc.create_item("alpha", "Alpha task")["ok"]
    for n in range(6):
        assert svc.record_decision("alpha", f"{n} " + "d" * 900, source="standup")["ok"]
    shown = svc.show_item("alpha")["data"]
    assert shown["decisions"]["total"] == 6 and shown["decisions"]["last"]["id"] == 6
    cont = shown["decisions_continuation"]
    page = svc.show_item("alpha", field="decisions", offset=0, rev=cont["rev"])
    assert page["ok"] and page["data"]["total"] == 6
    assert [row["id"] for row in page["data"]["window"]] == list(range(1, 7))


def test_record_decision_requires_a_session(tmp_path: Path) -> None:
    svc = _svc(tmp_path, session_id=None)
    assert svc.create_item("alpha", "Alpha task")["ok"]
    refused = svc.record_decision("alpha", "Ship", source="chat")
    assert not refused["ok"] and refused["code"] == "E_STATE"


def test_take_service_blocked_override(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    assert svc.create_item("alpha", "Alpha task")["ok"]
    refused = svc.update_item("alpha", status="blocked")
    assert not refused["ok"] and refused["code"] == "E_STATE"
    assert svc.update_item("alpha", status="blocked", reason="vendor")["ok"]
    assert svc.show_item("alpha")["data"]["blocked_reason"] == "vendor"
    refused = svc.take("alpha")
    assert not refused["ok"] and refused["code"] == "E_STATE"
    took = svc.take("alpha", override_blocked=True)
    assert took["ok"], took
    assert "blocked_reason" not in svc.show_item("alpha")["data"]


# -- acceptance progress --


def test_acceptance_met_stores_texts_and_reports_progress() -> None:
    snap = _snap()
    store.op_update(snap, "alpha", acceptance=["a", "b", "c"], acceptance_met=[2, 0])
    assert snap.details["alpha"]["acceptance_met"] == ["a", "c"]
    assert store.acceptance_progress(snap.details["alpha"]) == ([0, 2], "b")
    store.op_update(snap, "alpha", acceptance_met=[0, 1, 2])
    assert store.acceptance_progress(snap.details["alpha"]) == ([0, 1, 2], None)
    store.op_update(snap, "alpha", acceptance_met=[])
    assert "acceptance_met" not in snap.details["alpha"]


def test_acceptance_met_rejects_bad_indexes() -> None:
    snap = _snap()
    store.op_update(snap, "alpha", acceptance=["a", "b"])
    for bad in ([2], [-1], [0, 0], [True]):
        with pytest.raises(TodoError) as excinfo:
            store.op_update(snap, "alpha", acceptance_met=bad)
        assert _code(excinfo) == "E_STATE"
    assert "acceptance_met" not in snap.details["alpha"]
    # Indexes refer to acceptance after the same call's rewrite.
    store.op_update(snap, "alpha", acceptance=["a", "b", "c"], acceptance_met=[2])
    assert snap.details["alpha"]["acceptance_met"] == ["c"]


def test_acceptance_rewrite_prunes_stale_texts() -> None:
    snap = _snap()
    store.op_update(snap, "alpha", acceptance=["a", "b", "c"], acceptance_met=[0, 1])
    store.op_update(snap, "alpha", acceptance=["b", "c", "d"])
    assert snap.details["alpha"]["acceptance_met"] == ["b"]
    store.op_update(snap, "alpha", acceptance=["x"])
    assert "acceptance_met" not in snap.details["alpha"]


def test_stale_met_texts_from_older_client_are_tolerated() -> None:
    snap = _snap()
    store.op_update(snap, "alpha", acceptance=["a", "b"], acceptance_met=[0])
    # An older client rewrote acceptance without knowing acceptance_met.
    snap.details["alpha"]["acceptance"] = ["b", "z"]
    store.validate_snapshot(snap.index, snap.details)
    assert store.acceptance_progress(snap.details["alpha"]) == ([], "b")
    store.op_update(snap, "alpha", title="Edited")
    assert snap.details["alpha"]["acceptance_met"] == ["a"]


# -- size caps --


def test_create_and_update_refuse_growth_past_writable_cap() -> None:
    snap = _snap()
    with pytest.raises(TodoError) as excinfo:
        store.op_create(snap, item_id="big", title="Big", description="x" * 9000)
    assert _code(excinfo) == "E_ITEM_TOO_LARGE"
    message = str(excinfo.value)
    assert "8192" in message and "linked document" in message
    assert "big" not in snap.index["items"]
    store.op_update(snap, "alpha", description="x" * 8000)
    with pytest.raises(TodoError) as excinfo:
        store.op_update(snap, "alpha", context="y" * 500)
    assert _code(excinfo) == "E_ITEM_TOO_LARGE"
    assert "context" not in snap.details["alpha"]
    with pytest.raises(TodoError) as excinfo:
        _decide(snap, text="z" * 900, source="standup")
    assert _code(excinfo) == "E_ITEM_TOO_LARGE"
    assert "decisions" not in snap.details["alpha"]


def test_session_history_never_counts_toward_the_file_cap() -> None:
    snap = _snap()
    store.op_update(snap, "alpha", description="x" * 4000)
    for n in range(120):
        store.record_session(
            snap,
            "alpha",
            actor="w1",
            session_id="session-a",
            client=None,
            op="update",
            op_id=f"{n:032x}",
        )
    assert store.detail_bytes(snap.details["alpha"]) < store.MAX_DETAIL_BYTES
    store.op_update(snap, "alpha", context="grow")


def test_file_cap_refuses_growth_past_agent_written_content() -> None:
    snap = _snap()
    for n in range(9):
        store.op_defer(snap, "alpha", summary=f"later {n}", reason="r" * 1900)
    assert store.writable_bytes("Alpha", snap.details["alpha"]) < store.MAX_WRITABLE_BYTES
    assert store.detail_bytes(snap.details["alpha"]) > store.MAX_DETAIL_BYTES
    with pytest.raises(TodoError) as excinfo:
        store.op_update(snap, "alpha", context="grow")
    assert _code(excinfo) == "E_ITEM_TOO_LARGE"
    assert "16384" in str(excinfo.value)
    store.op_update(snap, "alpha", priority="high")


def test_oversized_item_stays_editable_without_growth() -> None:
    snap = _snap()
    # Written by an older client with no caps.
    snap.details["alpha"]["description"] = "x" * 10000
    store.validate_snapshot(snap.index, snap.details)
    store.op_update(snap, "alpha", priority="high")
    store.op_update(snap, "alpha", description="x" * 9500)
    with pytest.raises(TodoError):
        store.op_update(snap, "alpha", description="x" * 9600)


def test_legacy_key_is_excluded_from_the_file_cap() -> None:
    snap = _snap()
    snap.details["alpha"]["legacy"] = {"notes": "l" * 20000}
    store.op_update(snap, "alpha", description="fits")
    assert store.detail_bytes(snap.details["alpha"]) < store.MAX_DETAIL_BYTES


# -- credential check --

SECRET_SAMPLES = [
    ("AKIA" + "ABCDEFGHIJKLMNOP", "AWS access key ID"),
    ("ASIA" + "ABCDEFGHIJKLMNOP", "AWS access key ID"),
    ("https://b.s3.amazonaws.com/k?X-Amz-Credential=abc", "presigned URL credential"),
    ("https://b.s3.amazonaws.com/k?X-Amz-Signature=abc", "presigned URL signature"),
    ("ghp_" + "a" * 36, "GitHub token"),
    ("github_pat_" + "A" * 22, "GitHub fine-grained token"),
    ("-----BEGIN RSA PRIVATE KEY-----", "private key block"),
    ("-----BEGIN PRIVATE KEY-----", "private key block"),
    ("xoxb-" + "1234567890", "Slack token"),
    ("sk-ant-" + "a" * 20, "Anthropic API key"),
    ("sk-" + "a" * 32, "OpenAI API key"),
    ("sk-proj-" + "a" * 32, "OpenAI API key"),
    ("AIza" + "b" * 35, "Google API key"),
    ("eyJ" + "a" * 10 + ".eyJ" + "b" * 10 + "." + "c" * 10, "JSON Web Token"),
    ("postgres://admin:hunter2@db.internal/app", "credentials in a URL"),
]


@pytest.mark.parametrize(("value", "kind"), SECRET_SAMPLES)
def test_each_credential_pattern_is_refused_without_echo(value: str, kind: str) -> None:
    snap = _snap()
    with pytest.raises(TodoError) as excinfo:
        store.op_update(snap, "alpha", description=f"see {value} for access")
    assert _code(excinfo) == "E_SECRET"
    message = str(excinfo.value)
    assert kind in message and "description" in message
    assert value not in message


def test_ordinary_text_is_not_refused() -> None:
    snap = _snap()
    store.op_update(
        snap,
        "alpha",
        description="A task-manager change; risk-free. Commit 3b32919c0ffee0ddba11aa5e1c3e2f4d5a6b7c8d.",
        links=["https://github.com/joeharris76/todo-db/pull/42", "https://example.com/path?a=b"],
        acceptance=["sk-learn style naming is fine", "user@example.com is an address"],
    )
    store.op_update(snap, "alpha", status="blocked", reason="Waiting on https://example.com/issue/7")


def test_every_supplied_text_field_is_scanned() -> None:
    token = "ghp_" + "a" * 36
    snap = _snap()
    calls = [
        lambda: store.op_create(snap, item_id="new", title=f"t {token}"),
        lambda: store.op_create(snap, item_id="new", title="t", acceptance=[token]),
        lambda: store.op_update(snap, "alpha", context=token),
        lambda: store.op_update(snap, "alpha", links=[f"https://x/{token}"]),
        lambda: store.op_update(snap, "alpha", status="blocked", reason=token),
        lambda: _decide(snap, text=token, source="chat"),
        lambda: _decide(snap, text="ok", source=token),
        lambda: store.op_defer(snap, "alpha", summary=token, reason="r"),
        lambda: store.op_defer(snap, "alpha", summary="s", reason=token),
    ]
    for call in calls:
        with pytest.raises(TodoError) as excinfo:
            call()
        assert _code(excinfo) == "E_SECRET"
        assert token not in str(excinfo.value)
    store.op_defer(snap, "alpha", summary="s", reason="r")
    with pytest.raises(TodoError) as excinfo:
        store.op_dismiss_deferral(snap, "alpha", 1, reason=token)
    assert _code(excinfo) == "E_SECRET"
    take = store.op_take(snap, "alpha", "w1")
    assert take
    with pytest.raises(TodoError) as excinfo:
        store.op_takeover(snap, "alpha", "w2", "w1", f"dead session {token}")
    assert _code(excinfo) == "E_SECRET"


def test_existing_stored_content_is_never_scanned() -> None:
    token = "ghp_" + "a" * 36
    snap = _snap()
    snap.details["alpha"]["description"] = f"older client stored {token}"
    snap.details["alpha"]["acceptance"] = [token]
    store.op_update(snap, "alpha", title="Edited", context="new context")
    store.op_defer(snap, "alpha", summary="Later", reason="r")
    snap.details["alpha"]["deferrals"][0]["reason"] = token
    # Promotion copies the stored reason into the default description.
    store.op_promote_deferral(snap, "alpha", 1)
    assert token in snap.details["alpha-deferral-1"]["description"]


# -- older-client compatibility --


def test_new_keys_load_and_survive_older_code_paths(tmp_path: Path) -> None:
    snap = _snap()
    store.op_update(snap, "alpha", acceptance=["a", "b"], acceptance_met=[1])
    _decide(snap, kind="question", text="Q?")
    _decide(snap, text="A", source="chat", answers=1)
    store.op_update(snap, "alpha", status="blocked", reason="vendor")
    store.save_snapshot(tmp_path, snap)
    raw = json.loads((tmp_path / "items" / "alpha.json").read_text())
    assert {"blocked_reason", "decisions", "acceptance_met"} <= set(raw)
    # Write the raw file back, as an older client would after preserving keys.
    (tmp_path / "items" / "alpha.json").write_text(json.dumps(raw), encoding="utf-8")
    loaded = store.load_snapshot(tmp_path)
    store.op_update(loaded, "alpha", priority="high", description="edited")
    store.op_defer(loaded, "alpha", summary="Later", reason="r")
    store.save_snapshot(tmp_path, loaded)
    again = store.load_snapshot(tmp_path)
    detail = again.details["alpha"]
    assert detail["blocked_reason"] == "vendor"
    assert detail["acceptance_met"] == ["b"]
    assert [row["id"] for row in detail["decisions"]] == [1, 2]


def test_record_decision_over_mcp(tmp_path: Path) -> None:
    remote = tmp_path / "state.git"
    subprocess.run(["git", "init", "--quiet", "--bare", str(remote)], check=True)
    bootstrap(StateRef(remote=str(remote), branch="todo-state"))
    argv = ["--state-remote", str(remote), "--cache-dir", str(tmp_path / "cache"), "--actor", "w1"]
    launch = resolve_launch_config(build_parser().parse_args(argv))

    def payload(result) -> dict:
        return json.loads(result.content[0].text)

    async def go():
        server = build_server(launch)
        async with connect(server, client_info=types.Implementation(name="w1", version="0")) as session:
            assert payload(
                await session.call_tool("create_item", {"id": "q", "title": "Question task", "acceptance": ["x", "y"]})
            )["ok"]
            asked = payload(
                await session.call_tool("record_decision", {"id": "q", "text": "Which?", "kind": "question"})
            )
            assert asked["ok"] and asked["data"]["open_questions"] == [1], asked
            blocked = payload(
                await session.call_tool(
                    "update_item", {"id": "q", "status": "blocked", "reason": "owner", "acceptance_met": [1]}
                )
            )
            assert blocked["ok"], blocked
            refused = payload(await session.call_tool("take", {"id": "q"}))
            assert not refused["ok"] and refused["code"] == "E_STATE"
            took = payload(await session.call_tool("take", {"id": "q", "override_blocked": True}))
            assert took["ok"], took
            shown = payload(await session.call_tool("show_item", {"id": "q"}))["data"]
            assert shown["acceptance_met"] == [1] and shown["next_step"] == "x"
            secret = payload(
                await session.call_tool(
                    "record_decision", {"id": "q", "text": "key AKIAABCDEFGHIJKLMNOP", "source": "chat"}
                )
            )
            assert not secret["ok"] and secret["code"] == "E_SECRET"
            assert "AKIAABCDEFGHIJKLMNOP" not in secret["error"]

    anyio.run(go)


# -- review fixes: concurrency and oversized show_item --


def test_acceptance_rewrite_conflicts_with_a_concurrent_mark(tmp_path: Path, monkeypatch) -> None:
    svc = _svc(tmp_path)
    assert svc.create_item("t", "v1", acceptance=["a", "b"])["ok"]

    def fake_mutate(ref, *, op, summary, worker, op_id, apply, max_retries=5, session=None):
        first = git_backend.read(ref, tmp_path / "c1").snapshot
        apply(first)
        second = git_backend.read(ref, tmp_path / "c2").snapshot
        store.op_update(second, "t", acceptance_met=[0])
        try:
            apply(second)
        except TodoError as exc:
            return git_backend.MutationOutcome(
                ok=False, outcome="conflict", op_id=op_id,
                code=exc.code or "E_STATE", error=str(exc),
            )
        raise AssertionError("retry should have conflicted")

    monkeypatch.setattr(git_backend, "mutate", fake_mutate)
    out = svc.update_item("t", acceptance=["a", "b", "c"])
    assert not out["ok"] and out["code"] == "E_CONFLICT"


def test_oversized_show_item_keeps_session_state(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    assert svc.create_item("big", "Big", acceptance=["first", "second"])["ok"]
    assert svc.record_decision("big", "Ship now?", kind="question")["ok"]

    def seed(snap):
        # Past the size caps, as an older client without them could write.
        # Each section stays under the inline budget so that together they
        # overflow the response cap.
        detail = snap.details["big"]
        detail["description"] = "y" * 5800
        detail["context"] = "z" * 5800
        detail["acceptance"] = ["first", "second", "w" * 5700]
        return {"id": "big"}

    assert git_backend.mutate(svc.ref, op="update", summary="grow big", worker="older", apply=seed).ok
    out = svc.show_item("big")
    assert out["ok"]
    data = out["data"]
    assert "continuation" in data
    assert data["open_questions"] == [1]
    assert data["next_step"] == "first"
    assert data["decisions_continuation"]["field"] == "decisions"
