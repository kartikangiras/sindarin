"""DDD §11: artifacts with kind-specific state machines, ids, snapshots, approvals (replay
guard), reviews, actions, audit, read-only peer access."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from validation.harness import REPO, pr_proposal, release_proposal, review_fence, spec_proposal

from sindarin.schema import (
    REVIEW_FENCE,
    TERMINAL_STATES,
    Artifact,
    AuditEntry,
    Kind,
    Review,
    State,
    extract_fences,
    make_snapshot,
)
from sindarin.store import TRANSITIONS, IllegalTransition, MessageRecord, Store, UnknownArtifact

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


def _proposal(kind: Kind):
    if kind is Kind.SPEC:
        return spec_proposal()
    if kind is Kind.PR:
        return pr_proposal()
    return release_proposal(target_sha="a" * 40)


def _artifact(store: Store, kind: Kind = Kind.PR, **over) -> Artifact:
    p = _proposal(kind)
    aid = "PR-41" if kind is Kind.PR else store.peek_next_id(kind)
    snap = make_snapshot(aid, "@agent:t", p, {"head_sha": "a" * 40} if kind is Kind.PR else {})
    base = dict(
        id=aid,
        kind=kind,
        rule={Kind.SPEC: "spec", Kind.PR: "merge", Kind.RELEASE: "release"}[kind],
        room_id="!room:t",
        thread_id=over.pop("thread_id", f"m-{aid}"),
        proposal_message_id="m-prop",
        proposer_id="@agent:t",
        proposer_name="agent",
        proposal_json=p.model_dump_json(by_alias=True),
        snapshot_json=snap.canonical(),
        snapshot_sha=snap.hash,
        created_at=NOW,
        expires_at=NOW + timedelta(hours=24),
    )
    base.update(over)
    return store.insert_artifact(Artifact.model_validate(base))


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


def test_ids_per_kind_and_pr_from_github(store):
    assert store.peek_next_id(Kind.SPEC) == "SPEC-1" and store.peek_next_id(Kind.RELEASE) == "REL-1"
    a = _artifact(store, Kind.SPEC)
    b = _artifact(store, Kind.SPEC)
    assert (a.id, b.id) == ("SPEC-1", "SPEC-2") and store.peek_next_id(Kind.SPEC) == "SPEC-3"
    assert _artifact(store, Kind.RELEASE).id == "REL-1"
    pr = _artifact(store, Kind.PR)
    assert pr.id == "PR-41"
    with pytest.raises(ValueError, match="already exists"):
        _artifact(store, Kind.PR)
    with pytest.raises(ValueError, match="must be SPEC-3"):
        _artifact(store, Kind.SPEC, id="SPEC-9")
    with pytest.raises(ValueError):
        store.peek_next_id(Kind.PR)


def _path_to(kind: Kind, state: State) -> list[State]:
    run = {Kind.SPEC: State.OPENING, Kind.PR: State.EXECUTING, Kind.RELEASE: State.EXECUTING}[kind]
    ok = {Kind.SPEC: State.OPEN, Kind.PR: State.MERGED, Kind.RELEASE: State.PUBLISHED}[kind]
    bad = {
        Kind.SPEC: State.OPEN_FAILED,
        Kind.PR: State.MERGE_FAILED,
        Kind.RELEASE: State.PUBLISH_FAILED,
    }[kind]
    paths = {
        State.PENDING: [],
        State.CHANGES_REQUESTED: [State.CHANGES_REQUESTED],
        State.DENIED: [State.DENIED],
        State.EXPIRED: [State.EXPIRED],
        State.APPROVED: [State.APPROVED],
        run: [State.APPROVED, run],
        ok: [State.APPROVED, run, ok],
        bad: [State.APPROVED, run, bad],
        State.FAILED: [State.APPROVED, run, bad, State.FAILED],
        State.DONE: [State.APPROVED, run, ok, State.DONE],
        State.RELEASED: [State.APPROVED, run, ok, State.DONE, State.RELEASED],
    }
    return paths[state]


@pytest.mark.parametrize("kind", list(Kind))
def test_every_legal_transition(store, kind):
    for src, dsts in TRANSITIONS[kind].items():
        for dst in dsts:
            s = Store(":memory:")
            a = _artifact(s, kind)
            for st in _path_to(kind, src):
                s.transition(a.id, st)
            got = s.transition(a.id, dst)
            assert got.state is dst and got.seq == len(_path_to(kind, src)) + 2
            s.close()


@pytest.mark.parametrize("kind", list(Kind))
def test_every_illegal_transition_raises(store, kind):
    for src in TRANSITIONS[kind]:
        for dst in State:
            if dst in TRANSITIONS[kind][src]:
                continue
            s = Store(":memory:")
            a = _artifact(s, kind)
            for st in _path_to(kind, src):
                s.transition(a.id, st)
            with pytest.raises(IllegalTransition):
                s.transition(a.id, dst)
            assert s.get(a.id).state is src
            s.close()


def test_terminal_states_are_dead_ends():
    for kind in Kind:
        for t in TERMINAL_STATES:
            assert not TRANSITIONS[kind].get(t, frozenset())


def test_transition_fields_and_protection(store):
    a = _artifact(store)
    got = store.transition(
        a.id, State.APPROVED, approver_id="@u:t", approver_name="Isaac", approved_at=NOW
    )
    assert got.approver_id == "@u:t" and got.approved_at == NOW and got.seq == 2
    with pytest.raises(ValueError):
        store.transition(a.id, State.EXECUTING, token="nope")
    with pytest.raises(ValueError):
        store.transition(a.id, State.EXECUTING, seq=9)
    with pytest.raises(UnknownArtifact):
        store.transition("PR-99", State.APPROVED)


def test_update_snapshot_keeps_state_clears_approver_bumps_seq(store):
    a = _artifact(store)
    store.transition(
        a.id, State.CHANGES_REQUESTED, approver_id="@u:t", approver_name="Isaac", note="x"
    )
    p = pr_proposal(title="revised")
    snap = make_snapshot(a.id, "@agent:t", p, {"head_sha": "b" * 40})
    got = store.update_snapshot(
        a.id,
        proposal_json=p.model_dump_json(by_alias=True),
        snapshot_json=snap.canonical(),
        snapshot_sha=snap.hash,
        proposal_message_id="m-prop-2",
        expires_at=NOW + timedelta(days=2),
    )
    assert got.state is State.CHANGES_REQUESTED and got.seq == 3
    assert got.approver_id is None and got.snapshot_sha == snap.hash
    assert got.proposal_message_id == "m-prop-2" and got.expires_at == NOW + timedelta(days=2)
    assert got.proposal().body.title == "revised"


def test_by_thread_and_in_states(store):
    a = _artifact(store, Kind.SPEC, thread_id="t1")
    b = _artifact(store, Kind.SPEC, thread_id="t1")
    store.transition(a.id, State.DENIED)
    assert [x.id for x in store.by_thread("t1")] == [a.id, b.id]
    assert [x.id for x in store.by_thread("t1", State.PENDING)] == [b.id]
    pr = _artifact(store, Kind.PR)
    assert [x.id for x in store.in_states(State.PENDING)] == [b.id, pr.id]
    assert [x.id for x in store.in_states(State.PENDING, kind=Kind.PR)] == [pr.id]
    assert store.in_states() == [] and len(store.all()) == 3


def test_new_artifacts_must_start_pending(store):
    with pytest.raises(IllegalTransition):
        _artifact(store, Kind.SPEC, state=State.OPEN)


def test_approvals_replay_guard(store):
    a = _artifact(store)
    assert store.record_approval(
        a.id,
        approver_id="@u:t",
        room_id="!r",
        message_id="m1",
        snapshot_sha="s",
        decision="approve",
    )
    assert not store.record_approval(
        a.id,
        approver_id="@u:t",
        room_id="!r",
        message_id="m1",
        snapshot_sha="s",
        decision="approve",
    )
    assert store.record_approval(
        a.id,
        approver_id="@u:t",
        room_id="!r",
        message_id="m2",
        snapshot_sha="s",
        decision="changes",
        note="n",
    )
    rows = store.approvals_for(a.id)
    assert [r["decision"] for r in rows] == ["approve", "changes"] and rows[1]["note"] == "n"


def test_reviews_latest(store):
    a = _artifact(store)
    r1 = Review.model_validate_json(
        extract_fences(review_fence(head_sha="a" * 40, met=[True, False]), REVIEW_FENCE)[0]
    )
    r2 = Review.model_validate_json(
        extract_fences(review_fence(head_sha="b" * 40), REVIEW_FENCE)[0]
    )
    assert store.latest_review(a.id) is None
    store.record_review(a.id, r1, "@rev:t", "reviewer")
    store.record_review(a.id, r2, "@rev:t", "reviewer")
    review, who, name = store.latest_review(a.id)
    assert (
        review.head_sha == "b" * 40 and review.met == 4 and who == "@rev:t" and name == "reviewer"
    )


def test_actions_and_audit(store):
    a = _artifact(store)
    store.record_action(a.id, "merge_pr", 1, NOW, {"ok": False, "error": "502"}, False)
    store.record_action(a.id, "merge_pr", 2, NOW, {"ok": True}, True)
    acts = store.actions_for(a.id)
    assert [x["attempt"] for x in acts] == [1, 2] and acts[1]["verified"] is True
    for i in range(3):
        store.append_audit(
            AuditEntry(
                ts=NOW + timedelta(seconds=i), artifact_id=a.id, event=f"e{i}", details={"i": i}
            )
        )
    rows = store.audit_last(2)
    assert [r.event for r in rows] == ["e1", "e2"] and rows[0].details == {"i": 1}
    assert [r.event for r in store.audit_for(a.id)] == ["e0", "e1", "e2"]


def test_messages_seen_and_cursor(store):
    m = MessageRecord("m1", "!r", "@u:t", "Isaac", None, 1_700_000_000_000, False)
    store.record_message(m)
    store.record_message(MessageRecord("m1", "x", "y", "z", "q", 1, True))
    assert store.get_message("m1") == m and store.get_message("nope") is None
    assert store.mark_seen("m1") and not store.mark_seen("m1") and store.is_seen("m1")
    store.set_value("cursor", "4813")
    store.set_value("cursor", "4814")
    assert store.get_value("cursor") == "4814"


def test_crash_recovery_and_readonly_peer(tmp_path: Path):
    path = tmp_path / "github.db"
    s = Store(path)
    a = _artifact(s)
    s.transition(a.id, State.APPROVED, approved_at=NOW)
    s.transition(a.id, State.EXECUTING)
    s.close()  # "crash" after the pre-network write
    s2 = Store(path)
    assert [x.id for x in s2.in_states(State.EXECUTING)] == [a.id]
    peer = Store(path, readonly=True)
    assert [x.id for x in peer.all()] == [a.id]
    with pytest.raises(sqlite3.OperationalError):
        peer.set_value("k", "v")
    peer.close()
    s2.close()
    assert REPO in a.target
