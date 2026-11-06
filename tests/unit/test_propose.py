"""The agents' validators (design v5 §4): a fence can only be a well-formed proposal."""

from __future__ import annotations

import pytest
from validation.harness import BASIS, DATA, PERIOD, POLICY

from sindarin.agents.finance import plan_goals, standard_budget_lines
from sindarin.ledger import Ledger
from sindarin.propose import build_budget, build_entries, build_goals, render


@pytest.fixture
def books(tmp_path):
    ledger = Ledger(tmp_path / "books.db")
    ledger.import_dir(DATA / BASIS)
    yield ledger
    ledger.close()


def test_builders_render_the_owning_executor(books):
    goals = build_goals(
        POLICY,
        period=PERIOD,
        basis=BASIS,
        title="Plan November 2026",
        summary="grow on October",
        report=["Revenue 89,905.00 USD"],
        justification="asked",
        **plan_goals(books, PERIOD, BASIS),
    )
    assert render(goals).startswith("@sindarin-switch proposal\n```sindarin-proposal")
    lines = standard_budget_lines(1600000)
    budget = build_budget(POLICY, period=PERIOD, goals="GOALS-1", lines=lines, justification="x")
    assert render(budget).startswith("@sindarin-ledger proposal")
    assert budget.body.total_cents == 1600000 and sum(x["amount_cents"] for x in lines) == 1600000
    entries = build_entries(
        POLICY,
        period=PERIOD,
        goals="GOALS-1",
        budget="BUDGET-1",
        lines=lines,
        memo="Budget November",
        justification="recorded",
    )
    assert entries.rule == "entries" and entries.body.budget == "BUDGET-1"


def test_builders_refuse_in_one_line(books):
    lines = standard_budget_lines(1600000)
    with pytest.raises(ValueError, match="currency"):
        build_budget(
            POLICY, period=PERIOD, goals="GOALS-1", lines=lines, currency="EUR", justification="x"
        )
    with pytest.raises(ValueError, match="below the revenue target"):
        build_goals(
            POLICY,
            period=PERIOD,
            basis=BASIS,
            title="t",
            summary="s",
            report=[],
            revenue_target_cents=100,
            margin_target_bps=0,
            cost_cap_cents=200,
            justification="x",
        )
    with pytest.raises(ValueError, match="unknown budget rule"):
        build_budget(
            POLICY, period=PERIOD, goals="GOALS-1", lines=lines, justification="x", rule="goals"
        )
    with pytest.raises(ValueError, match="pattern"):
        build_entries(
            POLICY,
            period="November",
            goals="GOALS-1",
            budget="BUDGET-1",
            lines=lines,
            memo="m",
            justification="x",
        )
    with pytest.raises(ValueError, match="repeat"):
        build_budget(
            POLICY, period=PERIOD, goals="GOALS-1", lines=lines + lines[:1], justification="x"
        )
