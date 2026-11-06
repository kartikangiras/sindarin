"""Test 9 — A payable changed between proposal and approval (design v5 §5, A6).

PAY-1 is pending at 14,250.00 USD. INV-1042 is amended in the ledger to 4,900.00 USD. John's
approval is refused with the difference in plain money, the snapshot is replaced and the
treasury agent is asked to propose again. The re-proposal keeps the id; John approves; the
ledger pays 14,350.00 USD. A run that smuggles an invoice outside the plan is refused first.
"""

from __future__ import annotations

from sindarin.executor import core
from validation.harness import (
    JOHN,
    PAYMENTS,
    TREASURY_AGENT,
    Harness,
    pay_proposal,
    proposal_text,
    write_report,
)


def _posts(fx):
    return [e for e in fx if isinstance(e, core.Post)]


def test_changed_after_proposal(harness: Harness):
    h = harness
    h.run_plan_gate()
    rows = [
        {"invoice": r["invoice"], "vendor": r["vendor"], "amount_cents": int(r["amount_cents"])}
        for r in h.books.open_payables("2026-10")
    ] + [{"invoice": "INV-0040", "vendor": "Vincent Ltd", "amount_cents": 999900}]
    fx = h.say(
        PAYMENTS,
        TREASURY_AGENT,
        proposal_text(pay_proposal(h.books, run=rows, digest=False)),
        mid="m-pay-injected",
    )
    assert "INV-0040 not in the approved plan CLOSE-1" in _posts(fx)[0].text
    h.propose_pay()
    before = h.pay().snapshot_sha
    h.books.amend_payable("INV-1042", 490000)
    fx = h.approve_pay("m-approve-1", sender=JOHN)
    texts = [p.text for p in _posts(fx)]
    assert any(
        t.startswith(
            "**PAY-1** changed since it was proposed: INV-1042 4,800.00 USD → 4,900.00 USD."
        )
        for t in texts
    ), texts
    a = h.pay()
    assert a.state.value == "pending" and a.snapshot_sha != before and h.paid() == []
    h.propose_pay("m-pay-2", thread="m-pay-1")
    assert h.pay().proposal().body.total_cents == 1435000
    h.approve_pay("m-approve-2", sender=JOHN)
    assert h.pay().state.value == "paid" and len(h.paid()) == 3
    h.assert_no_leaks()
    write_report(
        "test_09_payable_changed",
        "Test 9 — A payable changed between proposal and approval",
        __doc__ or "",
        h,
        "PASS: the smuggled run was refused; the yes on the changed payable was refused with both "
        "amounts; the re-proposal was paid at the new total.",
        [
            "a run naming an invoice outside the plan is refused",
            "the refusal names the invoice and both amounts in money",
            "the re-proposal keeps the id and is paid at the new total",
        ],
    )
