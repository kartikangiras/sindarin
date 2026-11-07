"""The audit trail from chat, who approves what, and the loose phrasing of the reading commands."""

from __future__ import annotations

from datetime import UTC, datetime

from validation.harness import FINANCE, JOHN, LEONARDO, PERIOD_ROOM, PLANNING, VINCENT, Harness

from sindarin.executor import commands as cmd
from sindarin.executor import core
from sindarin.ledger import Ledger
from sindarin.names import SWITCH_EXECUTOR


def _posts(fx):
    return [e.text for e in fx if isinstance(e, core.Post)]


def test_loose_phrasing_resolves_months_but_never_a_gate(monkeypatch):
    monkeypatch.setattr(cmd, "_today", lambda: datetime(2026, 11, 7, tzinfo=UTC))
    assert cmd.parse_command("@sindarin-switch chart October revenue") == cmd.Chart(
        "2026-10", "revenue"
    )
    assert cmd.parse_command("@sindarin-switch show me the november budget chart") == cmd.Chart(
        "2026-11", "budget"
    )
    assert cmd.parse_command("@sindarin-switch export the october sales") == cmd.Export(
        "2026-10", "sales"
    )
    assert cmd.parse_command("@sindarin-switch report for October 2026") == cmd.Report("2026-10")
    assert cmd.parse_command("@sindarin-switch report December") == cmd.Report("2025-12")  # ahead
    assert cmd.parse_command("@sindarin-switch audit october") == cmd.Audit("2026-10")
    assert cmd.parse_command("@sindarin-ledger audit BUDGET-1") == cmd.Audit("BUDGET-1")
    assert cmd.parse_command("@sindarin-ledger who approves the budget") == cmd.Approvers("budget")
    assert cmd.parse_command("@sindarin-switch who approves") == cmd.Approvers(None)
    assert cmd.parse_command("@sindarin-switch approve october") is None
    assert cmd.parse_command("@sindarin-switch chart october") is None  # which chart?
    assert cmd.parse_command("@sindarin-switch chart october revenue costs") is None
    assert cmd.parse_command("@sindarin-switch please approve GOALS-1") is None
    assert "`audit <id>|<period>`" in cmd.HELP_TEXT and "who approves" in cmd.HELP_TEXT


def test_who_approves_is_answered_by_the_owner_only(harness: Harness):
    h = harness
    fx = h.command("w1", "who approves budget", sender=JOHN, room=PLANNING)
    text = _posts(fx)[0]
    assert text.startswith(
        "**budget** · board; cfo alone for amounts up to 10,000.00 USD · in #planning"
    )
    assert "board: Isaac" in text and "cfo: John" in text
    fx = h.command("w2", "who approves goals", sender=JOHN, room=PLANNING)  # not the ledger's
    assert _posts(fx) == []
    fx = h.command("w3", "who approves", sender=JOHN, room=FINANCE, executor=SWITCH_EXECUTOR)
    text = _posts(fx)[0]
    assert "**goals** · cfo · in #acme-finance · cfo: John" in text
    assert "**close** · controllers · in #acme-finance · controllers: Leonardo" in text
    assert "budget" not in text
    fx = h.command("w4", "who approves deal", sender=VINCENT, room=PLANNING)
    assert (
        "**deal** · cfo; controllers alone for a discount up to 1,000.00 USD · in #sales"
        in (_posts(fx)[0])
    )


def test_audit_trail_names_who_did_what(harness: Harness):
    h = harness
    h.propose_goals()
    h.approve_goals("m-v", sender=VINCENT)  # refused
    h.run_goals_gate()
    fx = h.command("a1", "audit GOALS-1", sender=JOHN, room=FINANCE, executor=SWITCH_EXECUTOR)
    text = _posts(fx)[0]
    assert text.startswith("**audit GOALS-1** ·") and "```" in text
    assert "refused_approval:not_approver · GOALS-1 · Vincent" in text
    assert "applied · GOALS-1 · John" in text
    assert "audit_posted" in h.audit_events(SWITCH_EXECUTOR)
    # the ledger does not hold GOALS-1: in its own room it points to the keeper, in a room the
    # switch opened it is silent, and an id nobody holds is unknown
    fx = h.command("a2", "audit GOALS-1", sender=JOHN, room=PLANNING)
    assert _posts(fx) == ["**GOALS-1** is kept by sindarin-switch: ask in #acme-finance."]
    fx = h.command("a2b", "audit LOCK-9", sender=JOHN, room=PLANNING)
    assert _posts(fx) == ["**LOCK-9** is unknown here."]
    fx = h.command("a3", "audit GOALS-1", sender=JOHN, room=PERIOD_ROOM)
    assert _posts(fx) == []
    # a month, asked in the finance room, comes from both executors' stores
    h.run_budget_gate()
    fx = h.command("a4", "audit 2026-11", sender=JOHN, room=FINANCE, executor=SWITCH_EXECUTOR)
    text = _posts(fx)[0]
    assert "GOALS-1 · John" in text and "BUDGET-1 · Isaac" in text, text
    # the same month asked in #planning: the ledger answers for its own events only
    fx = h.command("a5", "audit 2026-11", sender=JOHN, room=PLANNING)
    text = _posts(fx)[0]
    assert "BUDGET-1" in text and "GOALS-1" not in text
    # and in a room with both executors only the ledger speaks
    fx = h.command("a6", "audit 2026-11", sender=LEONARDO, room=PERIOD_ROOM)
    assert len(_posts(fx)) == 1
    # the CSV export of the month's trail
    h.switch.ledger = Ledger(h.books.path, readonly=True)
    fx = h.command(
        "a7", "export 2026-11 audit", sender=JOHN, room=FINANCE, executor=SWITCH_EXECUTOR
    )
    text = _posts(fx)[0]
    assert text.startswith("**audit 2026-11** ·") and "```csv" in text and "actor_id" in text
