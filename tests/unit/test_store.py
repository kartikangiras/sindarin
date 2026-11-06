"""The executors' SQLite state (design v5 §5): per-kind state machine, ids, messages seen once,
approvals, actions and the audit log. The same file is read by the other executor."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from validation.harness import BASIS, DATA, budget_proposal, entries_proposal, goals_proposal

from sindarin.ledger import Ledger
from sindarin.schema import TERMINAL_STATES, Artifact, AuditEntry, Kind, State, make_snapshot
from sindarin.store import TRANSITIONS, IllegalTransition, MessageRecord, Store, UnknownArtifact

T0 = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


@pytest.fixture
def books(tmp_path):
    ledger = Ledger(tmp_path / "books.db")
    ledger.import_dir(DATA / BASIS)
    yield ledger
    ledger.close()


def _artifact(books: Ledger, kind: Kind, aid: str, *, thread: str = "m-1") -> Artifact:
    p = {
        Kind.GOALS: lambda: goals_proposal(books),
        Kind.BUDGET: lambda: budget_proposal(books),
        Kind.ENTRIES: lambda: entries_proposal(books),
    }[kind]()
    snap = make_snapshot(aid, "@switch-agent-x:test", p, {"k": 1})
    return Artifact(
        id=aid,
        kind=kind,
        rule=kind.value,
        room_id="!room:test",
        thread_id=thread,
        proposal_message_id=f"{thread}-p",
        proposer_id="@switch-agent-x:test",
        proposer_name="x",
        proposal_json=p.model_dump_json(by_alias=True),
        snapshot_json=snap.canonical(),
        snapshot_sha=snap.hash,
        created_at=T0,
        expires_at=T0 + timedelta(hours=24),
    )


def test_transition_table_is_closed_and_terminal():
    assert set(TRANSITIONS) == set(Kind)
    for kind, table in TRANSITIONS.items():
        for state, nexts in table.items():
            assert state not in TERMINAL_STATES or not nexts, (kind, state)
    assert TRANSITIONS[Kind.BUDGET][State.EXECUTING] == {State.RECORDED, State.RECORD_FAILED}
    assert TRANSITIONS[Kind.ENTRIES][State.EXECUTING] == {State.PLANNED, State.PLAN_FAILED}
    assert TRANSITIONS[Kind.GOALS][State.OPEN] == {State.DONE}


def test_ids_are_numbered_per_kind(books, tmp_path):
    s = Store(tmp_path / "s.db")
    assert s.peek_next_id(Kind.GOALS) == "GOALS-1" and s.peek_next_id(Kind.BUDGET) == "BUDGET-1"
    s.insert_artifact(_artifact(books, Kind.GOALS, "GOALS-1"))
    assert s.peek_next_id(Kind.GOALS) == "GOALS-2" and s.peek_next_id(Kind.BUDGET) == "BUDGET-1"
    with pytest.raises(ValueError, match="must be GOALS-2"):
        s.insert_artifact(_artifact(books, Kind.GOALS, "GOALS-7"))
    s.insert_artifact(_artifact(books, Kind.BUDGET, "BUDGET-1", thread="m-2"))
    assert [a.id for a in s.all()] == ["GOALS-1", "BUDGET-1"]
    assert [a.id for a in s.in_states(State.PENDING, kind=Kind.BUDGET)] == ["BUDGET-1"]
    assert [a.id for a in s.by_thread("m-1", State.PENDING)] == ["GOALS-1"]
    s.close()


def test_state_machine_is_enforced(books, tmp_path):
    s = Store(tmp_path / "s.db")
    s.insert_artifact(_artifact(books, Kind.BUDGET, "BUDGET-1"))
    with pytest.raises(IllegalTransition):
        s.transition("BUDGET-1", State.RECORDED)
    s.transition(
        "BUDGET-1", State.APPROVED, approver_id="@u:t", approver_name="Isaac", approved_at=T0
    )
    s.transition("BUDGET-1", State.EXECUTING, attempts=1)
    s.transition("BUDGET-1", State.RECORD_FAILED, error="boom", failed_at=T0)
    s.transition("BUDGET-1", State.EXECUTING, attempts=2, error=None)
    with pytest.raises(ValueError):
        s.transition("BUDGET-1", State.RECORDED, kind=Kind.ENTRIES)  # id, kind, seq are immutable
    s.transition("BUDGET-1", State.RECORDED, result_json='{"lines": 4}')
    a = s.get("BUDGET-1")
    assert a.state is State.RECORDED and a.attempts == 2 and a.result() == {"lines": 4}
    with pytest.raises(IllegalTransition):
        s.transition("BUDGET-1", State.PENDING)
    with pytest.raises(UnknownArtifact):
        s.transition("BUDGET-9", State.APPROVED)
    s.close()


def test_snapshot_update_bumps_seq_and_approvals_are_recorded(books, tmp_path):
    s = Store(tmp_path / "s.db")
    a = _artifact(books, Kind.BUDGET, "BUDGET-1")
    s.insert_artifact(a)
    s.update_snapshot(
        "BUDGET-1",
        proposal_json=a.proposal_json,
        snapshot_json=a.snapshot_json,
        snapshot_sha="n" * 64,
        proposal_message_id="m-1-p2",
        expires_at=T0 + timedelta(hours=48),
    )
    b = s.get("BUDGET-1")
    assert b.seq == 2 and b.snapshot_sha == "n" * 64 and b.expires_at > a.expires_at
    s.record_approval(
        "BUDGET-1",
        approver_id="@u:t",
        room_id="!room:test",
        message_id="m-ok",
        snapshot_sha=b.snapshot_sha,
        decision="approve",
        note=None,
    )
    assert [r["decision"] for r in s.approvals_for("BUDGET-1")] == ["approve"]
    s.record_action(
        "BUDGET-1", "record_budget", 1, T0, {"ok": True, "data": {}, "error": None}, True
    )
    assert len(s.actions_for("BUDGET-1")) == 1
    s.close()


def test_messages_are_seen_once_and_audit_is_appended(tmp_path):
    s = Store(tmp_path / "s.db")
    m = MessageRecord("m-1", "!room:test", "@u:t", "John", None, 0, True)
    s.record_message(m)
    assert s.get_message("m-1").sender_name == "John" and not s.is_seen("m-1")
    s.mark_seen("m-1")
    assert s.is_seen("m-1") and not s.is_seen("m-2")
    s.set_value("stage:budget:GOALS-1", "2026-11-05")
    assert s.get_value("stage:budget:GOALS-1") == "2026-11-05" and s.get_value("nope") is None
    e = s.append_audit(
        AuditEntry(ts=T0, event="approved", artifact_id="BUDGET-1", actor_name="Isaac")
    )
    assert e.id is not None and [x.event for x in s.audit_all()] == ["approved"]
    assert s.audit_for("BUDGET-1")[0].actor_name == "Isaac"
    assert s.audit_last(1)[0].event == "approved"
    s.close()


def test_peer_reads_the_same_file_read_only(books, tmp_path):
    path: Path = tmp_path / "ledger.db"
    s = Store(path)
    s.insert_artifact(_artifact(books, Kind.BUDGET, "BUDGET-1"))
    peer = Store(path, readonly=True)
    assert [a.id for a in peer.all()] == ["BUDGET-1"]
    with pytest.raises(sqlite3.OperationalError):
        peer.set_value("k", "v")
    peer.close()
    s.close()
