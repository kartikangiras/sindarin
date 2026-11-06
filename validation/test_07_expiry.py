"""Test 7 — Goals pending past their TTL (design v5 §6, A4).

GOALS-1 is pending with a 24h TTL. The clock moves 25 hours; the sweep expires it and tells the
planner; a late approval is refused as expired; no plan room is opened.
"""

from __future__ import annotations

from sindarin.executor import core
from validation.harness import Harness, write_report


def _posts(fx):
    return [e for e in fx if isinstance(e, core.Post)]


def test_expiry(harness: Harness):
    h = harness
    h.propose_goals()
    assert h.goals().state.value == "pending"
    h.clock.advance(hours=25)
    fx = h.sweep("sweep after 25h")
    assert any(t.text.startswith("**GOALS-1** expired after 24h") for t in _posts(fx))
    assert h.goals().state.value == "expired"
    fx = h.approve_goals("m-late")
    assert _posts(fx)[0].text == "**GOALS-1** is expired, not pending."
    assert h.rooms.count("execute") == 0
    h.assert_no_leaks()
    write_report(
        "test_07_expiry",
        "Test 7 — Goals pending past their TTL",
        __doc__ or "",
        h,
        "PASS: expired by the sweep, the late yes refused, no room opened.",
        ["the sweep expires the goals and says so", "a late approve is refused", "no Gateway call"],
    )
