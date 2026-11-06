"""policy.yaml (design v5 §6): rules per kind, the budget limit, the P2/P3/P4 checks."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from validation.harness import (
    BASIS,
    BOOKKEEPER_AGENT,
    DATA,
    FIXTURES,
    ISAAC,
    JOHN,
    LEONARDO,
    PLANNER_AGENT,
    POLICY,
    VINCENT,
    budget_proposal,
    entries_proposal,
    goals_proposal,
)

from sindarin.ledger import Ledger
from sindarin.policy import (
    Policy,
    check_facts,
    check_proposer,
    check_target,
    format_duration,
    load_policy,
    parse_duration,
)
from sindarin.schema import Kind


@pytest.fixture
def books(tmp_path):
    ledger = Ledger(tmp_path / "books.db")
    ledger.import_dir(DATA / BASIS)
    yield ledger
    ledger.close()


def test_fixture_policy_loads_and_resolves():
    p = load_policy(FIXTURES / "policy.yaml")
    assert p.in_group(LEONARDO, "controllers") and p.in_group(JOHN, "cfo")
    assert p.in_group(ISAAC, "board") and p.groups_of(VINCENT) == []
    assert p.proposer_id("planner") == PLANNER_AGENT and p.role_of(BOOKKEEPER_AGENT) == "bookkeeper"
    assert p.executor_of("@switch-agent-sindarin-ledger:test") == "sindarin-ledger"
    assert [r.kind for r in p.rules] == [Kind.GOALS, Kind.BUDGET, Kind.ENTRIES]
    budget = p.rule("budget")
    assert budget.approver_groups(1600000) == ["board"]
    assert budget.approver_groups(880000) == ["board", "cfo"]
    assert budget.approver_groups(None) == ["board"]
    assert p.rule("goals").also_invite == "controllers" and p.rule("entries").room == "period"


def test_durations():
    assert parse_duration("24h") == 86400 and parse_duration("30m") == 1800
    assert format_duration(172800) == "2d" and format_duration(90) == "90s"
    for bad in ("", "1w", "0h", "-5m"):
        with pytest.raises(ValueError):
            parse_duration(bad)


def _data(mutate):
    import yaml

    data = yaml.safe_load((FIXTURES / "policy.yaml").read_text())
    mutate(data)
    return data


def test_rules_are_validated():
    with pytest.raises(ValidationError, match="approved in the plan room"):
        Policy.model_validate(_data(lambda d: d["rules"][2].__setitem__("room", "planning")))
    with pytest.raises(ValidationError, match="named room"):
        Policy.model_validate(_data(lambda d: d["rules"][0].__setitem__("room", "period")))
    with pytest.raises(ValidationError, match="go together"):
        Policy.model_validate(_data(lambda d: d["rules"][1].pop("approvers_under_limit")))
    with pytest.raises(ValidationError, match="only a budget rule has a limit"):
        Policy.model_validate(
            _data(lambda d: d["rules"][2].update(limit_cents=1, approvers_under_limit="cfo"))
        )
    with pytest.raises(ValidationError, match="is not defined"):
        Policy.model_validate(_data(lambda d: d["rules"][0].__setitem__("also_invite", "exec")))
    with pytest.raises(ValidationError, match="unknown room key"):
        Policy.model_validate(_data(lambda d: d["rooms"].__setitem__("payments", "!x:t")))
    with pytest.raises(ValidationError, match="unknown proposer role"):
        Policy.model_validate(_data(lambda d: d["proposers"].__setitem__("treasury", "@c:t")))
    with pytest.raises(ValidationError, match="currency"):
        Policy.model_validate(_data(lambda d: d["rules"][1].__setitem__("currency", "XYZ")))


def test_check_proposer_is_by_id_and_rule(books):
    budget = budget_proposal(books)
    assert check_proposer(POLICY, budget, PLANNER_AGENT) is None
    assert "only planner may propose" in check_proposer(POLICY, budget, BOOKKEEPER_AGENT)
    assert "only planner may propose" in check_proposer(POLICY, budget, JOHN)
    entries = entries_proposal(books)
    assert check_proposer(POLICY, entries, BOOKKEEPER_AGENT) is None
    assert "only bookkeeper" in check_proposer(POLICY, entries, PLANNER_AGENT)


def test_check_target_and_facts(books):
    goals = goals_proposal(books)
    assert check_target(POLICY, goals) is None
    with pytest.raises(ValueError, match="below the revenue target"):
        goals_proposal(books, cost_cap_cents=9500000)  # the builder runs check_target itself
    budget = budget_proposal(books)
    assert check_target(POLICY, budget) is None
    good = {
        "accounts": ["6000", "6200"],
        "cap_cents": 1600000,
        "basis_status": "open",
        "total_cents": 1600000,
        "digest": books.actuals_digest(BASIS),
    }
    assert check_facts(POLICY, budget, good) is None
    assert "over the cap 15,000.00 USD in GOALS-1" in check_facts(
        POLICY, budget, {**good, "cap_cents": 1500000}
    )
    assert "account 6200 is not in the chart" in check_facts(
        POLICY, budget, {**good, "accounts": ["6000"]}
    )
    assert "no rows" in check_facts(POLICY, budget, {**good, "basis_status": "unknown"})
    entries = entries_proposal(books)
    lines = [x.model_dump() for x in entries.body.lines]
    ok = {"period_status": "open", "budget_id": "BUDGET-1", "budget_lines": lines}
    assert check_facts(POLICY, entries, ok) is None
    assert "no budget is recorded" in check_facts(POLICY, entries, {**ok, "budget_lines": []})
    assert "is BUDGET-2, not BUDGET-1" in check_facts(
        POLICY, entries, {**ok, "budget_id": "BUDGET-2"}
    )
    assert "differ from the budget recorded" in check_facts(
        POLICY, entries, {**ok, "budget_lines": lines[1:]}
    )
    assert "is planned, already" in check_facts(POLICY, entries, {**ok, "period_status": "planned"})
