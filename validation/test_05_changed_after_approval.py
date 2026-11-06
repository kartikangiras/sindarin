"""Test 5 — The ledger changed between proposal and approval (design v5 §5, A6).

BUDGET-1 is pending, built on October's actuals. A late October sale of 3,980.00 USD lands in
the books, typed to sindarin-ledger from chat. Isaac's approval is refused with the difference in plain money, the snapshot is
replaced and the planner is asked to propose again. The re-proposal keeps the id; Isaac
approves; the budget is recorded.
"""

from __future__ import annotations

from sindarin.executor import core
from validation.harness import ISAAC, PLANNING, Harness, write_report


def _posts(fx):
    return [e for e in fx if isinstance(e, core.Post)]


def test_changed_after_proposal(harness: Harness):
    h = harness
    h.run_goals_gate()
    h.propose_budget()
    before = h.budget().snapshot_sha
    fx = h.command("m-sale", "sale S-2026-1055 3980.00 Contoso Ltd", sender=ISAAC, room=PLANNING)
    assert [p.text for p in _posts(fx)] == [
        "**S-2026-1055** Contoso Ltd 3,980.00 USD landed in 2026-10 · 2026-10 revenue is now "
        "93,885.00 USD."
    ]
    assert "sale_booked" in h.audit_events()

    fx = h.approve_budget("m-approve-1", sender=ISAAC)
    texts = [p.text for p in _posts(fx)]
    assert any(
        t.startswith(
            "**BUDGET-1** changed since it was proposed: 2026-10 revenue 89,905.00 USD → "
            "93,885.00 USD, sales rows 13 → 14."
        )
        for t in texts
    ), texts
    assert any(
        t == "**BUDGET-1** not applied — the ledger changed since the proposal "
        "(2026-10 revenue 89,905.00 USD → 93,885.00 USD, sales rows 13 → 14)."
        for t in texts
    )
    a = h.budget()
    assert a.state.value == "pending" and a.snapshot_sha != before and h.recorded() == []
    assert "approved" not in h.audit_events()

    # the planner proposes again from the current rows, in the same thread: same id
    h.propose_budget("m-budget-2", thread="m-budget-1")
    assert h.budget().seq >= 1 and h.budget().state.value == "pending"
    h.approve_budget("m-approve-2", sender=ISAAC)
    assert h.budget().state.value == "recorded" and len(h.recorded()) == 4
    h.assert_no_leaks()
    write_report(
        "test_05_changed_after_approval",
        "Test 5 — The ledger changed between proposal and approval",
        __doc__ or "",
        h,
        "PASS: the yes was refused with the changed revenue, the snapshot replaced, the "
        "re-proposal approved and recorded.",
        [
            "a sale typed from chat lands in the books under the person's id",
            "the refusal names the basis month and both revenues in money",
            "the snapshot is replaced; nothing is recorded on the stale yes",
            "the re-proposal keeps the id and is recorded",
        ],
    )
