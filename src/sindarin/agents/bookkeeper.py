"""`bookkeeper` (design v5 §4): proposes the budget register entries once a period's budget is
recorded; proposes the month's close plan when the business asks, and the lock once the
payment run is paid. Every figure is computed from the ledger; the agent cannot post, pay,
lock or approve."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sindarin.agents.base import Outgoing, Tool, Turn, human_turns, strict_tool, tool_history
from sindarin.agents.finance import (
    _between,
    approver_mention,
    month_label,
    period_from,
    period_from_any,
    report_lines,
    report_period,
    standard_entries,
)
from sindarin.agents.roles import (
    BRIEF_RE,
    BUDGET_ID_RE,
    CLOSE_ID_RE,
    PLAN_ID_RE,
    RoleContext,
    split_fence_message,
)
from sindarin.ledger import Ledger
from sindarin.names import LEDGER_EXECUTOR, SWITCH_EXECUTOR
from sindarin.propose import build_close, build_entries, build_lock, render
from sindarin.schema import Kind, money
from sindarin.switch.events import MessageEvent

NAME = "bookkeeper"

PROPOSE_ENTRIES = strict_tool(
    "propose_entries",
    "Validate the budget register entries of a period whose budget is recorded: the recorded "
    "lines as the ledger holds them, posted as the period's planned amounts. Returns the block "
    "to post, or a one-line reason it is not allowed.",
    {
        "period": {"type": "string", "description": "YYYY-MM"},
        "goals": {"type": "string", "description": "GOALS-n, from the room's brief"},
        "budget": {"type": "string", "description": "BUDGET-n, from the stage line"},
        "memo": {"type": "string", "description": "one line for the books"},
        "justification": {"type": "string"},
    },
    ["period", "goals", "budget", "memo", "justification"],
)
PROPOSE_CLOSE = strict_tool(
    "propose_close",
    "Validate the month's close plan and return the exact block to post: the adjusting entries "
    "(computed from the ledger), the open payables to pay with their total, the report lines. "
    "Or a one-line reason it is not allowed.",
    {
        "period": {"type": "string", "description": "YYYY-MM, the month to close"},
        "title": {"type": "string", "description": "e.g. Close October 2026"},
        "summary": {"type": "string", "description": "one sentence on the month"},
        "ask": {"type": "string", "description": "the thread root id from the header"},
        "justification": {"type": "string", "description": "one line: why this plan"},
    },
    ["period", "title", "summary", "ask", "justification"],
)
PROPOSE_LOCK = strict_tool(
    "propose_lock",
    "Validate the lock of a period after its payment run was paid: the adjusting entries and "
    "the lock itself. Returns the block to post, or a one-line reason it is not allowed.",
    {
        "period": {"type": "string", "description": "YYYY-MM"},
        "plan": {"type": "string", "description": "CLOSE-n, from the room's brief"},
        "pay_run": {"type": "string", "description": "PAY-n that was paid, or empty"},
        "memo": {"type": "string", "description": "one line for the journal"},
        "justification": {"type": "string"},
    },
    ["period", "plan", "pay_run", "memo", "justification"],
)


def tools(ctx: RoleContext) -> list[Tool]:
    def propose_close(args: dict[str, Any]) -> tuple[str, bool]:
        if ctx.ledger is None:
            return "no ledger configured", False
        period = report_period(period_from(str(args.get("period", "")), ctx.ledger), ctx.ledger)
        if not period:
            return "which period? none in the ledger", False
        try:
            s = ctx.ledger.summary(period)
            rule = next(iter(ctx.policy().rules_for(Kind.CLOSE)), None)
            currency = rule.currency if rule else "USD"
            open_rows = [p for p in s["payables"] if p["status"] == "open"]
            p = build_close(
                ctx.policy(),
                period=period,
                title=str(args.get("title") or f"Close {month_label(period)}"),
                summary=str(args.get("summary", "")),
                report=report_lines(s, currency),
                entries=standard_entries(ctx.ledger, period),
                payables=[row["invoice"] for row in open_rows],
                total_payables_cents=sum(int(row["amount_cents"]) for row in open_rows),
                currency=currency,
                ask=str(args.get("ask", "")),
                justification=str(args.get("justification", "")),
            )
        except ValueError as exc:
            return f"Not allowed: {exc}", False
        return render(p), True

    def propose_lock(args: dict[str, Any]) -> tuple[str, bool]:
        if ctx.ledger is None:
            return "no ledger configured", False
        period = period_from(str(args.get("period", "")), ctx.ledger)
        if not period:
            return "which period? none in the ledger", False
        try:
            digest = Ledger.balance_digest(ctx.ledger.trial_balance(period))
            p = build_lock(
                ctx.policy(),
                period=period,
                plan=str(args.get("plan", "")),
                pay_run=str(args.get("pay_run", "")),
                entries=standard_entries(ctx.ledger, period),
                memo=str(args.get("memo") or f"Close {month_label(period)}"),
                digest=digest,
                justification=str(args.get("justification", "")),
            )
        except ValueError as exc:
            return f"Not allowed: {exc}", False
        return render(p), True

    def propose_entries(args: dict[str, Any]) -> tuple[str, bool]:
        if ctx.ledger is None:
            return "no ledger configured", False
        period = period_from(str(args.get("period", "")), ctx.ledger)
        if not period:
            return "which period? none in the ledger", False
        rows = ctx.ledger.budget(period)
        if not rows:
            return f"no budget recorded for {period} yet", False
        try:
            p = build_entries(
                ctx.policy(),
                period=period,
                goals=str(args.get("goals", "")),
                budget=str(args.get("budget") or rows[0]["source"]),
                lines=[
                    {
                        "department": r["department"],
                        "account": r["account"],
                        "amount_cents": int(r["amount_cents"]),
                    }
                    for r in rows
                ],
                memo=str(args.get("memo") or f"Budget {month_label(period)}"),
                digest=Ledger.budget_digest(rows),
                justification=str(args.get("justification", "")),
            )
        except ValueError as exc:
            return f"Not allowed: {exc}", False
        return render(p), True

    return [
        Tool(PROPOSE_ENTRIES, propose_entries),
        Tool(PROPOSE_CLOSE, propose_close),
        Tool(PROPOSE_LOCK, propose_lock),
    ]


def context_text(ctx: RoleContext, room_id: str = "") -> str:
    periods = ", ".join(ctx.ledger.periods()) if ctx.ledger is not None else "none"
    brief = ctx.brief(room_id)
    policy = ctx.policy_path.read_text(encoding="utf-8").strip()
    return (
        f"# The ledger\nRead-only. Periods it holds: {periods}. Lines and totals come from the "
        "tool; you never type a figure.\n\n"
        f"# The brief (room instructions)\n{brief or '(no plan or period room brief yet)'}\n\n"
        f"# policy.yaml\n```yaml\n{policy}\n```"
    )


def trigger(ev: MessageEvent, ctx: RoleContext, thread: list[Turn]) -> bool:
    if ev.sender == ctx.self_id:
        return False
    body = ev.body or ""
    if ev.sender in ctx.executor_ids():
        if BRIEF_RE.search(body):
            return False  # the brief is context, not a call to act
        # a stage line in a plan or period room ("@bookkeeper propose ..."), or a `changes`
        # sent back naming me, in #acme-finance or the room
        return f"@{NAME}" in body and (
            ctx.is_period_room(ev.room_id) or ctx.room_key(ev.room_id) == "finance"
        )
    if ctx.room_key(ev.room_id) == "finance":
        return ev.addressed and ctx.is_human(ev.sender)  # the close ask names me
    return False


def header(ev: MessageEvent, ctx: RoleContext, thread: list[Turn]) -> str:
    root = thread[0] if thread else None
    asker = root.sender if root else ev.sender
    body = ev.body or ""
    text = " ".join(t.body for t in thread) or body
    brief = ctx.brief(ev.room_id)
    period = period_from_any([body, root.body if root else "", brief, text], ctx.ledger)
    key = ctx.room_key(ev.room_id) or "period"
    goals = next(iter(PLAN_ID_RE.findall(brief + " " + text)), "")
    plan = next(iter(CLOSE_ID_RE.findall(brief + " " + text)), "")
    budget = next(iter(BUDGET_ID_RE.findall(text)), "")
    pay = _between(body, "**PAY-", "**")
    pay_run = f"PAY-{pay}" if pay else ""
    root_id = ev.thread_id or ev.message_id
    return (
        f"[Room {ctx.room_name(ev.room_id)} ({key}) · thread root {root_id} · asked by "
        f"{ctx.name_of(asker)}, mention as {ctx.mention(asker)} · period {period or '?'} · "
        f"goals {goals or '-'} · budget {budget or '-'} · plan {plan or '-'} · "
        f"pay run {pay_run or '-'}]"
    )


def route(text: str, ev: MessageEvent, ctx: RoleContext, thread: list[Turn]) -> list[Outgoing]:
    before, fence, after = split_fence_message(text)
    root = ev.thread_id or ev.message_id
    if not fence:
        return [Outgoing(ev.room_id, text, root)]
    return [Outgoing(ev.room_id, "\n".join(x for x in (before, fence, after) if x), root)]


# -- the stand-in (SINDARIN_MODEL=fake) ----------------------------------------------------------


def fake_step(ctx: RoleContext) -> Callable[[list[dict[str, Any]], str], tuple[Any, ...]]:
    def step(messages: list[dict[str, Any]], system: str) -> tuple[Any, ...]:
        header_line = messages[0]["content"].splitlines()[0] if messages else ""
        period = _between(header_line, "period ", " ·")
        goals = _between(header_line, "goals ", " ·")
        budget = _between(header_line, "budget ", " ·")
        plan = _between(header_line, "plan ", " ·")
        pay_run = _between(header_line, "pay run ", "]").strip()
        ask = _between(header_line, "thread root ", " ·")
        in_finance = "(finance)" in header_line
        in_room = "(period)" in header_line
        history = tool_history(messages)
        turns = human_turns(messages)
        if history:
            name, args, out, ok = history[-1]
            if not ok:
                return ("text", f"I can't propose that: {out}")
            if name == "propose_close":
                who = approver_mention(ctx, Kind.CLOSE)
                return (
                    "text",
                    f"Plan for {month_label(period)}, from the ledger.\n{out}\n"
                    f"Your call, {who}: `@{SWITCH_EXECUTOR} approve CLOSE-n` with the id above.",
                )
            if name == "propose_lock":
                who = approver_mention(ctx, Kind.LOCK)
                return (
                    "text",
                    f"Lock of {month_label(period)}: the adjusting entries and the period lock.\n"
                    f"{out}\nYour call, {who}: `@{LEDGER_EXECUTOR} approve LOCK-n` with the id "
                    "above.",
                )
            who = approver_mention(ctx, Kind.ENTRIES)
            return (
                "text",
                f"Budget register entries for {month_label(period)}: the recorded lines into "
                f"the books.\n{out}\nYour call, {who}: `@{LEDGER_EXECUTOR} approve ENTRIES-n` "
                "with the id above.",
            )
        if in_finance:
            sent_back = next((t for t in turns if "sent back by" in t), "")
            summary = f"{month_label(period)} closes with the standard accruals" if period else ""
            if sent_back:
                summary += f"; revised: {sent_back.split('sent back by', 1)[1].strip()[:80]}"
            return (
                "tool",
                "propose_close",
                {
                    "period": period,
                    "title": f"Close {month_label(period)}" if period else "Close",
                    "summary": summary or "Month-end close",
                    "ask": ask,
                    "justification": "the business asked for the close; entries from the ledger",
                },
            )
        if in_room and plan not in ("", "-") and "ready to lock" in " ".join(turns):
            return (
                "tool",
                "propose_lock",
                {
                    "period": period,
                    "plan": plan,
                    "pay_run": "" if pay_run == "-" else pay_run,
                    "memo": f"Close {month_label(period)}" if period else "Close",
                    "justification": "payment run paid; entries computed from the ledger",
                },
            )
        return (
            "tool",
            "propose_entries",
            {
                "period": period,
                "goals": goals if goals != "-" else "",
                "budget": budget if budget != "-" else "",
                "memo": f"Budget {month_label(period)}" if period else "Budget",
                "justification": "the budget is recorded; lines from the ledger",
            },
        )

    return step


__all__ = ["NAME", "fake_step", "header", "money", "route", "tools", "trigger"]
