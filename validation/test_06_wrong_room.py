"""Test 6 — Wrong room (design v5 §6, A5).

The budget is approved in #planning, its register entries in the plan room. An
`approve ENTRIES-1` typed in #planning is refused with the room; an `approve BUDGET-1` typed in
the plan room the same; a budget proposed in the plan room is not even recorded.
"""

from __future__ import annotations

from sindarin.executor import core
from validation.harness import ISAAC, LEONARDO, PERIOD_ROOM, PLANNING, Harness, write_report


def _post(fx):
    return [e for e in fx if isinstance(e, core.Post)][0]


def test_wrong_room(harness: Harness):
    h = harness
    h.run_goals_gate()
    fx = h.propose_budget(room=PERIOD_ROOM, mid="m-budget-wrong")
    assert _post(fx).text == "Proposal not recorded — I take budget proposals in #planning only."
    h.propose_budget()
    fx = h.approve_budget("m-wrong-1", sender=ISAAC, room=PERIOD_ROOM, thread=None)
    assert _post(fx).text == "**BUDGET-1** not applied — I take budget commands in #planning only."
    h.approve_budget("m-ok", sender=ISAAC)
    h.sweep()
    h.propose_entries()
    fx = h.approve_entries("m-wrong-2", sender=LEONARDO, room=PLANNING, thread=None)
    assert _post(fx).text == (
        "**ENTRIES-1** not applied — I take entries commands in #plan-2026-11 only."
    )
    assert h.books.period_status("2026-11") == "open"
    h.approve_entries("m-ok-2")
    assert h.entries().state.value == "planned" and h.books.period_status("2026-11") == "planned"
    h.assert_no_leaks()
    write_report(
        "test_06_wrong_room",
        "Test 6 — Wrong room",
        __doc__ or "",
        h,
        "PASS: commands and proposals in the wrong room were refused with the right room named; "
        "the entries landed only from the plan room.",
        [
            "a proposal in the wrong room is not recorded",
            "an approve in the wrong room is refused and names the room",
            "the period is planned only from its own room",
        ],
    )
