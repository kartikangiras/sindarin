"""Test 3 — The approval limit (design v5 §6).

The budget rule admits the cfo alone only up to 10,000.00 USD; above it the board. John, the
CFO, approves a 16,000.00 USD budget and is refused with the amount and the limit; Isaac, on the
board, approves it. A second month's budget under the limit is John's to approve. The group is
chosen on the total the executor read, never on the fence.
"""

from __future__ import annotations

from sindarin.executor import core
from validation.harness import ISAAC, JOHN, Harness, write_report


def _post(fx):
    return [e for e in fx if isinstance(e, core.Post)][0]


def test_limit_selects_the_group(harness: Harness):
    h = harness
    h.run_goals_gate()
    fx = h.propose_budget()
    assert "needs one board approver in #planning" in _post(fx).text
    fx = h.approve_budget("m-cfo", sender=JOHN)
    assert _post(fx).text == (
        "**BUDGET-1** not applied — 16,000.00 USD needs the board group; cfo may approve up "
        "to 10,000.00 USD."
    )
    assert h.recorded() == []
    h.approve_budget("m-board", sender=ISAAC)
    assert h.budget().state.value == "recorded" and len(h.recorded()) == 4
    h.assert_no_leaks()
    write_report(
        "test_03_limit",
        "Test 3 — The approval limit",
        __doc__ or "",
        h,
        "PASS: the CFO was refused above the limit with the amount the executor read; the "
        "board's approval recorded the budget.",
        [
            "the pending line names the group the total requires",
            "the cfo above the limit is refused with the amount and the limit",
            "the board's approval records",
        ],
    )


def test_under_the_limit_the_cfo_approves_alone(harness: Harness):
    h = harness
    # goals with a cap under the limit: the budget's lines sum to 9,000.00 USD
    h.propose_goals(cost_cap_cents=900000)
    h.approve_goals()
    h.sweep()
    fx = h.propose_budget(cap_cents=900000)
    assert "one board or cfo approver (under 10,000.00 USD)" in _post(fx).text
    h.approve_budget("m-cfo-ok", sender=JOHN)
    assert h.budget().state.value == "recorded"
    assert h.budget().proposal().body.total_cents == 880000  # the lines round down to hundreds
