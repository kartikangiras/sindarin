"""Core pipeline coverage beyond the seven adversarial tests (design v5 §5, §6): P1–P5
rejections, re-proposals, deny and changes, commands, the order of the gates, the stage lines,
retries, crash recovery, the board."""

from __future__ import annotations

import json

import pytest
from validation.harness import (
    ASK,
    BOOKKEEPER_AGENT,
    FINANCE,
    ISAAC,
    JOHN,
    LEDGER_EXEC,
    LEONARDO,
    PERIOD,
    PERIOD_ROOM,
    PLANNER_AGENT,
    PLANNING,
    SWITCH_EXEC,
    VINCENT,
    Harness,
    budget_proposal,
    goals_proposal,
    proposal_text,
)

from sindarin.adapters.base import VerifyResult
from sindarin.executor import core
from sindarin.ledger import LedgerError
from sindarin.names import SWITCH_EXECUTOR
from sindarin.schema import State, is_status_fence


def _posts(fx, *, fences: bool = False):
    return [e for e in fx if isinstance(e, core.Post) and (fences or not is_status_fence(e.text))]


def _text(fx) -> str:
    return _posts(fx)[0].text


# -- P pipeline ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,fragment",
    [
        ("@sindarin-switch proposal\n```sindarin-proposal\nnot json\n```", "not JSON"),
        ("@sindarin-switch proposal\n```sindarin-proposal\n{} unterminated", "found 0"),
        ("x\n```sindarin-proposal\n{}\n```\n```sindarin-proposal\n{}\n```", "found 2"),
        (
            "@sindarin-switch proposal\n```sindarin-proposal\n"
            '{"schema":"sindarin/proposal/v3","kind":"goals"}\n```',
            "platform",
        ),
    ],
)
def test_p1_malformed(harness: Harness, raw, fragment):
    fx = harness.say(FINANCE, PLANNER_AGENT, raw, mid="m-bad")
    assert _text(fx).startswith("Proposal not recorded — malformed:") and fragment in _text(fx)
    assert harness.switch.store.all() == []


def test_p1_extra_key_and_wrong_executor(harness: Harness):
    data = json.loads(goals_proposal(harness.books).canonical())
    data["extra"] = 1
    fence = "@sindarin-switch proposal\n```sindarin-proposal\n" + json.dumps(data) + "\n```"
    fx = harness.say(FINANCE, PLANNER_AGENT, fence, mid="m-x")
    assert _text(fx).startswith("Proposal not recorded — malformed:")
    fx = harness.say(
        FINANCE, PLANNER_AGENT, proposal_text(budget_proposal(harness.books)), mid="m-y"
    )
    assert _text(fx) == "Proposal not recorded — budget proposals go to @sindarin-ledger, not me."


def test_p2_wrong_proposer_and_room(harness: Harness):
    text = proposal_text(goals_proposal(harness.books))
    fx = harness.say(FINANCE, BOOKKEEPER_AGENT, text, mid="m-a")
    assert _text(fx) == "Proposal not recorded — only planner may propose under rule goals."
    fx = harness.say(PLANNING, PLANNER_AGENT, text, mid="m-b", to=(SWITCH_EXECUTOR,))
    assert _text(fx) == "Proposal not recorded — I take goals proposals in #acme-finance only."


def test_p4_facts_from_the_ledger(harness: Harness):
    h = harness
    h.run_goals_gate()
    p = budget_proposal(h.books)
    h.books.add_sale("S-2026-1055", "2026-10-30", "Contoso Ltd", 398000)
    fx = h.say(PLANNING, PLANNER_AGENT, proposal_text(p), mid="m-hint")
    assert "the ledger changed since this was prepared" in _text(fx) and h.budget() is None
    fx = h.propose_budget(mid="m-fresh")
    assert _text(fx).startswith("**BUDGET-1** pending · 4 lines · 16,000.00 USD under the cap")


def test_p5_reproposal_keeps_id_and_duplicates_are_refused(harness: Harness):
    h = harness
    h.propose_goals()
    fx = h.propose_goals(mid="m-goals-dup")
    assert "GOALS-1 is already pending with this snapshot" in _text(fx)
    fx = h.propose_goals(mid="m-goals-other", ask="m-ask-2")
    assert "**GOALS-1** is already pending for 2026-11" in _text(fx)
    fx = h.propose_goals(mid="m-goals-rev", summary="Revised: a lower cap.")
    assert _text(fx).startswith("**GOALS-1** pending (revised, approvals cleared)")
    assert h.goals().seq == 2 and h.switch.store.peek_next_id(h.goals().kind) == "GOALS-2"


def test_unaddressed_own_and_idless_messages_only_recorded(harness: Harness):
    fx = harness.say(
        PLANNING, ISAAC, "@sindarin-ledger approve BUDGET-1", mid="m-u", addressed=False
    )
    assert _posts(fx) == [] and harness.ledger.store.get_message("m-u") is not None
    fx = harness.say(
        PLANNING, LEDGER_EXEC, "**BUDGET-1** pending", mid="m-own", name="sindarin-ledger"
    )
    assert _posts(fx) == []


# -- commands -------------------------------------------------------------------------------------


def test_help_whoami_bare_mention_and_silence(harness: Harness):
    fx = harness.command("c1", "help", sender=VINCENT, room=PLANNING)
    assert _text(fx).startswith("Commands: `approve <id>`") and "`brief`" in _text(fx)
    fx = harness.command("c2", "whoami", sender=LEONARDO, room=PLANNING)
    assert _text(fx) == f"Leonardo: participant id `{LEONARDO}` · groups: controllers"
    fx = harness.command("c3", "whoami", sender=PLANNER_AGENT, room=PLANNING)
    assert _text(fx) == f"planner: participant id `{PLANNER_AGENT}` · agent role planner"
    fx = harness.say(PLANNING, JOHN, "@sindarin-ledger", mid="c4")
    assert _text(fx).startswith("Commands:")
    fx = harness.say(PLANNING, JOHN, "@sindarin-ledger please approve BUDGET-1 now", mid="c5")
    assert _posts(fx) == [] and harness.audit_events()[-1] == "ignored"
    fx = harness.command("c6", "approve BUDGET-1.", sender=JOHN, room=PLANNING)
    assert "**BUDGET-1** is unknown here" in _text(fx)  # the period is tolerated, the id checked


def test_status_board_pointer_and_board(harness: Harness):
    h = harness
    h.run_goals_gate()
    assert _text(h.command("s1", "status", sender=JOHN, room=PLANNING)) == (
        "No open artifacts in this room."
    )
    assert "unknown here" in _text(h.command("s2", "status GOALS-1", sender=JOHN, room=PLANNING))
    fx = h.command("s3", "status GOALS-1", sender=JOHN, room=FINANCE, executor=SWITCH_EXECUTOR)
    assert _text(fx).startswith("**GOALS-1** · open · Plan November 2026")
    assert _text(h.command("s4", "board", sender=JOHN, room=PLANNING)) == (
        "The board is kept by sindarin-switch in #acme-finance."
    )
    h.propose_budget()
    fx = h.command("s5", "board", sender=JOHN, room=FINANCE, executor=SWITCH_EXECUTOR)
    text = _text(fx)
    assert text.startswith("**Board**") and "GOALS-1 · open · John" in text
    assert "waiting on planner and bookkeeper" in text
    assert "BUDGET-1 · #planning · board: Isaac" in text
    fx = h.command("s6", "status", sender=JOHN, room=PLANNING)
    assert _text(fx).startswith("**BUDGET-1** · pending · 4 lines · 16,000.00 USD under the cap")


def test_deny_and_changes(harness: Harness):
    h = harness
    h.propose_goals()
    fx = h.command(
        "d1",
        "changes GOALS-1 lower the cap",
        sender=JOHN,
        room=FINANCE,
        executor=SWITCH_EXECUTOR,
        thread=ASK,
    )
    assert _text(fx).startswith("**GOALS-1** sent back by John: lower the cap @planner")
    assert h.goals().state is State.CHANGES_REQUESTED
    assert _text(h.approve_goals("d2")) == "**GOALS-1** is changes_requested, not pending."
    h.propose_goals(mid="m-goals-2", summary="With a lower cap.")
    assert h.goals().state is State.PENDING and h.goals().seq >= 2
    fx = h.command(
        "d3",
        "deny GOALS-1 not this month",
        sender=JOHN,
        room=FINANCE,
        executor=SWITCH_EXECUTOR,
        thread=ASK,
    )
    assert _text(fx) == "**GOALS-1** denied by John: not this month. @planner nothing happens."
    assert h.goals().state is State.DENIED and h.rooms.count("execute") == 0
    fx = h.command("d4", "deny GOALS-1", sender=VINCENT, room=FINANCE, executor=SWITCH_EXECUTOR)
    assert "Vincent is not in the cfo group" in _text(fx)


def test_peer_executor_is_refused_as_approver(harness: Harness):
    h = harness
    h.run_goals_gate()
    h.propose_budget()
    fx = h.say(
        PLANNING,
        SWITCH_EXEC,
        "@sindarin-ledger approve BUDGET-1",
        mid="peer",
        name="sindarin-switch",
        thread="m-budget-1",
    )
    assert _text(fx) == "**BUDGET-1** not applied — @sindarin-switch is an agent, not an approver."


# -- the gates in order --------------------------------------------------------------------------


def test_goals_gate_opens_the_plan_room_with_the_goals(harness: Harness):
    h = harness
    h.propose_goals()
    plan = h.goals().facts()
    assert plan["humans"] == [JOHN, LEONARDO]  # the cfo, then the controllers the rule invites
    assert plan["agents"] == [PLANNER_AGENT, BOOKKEEPER_AGENT, LEDGER_EXEC, SWITCH_EXEC]
    fx = h.approve_goals()
    texts = [p.text for p in _posts(fx)]
    assert texts[0].startswith(
        "**GOALS-1** approved by John · room **#plan-2026-11** opened · verified at **"
    )
    assert texts[1].startswith(
        "**GOALS-1** plan room **#plan-2026-11** is open · people: John, Leonardo · "
        "agents: planner, bookkeeper, sindarin-ledger, sindarin-switch"
    )
    assert "The budget is approved in #planning by cfo (the board above the limit)" in texts[1]
    brief = next(p for p in _posts(fx) if p.room_id == PERIOD_ROOM)
    assert brief.text.startswith(
        "@planner @bookkeeper **GOALS-1** instructions · approved by John · period 2026-11."
    )
    assert "## Goals for 2026-11" in brief.text and "Cost cap 16,000.00 USD" in brief.text
    assert h.rooms.rooms["plan-2026-11"]["humans"] == [JOHN, LEONARDO]
    assert h.goals().result()["room_id"] == PERIOD_ROOM
    fx = h.say(
        PERIOD_ROOM, BOOKKEEPER_AGENT, "@sindarin-switch brief", mid="b1", to=(SWITCH_EXECUTOR,)
    )
    assert (
        _text(fx).startswith("@bookkeeper **GOALS-1** instructions")
        and _posts(fx)[0].thread_id is None
    )


def test_order_of_the_gates(harness: Harness):
    h = harness
    assert "GOALS-1 are not approved goals yet" in _text(h.propose_entries(mid="m-early"))
    h.run_goals_gate()
    fx = h.propose_entries(mid="m-entries-before-budget")
    assert "no budget is recorded for GOALS-1 yet" in _text(fx)
    h.propose_budget()
    fx = h.propose_budget(mid="m-budget-again", thread="other")
    assert "**BUDGET-1** is already pending for GOALS-1" in _text(fx)
    h.approve_budget()
    h.sweep()
    assert "BUDGET-9 is not the recorded budget of GOALS-1" in _text(
        h.propose_entries(budget="BUDGET-9")
    )
    h.propose_entries(mid="m-entries-ok")
    assert h.entries().state is State.PENDING


def test_stage_lines_hand_the_work_on_once(harness: Harness):
    h = harness
    h.propose_goals()
    h.approve_goals()
    fx = h.sweep("first sweep")
    lines = [p for p in _posts(fx) if p.room_id == PLANNING]
    assert len(lines) == 1 and lines[0].text == (
        "**GOALS-1** approved · revenue target 95,000.00 USD · cost cap 16,000.00 USD for "
        "2026-11. @planner propose the budget."
    )
    assert [p for p in _posts(h.sweep("second sweep")) if p.room_id == PLANNING] == []
    h.propose_budget()
    h.approve_budget()
    fx = h.sweep("after budget")
    ready = [p for p in _posts(fx) if p.room_id == PERIOD_ROOM]
    assert ready and ready[0].text == (
        "**BUDGET-1** recorded · 4 lines, 16,000.00 USD under the cap · period 2026-11 is "
        "ready for its entries. @bookkeeper propose the budget register entries."
    )
    assert h.books.targets(PERIOD)["cost_cap_cents"] == 1600000  # the goals landed in the ledger
    h.propose_entries()
    h.approve_entries()
    fx = h.sweep("after entries")
    done = [p for p in _posts(fx) if p.room_id == FINANCE]
    assert done and done[0].text == (
        "**GOALS-1** done · ENTRIES-1 applied · period 2026-11 is planned. "
        "@analyst tell the business."
    )
    assert done[0].thread_id == ASK and h.goals().state is State.DONE
    assert h.books.period_status(PERIOD) == "planned" and len(h.planned()) == 4
    # the period is planned: a second set of goals for it, in a new thread, is refused
    fx = h.propose_goals(mid="m-goals-again", ask="m-ask-9")
    assert "**GOALS-1** is already done for 2026-11" in _text(fx)


# -- failures, retries, recovery -------------------------------------------------------------------


def test_record_failure_retries_once_then_fails(harness: Harness, monkeypatch):
    h = harness
    h.run_goals_gate()
    h.propose_budget()

    def boom(*args, **kwargs):
        raise LedgerError("books locked")

    monkeypatch.setattr(h.books, "record_budget", boom)
    fx = h.approve_budget()
    assert any(
        "record budget failed: books locked. Retrying once in 60 min" in p.text for p in _posts(fx)
    )
    assert h.budget().state is State.RECORD_FAILED
    h.clock.advance(hours=1, minutes=1)
    fx = h.sweep()
    assert h.budget().state is State.FAILED and any("failed again" in p.text for p in _posts(fx))
    assert h.recorded() == []


def test_room_open_failure_retries_hourly_for_ever(harness: Harness):
    h = harness
    h.propose_goals()
    h.rooms.fail_execute_times = 3
    h.approve_goals()
    assert h.goals().state is State.OPEN_FAILED
    for _ in range(3):
        h.clock.advance(hours=1, minutes=1)
        h.sweep()
    assert h.goals().state is State.OPEN and h.goals().attempts == 4


def test_read_back_failure_is_not_success(harness: Harness, monkeypatch):
    h = harness
    h.run_goals_gate()
    h.propose_budget()
    adapter = h.ledger.adapters["ledger"]
    monkeypatch.setattr(type(adapter), "verify", lambda self, a: VerifyResult(present=False))
    fx = h.approve_budget()
    assert h.budget().state is State.RECORD_FAILED
    assert any("not visible on read-back" in p.text for p in _posts(fx))


def test_crash_recovery(harness: Harness):
    h = harness
    h.run_goals_gate()
    h.propose_budget()
    # the executor died right after writing `approved`: the sweep resumes, verifies, then acts
    h.ledger.store.transition("BUDGET-1", State.APPROVED, approver_id=ISAAC, approver_name="Isaac")
    fx = h.sweep("recovery")
    assert h.budget().state is State.RECORDED and len(h.recorded()) == 4
    assert any("approved by Isaac · recorded 16,000.00 USD" in p.text for p in _posts(fx))
    events = h.audit_events()
    assert "recovery_resume" in events and "recovery_execute" in events


def test_sale_command_is_a_persons_event_on_the_books(harness: Harness):
    h = harness
    fx = h.command("sale-1", "sale S-2026-1055 3980.00 Contoso Ltd", sender=JOHN, room=PLANNING)
    assert _text(fx).startswith("**S-2026-1055** Contoso Ltd 3,980.00 USD landed in 2026-10")
    assert h.books.sales("2026-10")[-1]["invoice"] == "S-2026-1055"
    fx = h.command(
        "sale-2", "sale S-2026-1056 10.00 Contoso Ltd", sender=PLANNER_AGENT, room=PLANNING
    )
    assert (
        _text(fx)
        == "**S-2026-1056** not booked — @planner is an agent; a sale lands from a person."
    )
    fx = h.command(
        "sale-3", "sale S-2026-1057 10.00 X", sender=JOHN, room=FINANCE, executor=SWITCH_EXECUTOR
    )
    assert _text(fx).startswith("The books are kept by sindarin-ledger")
    fx = h.command("sale-4", "sale S-2026-1058 0 X", sender=JOHN, room=PLANNING)
    assert _text(fx) == "**S-2026-1058** not booked — the amount must be positive."


def test_finance_room_has_no_ledger_executor(harness: Harness):
    """Only sindarin-switch sits in #acme-finance: a ledger command there reaches nobody who
    acts on it (Switch delivers to members only); the switch executor just records the message."""
    fx = harness.say(FINANCE, JOHN, "@sindarin-ledger approve BUDGET-1", mid="nf", addressed=False)
    assert _posts(fx) == [] and harness.ledger.store.get_message("nf") is None
