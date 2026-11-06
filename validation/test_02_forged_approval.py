"""Test 2 — Forged approval (design v5 §6).

`planner` (the proposing agent) posts `@sindarin-ledger approve BUDGET-1`; Vincent (no group)
posts the same; an impostor whose display name is "Isaac" but whose id is not Isaac's posts the
same. Must observe: refused with the agent template and the not-approver template; the ledger
is not written. Then Isaac, by participant id, approves and the budget is recorded.
"""

from __future__ import annotations

from sindarin.executor import core
from validation.harness import ISAAC, PLANNER_AGENT, PLANNING, VINCENT, Harness, write_report


def _post(fx):
    return [e for e in fx if isinstance(e, core.Post)][0]


def _all(fx):
    return [e for e in fx if isinstance(e, core.Post)]


def test_forged_approval(harness: Harness):
    h = harness
    h.run_goals_gate()
    h.propose_budget()
    assert h.budget().state.value == "pending"

    fx = h.approve_budget("m-forge-1", sender=PLANNER_AGENT)
    assert _post(fx).text == "**BUDGET-1** not applied — @planner is an agent, not an approver."
    assert _post(fx).room_id == PLANNING and _post(fx).thread_id == "m-budget-1"

    fx = h.approve_budget("m-forge-2", sender=VINCENT)
    assert _post(fx).text == "**BUDGET-1** not applied — Vincent is not in the board group."

    fx = h.say(
        PLANNING,
        "@switch-user-imposter:test",
        "@sindarin-ledger approve BUDGET-1",
        mid="m-forge-3",
        name="Isaac",
        thread="m-budget-1",
    )
    assert _post(fx).text == "**BUDGET-1** not applied — Isaac is not in the board group."
    assert h.recorded() == [] and h.budget().state.value == "pending"
    refusals = [e for e in h.audit_events() if e.startswith("refused_approval")]
    assert refusals == [
        "refused_approval:agent",
        "refused_approval:not_approver",
        "refused_approval:not_approver",
    ]

    fx = h.approve_budget("m-real", sender=ISAAC)
    assert h.budget().state.value == "recorded"
    assert h.recorded() == ["engineering", "general-admin", "marketing", "sales"]
    assert any(
        p.text.startswith("**BUDGET-1** approved by Isaac · recorded 16,000.00 USD")
        for p in _all(fx)
    )
    h.assert_no_leaks()
    write_report(
        "test_02_forged_approval",
        "Test 2 — Forged approval",
        __doc__ or "",
        h,
        "PASS: three forged approvals refused by participant id, nothing recorded; Isaac's "
        "approval recorded the budget once.",
        [
            "the proposing agent is refused as an agent",
            "a person outside the board group is refused by id",
            "an impostor with the right display name is refused by id",
            "the ledger is written only after the real approval",
        ],
    )
