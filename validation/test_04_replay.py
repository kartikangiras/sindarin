"""Test 4 — Replay (design v5 §6).

Isaac approves BUDGET-1; the ledger records once. Isaac approves again: "not pending". The
approval message is redelivered with the same id: no effects at all. Must observe: one record,
one `applied` audit row, four lines recorded exactly once.
"""

from __future__ import annotations

from sindarin.executor import core
from validation.harness import ISAAC, PLANNING, Harness, write_report


def _post(fx):
    return [e for e in fx if isinstance(e, core.Post)][0]


def test_replay(harness: Harness):
    h = harness
    h.run_goals_gate()
    h.propose_budget()
    h.approve_budget("m-approve-1", sender=ISAAC)
    assert h.budget().state.value == "recorded" and len(h.recorded()) == 4

    fx = h.approve_budget("m-approve-2", sender=ISAAC)
    assert _post(fx).text == "**BUDGET-1** is recorded, not pending."

    fx = h.say(
        PLANNING, ISAAC, "@sindarin-ledger approve BUDGET-1", mid="m-approve-1", thread="m-budget-1"
    )
    assert fx == [], "a redelivered message id produces nothing"

    assert len(h.recorded()) == 4
    assert h.audit_events().count("applied") == 1
    h.assert_no_leaks()
    write_report(
        "test_04_replay",
        "Test 4 — Replay",
        __doc__ or "",
        h,
        "PASS: one record; the second yes answered not pending; the redelivery was ignored.",
        [
            "one `applied` row",
            "a second approve answers not pending",
            "a redelivery has no effects",
        ],
    )
