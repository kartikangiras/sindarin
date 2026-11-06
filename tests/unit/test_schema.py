"""Proposal v3 bodies, the snapshot hash, states and the status fence (design v5 §5)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from validation.harness import (
    BASIS,
    DATA,
    PERIOD,
    budget_proposal,
    entries_proposal,
    goals_proposal,
)

from sindarin.ledger import Ledger
from sindarin.schema import (
    PROPOSAL_FENCE,
    Artifact,
    Kind,
    Proposal,
    Snapshot,
    State,
    StatusFence,
    canonical_json,
    extract_fences,
    kind_of_id,
    make_snapshot,
    money,
    percent,
    previous_period,
    render_fence,
)


@pytest.fixture
def books(tmp_path):
    ledger = Ledger(tmp_path / "books.db")
    ledger.import_dir(DATA / BASIS)
    yield ledger
    ledger.close()


def test_kinds_ids_money_and_periods():
    assert kind_of_id("GOALS-1") is Kind.GOALS and kind_of_id("BUDGET-7") is Kind.BUDGET
    assert kind_of_id("ENTRIES-2") is Kind.ENTRIES and kind_of_id("PR-41") is None
    assert money(1600000) == "16,000.00 USD" and money(-5, "EUR") == "-0.05 EUR"
    assert percent(8316) == "83.2%" and previous_period("2026-01") == "2025-12"
    assert previous_period("2026-11") == "2026-10"


def test_proposals_round_trip_and_fit_their_kind(books):
    goals = goals_proposal(books)
    assert goals.kind is Kind.GOALS and goals.target == f"plan-{PERIOD}"
    assert not goals.snapshot_hint and goals.body.basis == BASIS
    assert goals.body.revenue_target_cents == 9500000 and goals.body.cost_cap_cents == 1600000
    assert goals.body.margin_target_bps == 8316
    budget = budget_proposal(books)
    assert budget.kind is Kind.BUDGET and budget.target == f"budget:{PERIOD}"
    assert budget.body.total_cents == 1600000 and len(budget.snapshot_hint) == 64
    assert [x.department for x in budget.body.lines] == [
        "engineering",
        "sales",
        "marketing",
        "general-admin",
    ]
    entries = entries_proposal(books)
    assert entries.kind is Kind.ENTRIES and entries.target == f"period:{PERIOD}"
    assert entries.body.budget == "BUDGET-1" and entries.body.total_cents == 1600000
    for p in (goals, budget, entries):
        again = Proposal.model_validate_json(p.model_dump_json(by_alias=True))
        assert again == p and extract_fences(p.render(), PROPOSAL_FENCE)[0] == p.canonical()


def test_proposals_refuse_what_the_executor_must_never_see(books):
    data = json.loads(goals_proposal(books).canonical())
    data["extra"] = 1
    with pytest.raises(ValidationError):
        Proposal.model_validate(data)
    data = json.loads(budget_proposal(books).canonical())
    data["body"]["total_cents"] += 1  # the fence's total must be the sum of its lines
    with pytest.raises(ValidationError, match="not the sum of the lines"):
        Proposal.model_validate(data)
    data = json.loads(budget_proposal(books).canonical())
    data["target"] = "budget:2026-12"  # target and period disagree
    with pytest.raises(ValidationError, match="budget target"):
        Proposal.model_validate(data)
    data = json.loads(budget_proposal(books).canonical())
    data["body"]["lines"].append(dict(data["body"]["lines"][0]))  # a department twice
    with pytest.raises(ValidationError, match="repeat"):
        Proposal.model_validate(data)
    data = json.loads(goals_proposal(books).canonical())
    data["snapshot_hint"] = "a" * 64  # goals bind to policy, not to a ledger read
    with pytest.raises(ValidationError, match="no snapshot_hint"):
        Proposal.model_validate(data)
    data = json.loads(goals_proposal(books).canonical())
    data["body"]["basis"] = "2026-12"  # the basis comes before the period
    with pytest.raises(ValidationError, match="basis period comes before"):
        Proposal.model_validate(data)
    data = json.loads(entries_proposal(books).canonical())
    data["platform"] = "switch"
    with pytest.raises(ValidationError, match="platform"):
        Proposal.model_validate(data)


def test_snapshot_hash_binds_proposal_and_facts(books):
    p = budget_proposal(books)
    facts = {"total_cents": 1600000, "digest": "d" * 64}
    s1 = make_snapshot("BUDGET-1", "@switch-agent-planner:test", p, facts)
    s2 = make_snapshot("BUDGET-1", "@switch-agent-planner:test", p, {**facts, "total_cents": 1})
    assert s1.hash != s2.hash and len(s1.hash) == 64
    assert Snapshot.model_validate_json(s1.canonical()).hash == s1.hash
    assert canonical_json({"b": 1, "a": [2, 3]}) == '{"a":[2,3],"b":1}'


def test_states_and_status_fence(books):
    assert State.RECORDED.value == "recorded" and State.PLANNED.value == "planned"
    p = budget_proposal(books)
    snap = make_snapshot("BUDGET-1", "@p:t", p, {})
    now = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
    a = Artifact(
        id="BUDGET-1",
        kind=Kind.BUDGET,
        rule="budget",
        room_id="!r:t",
        thread_id="m",
        proposal_message_id="m",
        proposer_id="@p:t",
        proposer_name="planner",
        proposal_json=p.model_dump_json(by_alias=True),
        snapshot_json=snap.canonical(),
        snapshot_sha=snap.hash,
        created_at=now,
        expires_at=now,
    )
    assert a.period == PERIOD and a.target == f"budget:{PERIOD}" and a.facts() == {}
    fence = StatusFence.from_artifact(a, now, State.RECORDED)
    text = fence.render()
    assert text.startswith("```sindarin-status") and '"state":"recorded"' in text
    assert "@p:t" not in text and render_fence("x", {"a": 1}) == '```x\n{"a":1}\n```'
