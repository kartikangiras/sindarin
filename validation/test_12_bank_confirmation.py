"""Test 12 — The bank's confirmation is a person's word, in the right room, once.

An approved payment run is sent to the bank, not paid: the file is in the thread, the books say
sent, the lock waits. `paid PAY-1 <reference>` is refused before the run is sent, from the
treasury agent, from Vincent (in no group), from the period room, and a second time once it is
paid. John's confirmation books the payments under the bank's reference and the lock stage line
follows.
"""

from __future__ import annotations

from sindarin.executor import core
from validation.harness import (
    CLOSE_ROOM,
    JOHN,
    TREASURY_AGENT,
    VINCENT,
    Harness,
    write_report,
)


def _post(fx):
    return [e for e in fx if isinstance(e, core.Post)][0]


def test_the_bank_confirms_once_through_a_person(harness: Harness):
    h = harness
    h.run_plan_gate()
    h.propose_pay()
    fx = h.confirm_pay("m-early", sender=JOHN)
    assert _post(fx).text == "**PAY-1** not confirmed — it is pending, not sent to the bank."
    fx = h.approve_pay(sender=JOHN)
    texts = [e.text for e in fx if isinstance(e, core.Post)]
    assert texts[0].startswith(
        "**PAY-1** approved by John · sent to bank file · 3 payables · 14,250.00 USD · "
        "file **PAY-1.csv**"
    )
    assert "```csv" in texts[1] and "PAY-1/INV-1042,Amazon Web Services,4800.00,USD" in texts[1]
    assert "`@sindarin-ledger paid PAY-1 <bank reference>`" in texts[1]
    assert h.sent() == ["INV-1042", "INV-1043", "INV-1044"] and h.paid() == []
    assert h.books.journal_by_source("PAY-1") == []
    fx = h.confirm_pay("m-agent", sender=TREASURY_AGENT)
    assert "agents and executors do not confirm payments" in _post(fx).text
    fx = h.confirm_pay("m-vincent", sender=VINCENT)
    assert (
        _post(fx).text
        == "**PAY-1** not confirmed — Vincent is not in the cfo or controllers group."
    )
    fx = h.confirm_pay("m-room", sender=JOHN, room=CLOSE_ROOM, thread=None)
    assert _post(fx).text == "**PAY-1** not confirmed — I take paid commands in #payments only."
    assert h.paid() == []
    fx = h.confirm_pay("m-bank", sender=JOHN, reference="BANK-7781")
    assert _post(fx).text.startswith(
        "**PAY-1** paid by John · paid 14,250.00 USD · 3 payables · reference **BANK-7781** · "
        "booked and verified at"
    )
    assert h.paid() == ["INV-1042", "INV-1043", "INV-1044"]
    assert len(h.books.journal_by_source("PAY-1")) == 3
    assert all(r["reference"] == "BANK-7781" for r in h.books.payables(period="2026-10"))
    fx = h.confirm_pay("m-again", sender=JOHN)
    assert _post(fx).text == "**PAY-1** is already paid."
    fx = h.sweep("sweep: stage lines")
    lock_lines = [e.text for e in fx if isinstance(e, core.Post) and "ready to lock" in e.text]
    assert lock_lines == [
        "**PAY-1** paid · period 2026-10 is ready to lock. @bookkeeper propose the lock."
    ]
    assert "paid" in h.audit_events() and "refused_paid:not_in_group" in h.audit_events()
    h.assert_no_leaks()
    write_report(
        "test_12_bank_confirmation",
        "Test 12 — The bank's confirmation",
        __doc__ or "",
        h,
        "PASS: the run was sent, not paid; every wrong confirmation was refused and named; "
        "John's confirmation booked the payments under the bank's reference once.",
        [
            "an approved run is sent to the bank with its file; nothing is booked yet",
            "paid is refused before the send, from an agent, outside the groups, from the wrong room",
            "the confirmation books the run under the reference and the lock follows",
            "a second confirmation is refused",
        ],
    )
