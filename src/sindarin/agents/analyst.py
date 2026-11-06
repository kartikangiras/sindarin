"""`analyst` (design v5 §4): reports the month from the ledger when the business asks, and tells
the business when the period is planned. Proposes nothing, approves nothing."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sindarin.agents.base import Outgoing, Tool, Turn, human_turns, strict_tool, tool_history
from sindarin.agents.finance import (
    _between,
    month_label,
    period_from,
    period_from_any,
    render_report,
    report_period,
)
from sindarin.agents.roles import BRIEF_RE, RoleContext
from sindarin.schema import utcnow
from sindarin.switch.events import MessageEvent

NAME = "analyst"

MONTH_REPORT = strict_tool(
    "month_report",
    "The month's report from the ledger: revenue against its target, revenue by customer, "
    "costs against the cap, margin, anomalies such as duplicate sales rows. Returns a markdown "
    "table to post verbatim. For a month with no sales yet, reports the month before it.",
    {"period": {"type": "string", "description": "YYYY-MM"}},
    ["period"],
)


def tools(ctx: RoleContext) -> list[Tool]:
    def month_report(args: dict[str, Any]) -> tuple[str, bool]:
        if ctx.ledger is None:
            return "no ledger configured", False
        period = report_period(period_from(str(args.get("period", "")), ctx.ledger), ctx.ledger)
        if not period:
            return "which period? none in the ledger", False
        try:
            return render_report(ctx.ledger.summary(period)), True
        except Exception as exc:
            return f"could not read the ledger: {exc.__class__.__name__}", False

    return [Tool(MONTH_REPORT, month_report)]


def context_text(ctx: RoleContext, room_id: str = "") -> str:
    periods = ", ".join(ctx.ledger.periods()) if ctx.ledger is not None else "none"
    return (
        "# The ledger\n"
        f"Read-only. Periods it holds: {periods}. Every number you post comes from "
        "`month_report`; you never type a figure yourself.\n\n"
        "# Rooms\nYou answer in #acme-finance only, in the thread you were asked in."
    )


def trigger(ev: MessageEvent, ctx: RoleContext, thread: list[Turn]) -> bool:
    if ev.sender == ctx.self_id or ctx.room_key(ev.room_id) != "finance":
        return False
    body = ev.body or ""
    if ev.sender in ctx.executor_ids():
        if BRIEF_RE.search(body):
            return False  # the brief is context, not a call to act
        # the Switch executor's stage line once the period is planned: "@analyst tell the business"
        return f"@{NAME}" in body
    # a person who names me; approvals and chatter in the thread are not for me (one message
    # per trigger, design v5 §4)
    return ev.addressed and ctx.is_human(ev.sender)


def header(ev: MessageEvent, ctx: RoleContext, thread: list[Turn]) -> str:
    root = thread[0] if thread else None
    asker = root.sender if root else ev.sender
    texts = [ev.body or "", root.body if root else "", " ".join(t.body for t in thread)]
    period = period_from_any(texts, ctx.ledger)
    basis = report_period(period, ctx.ledger)
    planned = "planned" if "is planned" in (ev.body or "") else "open"
    root_id = ev.thread_id or ev.message_id
    return (
        f"[Room {ctx.room_name(ev.room_id)} (finance) · thread root {root_id} · asked by "
        f"{ctx.name_of(asker)}, mention as {ctx.mention(asker)} · period {period or '?'} "
        f"({planned}) · basis {basis or '?'} · today {utcnow():%Y-%m-%d}]"
    )


def route(text: str, ev: MessageEvent, ctx: RoleContext, thread: list[Turn]) -> list[Outgoing]:
    return [Outgoing(ev.room_id, text, ev.thread_id or ev.message_id)]


# -- the stand-in (SINDARIN_MODEL=fake) ----------------------------------------------------------


def fake_step(ctx: RoleContext) -> Callable[[list[dict[str, Any]], str], tuple[Any, ...]]:
    def step(messages: list[dict[str, Any]], system: str) -> tuple[Any, ...]:
        header_line = messages[0]["content"].splitlines()[0] if messages else ""
        period = _between(header_line, "period ", " (")
        basis = _between(header_line, "basis ", " ·")
        who = _between(header_line, "mention as ", " ·") or "@there"
        planned = "(planned)" in header_line
        history = tool_history(messages)
        turns = human_turns(messages)
        if not history:
            return ("tool", "month_report", {"period": basis or period})
        name, args, out, ok = history[-1]
        if not ok:
            return ("text", f"{who} I could not read the ledger: {out}")
        label = month_label(period) if period else "the month"
        if planned:
            return (
                "text",
                f"{who} {label} is planned: goals approved, the budget recorded and its "
                "register entries posted. The basis month is above in this thread.",
            )
        tail = "The planner's goals follow in this thread." if turns else ""
        shown = month_label(basis) if basis else label
        return ("text", f"{who} here is {shown}.\n{out}\n{tail}".rstrip())

    return step
