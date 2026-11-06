"""Test 10 — The order of the month-end chain and its rooms (design v5 §5, §6).

A lock before the plan is refused; a lock before the run is paid is refused; a run proposed in
the period room is not recorded; `approve LOCK-1` typed in #payments is refused with the room.
Then the run is paid and the lock lands from the period room only: entries posted, period
locked, the plan done, the analyst told.
"""

from __future__ import annotations

from sindarin.executor import core
from validation.harness import ASK, CLOSE_ROOM, FINANCE, LEONARDO, PAYMENTS, Harness, write_report


def _post(fx):
    return [e for e in fx if isinstance(e, core.Post)][0]


def _posts(fx):
    return [e for e in fx if isinstance(e, core.Post)]


def test_lock_follows_the_paid_run_in_its_room(harness: Harness):
    h = harness
    assert "CLOSE-1 is not an approved plan yet" in _post(h.propose_lock(mid="m-early")).text
    h.run_plan_gate()
    assert (
        "the lock follows their payment run being paid"
        in _post(h.propose_lock(mid="m-lock-before-pay")).text
    )
    fx = h.propose_pay(room=CLOSE_ROOM, mid="m-pay-wrong")
    assert _post(fx).text == "Proposal not recorded — I take pay proposals in #payments only."
    h.run_pay_gate()
    h.propose_lock()
    fx = h.approve_lock("m-wrong", sender=LEONARDO, room=PAYMENTS, thread=None)
    assert _post(fx).text == "**LOCK-1** not applied — I take lock commands in #close-2026-10 only."
    assert h.books.period_status("2026-10") == "open"
    h.approve_lock("m-ok")
    assert h.lock().state.value == "locked" and h.books.period_status("2026-10") == "locked"
    assert len(h.books.journal_by_source("LOCK-1")) == 3
    fx = h.sweep("close done")
    done = [p for p in _posts(fx) if p.room_id == FINANCE]
    assert done and done[0].text == (
        "**CLOSE-1** done · LOCK-1 applied · period 2026-10 is locked. @analyst tell the business."
    )
    assert done[0].thread_id == ASK and h.plan().state.value == "done"
    h.assert_no_leaks()
    write_report(
        "test_10_lock_order",
        "Test 10 — The order of the month-end chain",
        __doc__ or "",
        h,
        "PASS: every out-of-order or wrong-room step was refused; the period locked only after "
        "its run was paid and only from its own room.",
        [
            "a lock before the plan, or before the run is paid, is refused",
            "a proposal or an approve in the wrong room is refused and names the room",
            "the lock posts the entries, locks the period and finishes the plan",
        ],
    )
