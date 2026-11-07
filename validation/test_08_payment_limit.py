"""Test 8 — The payment limit on the month-end chain (design v5 §6).

The pay rule admits controllers only below 10,000.00 USD; above it only the cfo. Leonardo, a
controller, approves a 14,250.00 USD run and is refused with the amount and the limit; John
approves it. A second plan with two small payables (6,250.00 USD) is Leonardo's to approve.
The group is chosen on the total the executor read from the ledger, never on the fence.
"""

from __future__ import annotations

from sindarin.executor import core
from validation.harness import JOHN, LEONARDO, Harness, write_report


def _post(fx):
    return [e for e in fx if isinstance(e, core.Post)][0]


def test_limit_selects_the_group(harness: Harness):
    h = harness
    h.run_plan_gate()
    fx = h.propose_pay()
    assert "needs one cfo approver in #payments" in _post(fx).text
    fx = h.approve_pay("m-ctrl", sender=LEONARDO)
    assert _post(fx).text == (
        "**PAY-1** not applied — 14,250.00 USD needs the cfo group; controllers may approve up "
        "to 10,000.00 USD."
    )
    assert h.paid() == []
    h.approve_pay("m-cfo", sender=JOHN)
    # the approval hands the run to the bank; the books say sent, not paid, until the bank does
    assert h.pay().state.value == "sent" and len(h.sent()) == 3 and h.paid() == []
    h.confirm_pay("m-bank", sender=JOHN, reference="BANK-7781")
    assert h.pay().state.value == "paid" and len(h.paid()) == 3
    assert h.pay().result()["reference"] == "BANK-7781"
    h.assert_no_leaks()
    write_report(
        "test_08_payment_limit",
        "Test 8 — The payment limit",
        __doc__ or "",
        h,
        "PASS: the controller was refused above the limit with the amount read from the ledger; "
        "the CFO's approval sent the run to the bank and the bank's confirmation paid it.",
        [
            "the pending line names the group the total requires",
            "a controller above the limit is refused with the amount and the limit",
            "the cfo's approval sends the run; the bank's confirmation pays it",
        ],
    )


def test_under_the_limit_a_controller_may_approve(harness: Harness):
    h = harness
    h.propose_close(payables=["INV-1042", "INV-1043"])
    h.approve_close()
    h.sweep()
    fx = h.propose_pay(invoices=["INV-1042", "INV-1043"])
    assert "one cfo or controllers approver (under 10,000.00 USD)" in _post(fx).text
    h.approve_pay("m-ctrl-ok", sender=LEONARDO)
    assert h.pay().state.value == "sent" and h.sent() == ["INV-1042", "INV-1043"]
    h.confirm_pay("m-bank-ok", sender=LEONARDO)
    assert h.pay().state.value == "paid" and h.paid() == ["INV-1042", "INV-1043"]
