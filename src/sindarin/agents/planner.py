"""`planner` (design v5 §4): proposes the period's goals when the business asks, from the basis
month's actuals, and the budget under the approved cap when sindarin-ledger calls for it.
Every figure is computed from the ledger; the agent cannot record, approve or spend."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from sindarin.agents.base import Outgoing, Tool, Turn, human_turns, strict_tool, tool_history
from sindarin.agents.finance import (
    _between,
    approver_mention,
    month_label,
    parse_money,
    period_from,
    period_from_any,
    plan_goals,
    report_lines,
    standard_budget_lines,
)
from sindarin.agents.roles import BRIEF_RE, PLAN_ID_RE, RoleContext, split_fence_message
from sindarin.names import LEDGER_EXECUTOR, SWITCH_EXECUTOR
from sindarin.propose import build_budget, build_goals, render
from sindarin.schema import Kind, money, previous_period
from sindarin.switch.events import MessageEvent

NAME = "planner"
CAP_RE = re.compile(r"cost cap ([\d,]+\.\d{2} [A-Z]{3})")

PROPOSE_GOALS = strict_tool(
    "propose_goals",
    "Validate the period's goals and return the exact block to post: the revenue target, the "
    "margin target and the cost cap, computed from the basis month's actuals in the ledger, "
    "with the basis report lines. Or a one-line reason it is not allowed.",
    {
        "period": {"type": "string", "description": "the month to plan, YYYY-MM"},
        "title": {"type": "string", "description": "e.g. Plan November 2026"},
        "summary": {"type": "string", "description": "one sentence on the month ahead"},
        "ask": {"type": "string", "description": "the thread root id from the header"},
        "justification": {"type": "string", "description": "one line: why these goals"},
    },
    ["period", "title", "summary", "ask", "justification"],
)
PROPOSE_BUDGET = strict_tool(
    "propose_budget",
    "Validate the period's budget under the approved cost cap and return the block to post: "
    "one line per department, computed from the cap, their total under it. Or a one-line "
    "reason it is not allowed.",
    {
        "period": {"type": "string", "description": "YYYY-MM"},
        "goals": {"type": "string", "description": "GOALS-n, from the stage line"},
        "cost_cap": {
            "type": "string",
            "description": "the cap as the stage line shows it, e.g. 16,000.00 USD",
        },
        "justification": {"type": "string", "description": "one line"},
    },
    ["period", "goals", "cost_cap", "justification"],
)


def tools(ctx: RoleContext) -> list[Tool]:
    def propose_goals(args: dict[str, Any]) -> tuple[str, bool]:
        if ctx.ledger is None:
            return "no ledger configured", False
        period = period_from(str(args.get("period", "")), ctx.ledger)
        if not period:
            return "which period? none in the ledger", False
        basis = previous_period(period)
        if not ctx.ledger.sales(basis):
            return f"no actuals for {basis} in the ledger to plan {period} from", False
        try:
            rule = next(iter(ctx.policy().rules_for(Kind.GOALS)), None)
            currency = rule.currency if rule else "USD"
            s = ctx.ledger.summary(basis)
            goals = plan_goals(ctx.ledger, period, basis)
            p = build_goals(
                ctx.policy(),
                period=period,
                basis=basis,
                title=str(args.get("title") or f"Plan {month_label(period)}"),
                summary=str(args.get("summary", "")),
                report=report_lines(s, currency),
                currency=currency,
                ask=str(args.get("ask", "")),
                justification=str(args.get("justification", "")),
                **goals,
            )
        except ValueError as exc:
            return f"Not allowed: {exc}", False
        return render(p), True

    def propose_budget(args: dict[str, Any]) -> tuple[str, bool]:
        if ctx.ledger is None:
            return "no ledger configured", False
        period = period_from(str(args.get("period", "")), ctx.ledger)
        if not period:
            return "which period? none in the ledger", False
        cap = parse_money(str(args.get("cost_cap", "")))
        if cap <= 0:
            return "which cap? give it as the stage line shows it, e.g. 16,000.00 USD", False
        basis = previous_period(period)
        try:
            rule = next(iter(ctx.policy().rules_for(Kind.BUDGET)), None)
            p = build_budget(
                ctx.policy(),
                period=period,
                goals=str(args.get("goals", "")),
                lines=standard_budget_lines(cap),
                currency=rule.currency if rule else "USD",
                digest=ctx.ledger.actuals_digest(basis),
                justification=str(args.get("justification", "")),
            )
        except ValueError as exc:
            return f"Not allowed: {exc}", False
        return render(p), True

    return [Tool(PROPOSE_GOALS, propose_goals), Tool(PROPOSE_BUDGET, propose_budget)]


def context_text(ctx: RoleContext, room_id: str = "") -> str:
    periods = ", ".join(ctx.ledger.periods()) if ctx.ledger is not None else "none"
    policy = ctx.policy_path.read_text(encoding="utf-8").strip()
    return (
        f"# The ledger\nRead-only. Periods it holds: {periods}. Targets, caps and lines come "
        "from the tools; you never type a figure.\n\n"
        f"# policy.yaml\n```yaml\n{policy}\n```"
    )


def trigger(ev: MessageEvent, ctx: RoleContext, thread: list[Turn]) -> bool:
    if ev.sender == ctx.self_id:
        return False
    key = ctx.room_key(ev.room_id)
    body = ev.body or ""
    if ev.sender in ctx.executor_ids():
        if BRIEF_RE.search(body):
            return False  # the brief is context, not a call to act
        # the stage line in #planning ("@planner propose the budget"), a `changes`, a changed
        # ledger; `changes GOALS-n` sent back in #acme-finance
        return f"@{NAME}" in body and key in ("finance", "planning")
    if key == "finance":
        return ev.addressed and ctx.is_human(ev.sender)  # the ask names me; chatter does not
    return False


def header(ev: MessageEvent, ctx: RoleContext, thread: list[Turn]) -> str:
    root = thread[0] if thread else None
    asker = root.sender if root else ev.sender
    body = ev.body or ""
    text = " ".join(t.body for t in thread) or body
    period = period_from_any([body, root.body if root else "", text], ctx.ledger)
    key = ctx.room_key(ev.room_id) or "period"
    goals = next(iter(PLAN_ID_RE.findall(text)), "")
    cap = next(iter(CAP_RE.findall(text)), "")
    root_id = ev.thread_id or ev.message_id
    return (
        f"[Room {ctx.room_name(ev.room_id)} ({key}) · thread root {root_id} · asked by "
        f"{ctx.name_of(asker)}, mention as {ctx.mention(asker)} · period {period or '?'} · "
        f"goals {goals or '-'} · cost cap {cap or '-'}]"
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
        cap = _between(header_line, "cost cap ", "]").strip()
        ask = _between(header_line, "thread root ", " ·")
        in_planning = "(planning)" in header_line
        history = tool_history(messages)
        turns = human_turns(messages)
        if history:
            name, args, out, ok = history[-1]
            if not ok:
                return ("text", f"I can't propose that: {out}")
            if name == "propose_budget":
                who = approver_mention(ctx, Kind.BUDGET)
                return (
                    "text",
                    f"Budget for {month_label(period)}: the departments' lines under the cap "
                    f"{cap}.\n{out}\nYour call, {who}: `@{LEDGER_EXECUTOR} approve BUDGET-n` "
                    "with the id above.",
                )
            who = approver_mention(ctx, Kind.GOALS)
            return (
                "text",
                f"Goals for {month_label(period)}, from the ledger.\n{out}\n"
                f"Your call, {who}: `@{SWITCH_EXECUTOR} approve GOALS-n` with the id above.",
            )
        if in_planning:
            return (
                "tool",
                "propose_budget",
                {
                    "period": period,
                    "goals": goals if goals != "-" else "",
                    "cost_cap": cap if cap != "-" else "",
                    "justification": "the departments' shares of the approved cap",
                },
            )
        sent_back = next((t for t in turns if "sent back by" in t), "")
        summary = f"{month_label(period)} grows on the basis month" if period else "the plan"
        if sent_back:
            summary += f"; revised: {sent_back.split('sent back by', 1)[1].strip()[:80]}"
        return (
            "tool",
            "propose_goals",
            {
                "period": period,
                "title": f"Plan {month_label(period)}" if period else "Plan",
                "summary": summary,
                "ask": ask,
                "justification": "the business asked for the plan; targets from the ledger",
            },
        )

    return step


__all__ = ["NAME", "fake_step", "header", "money", "route", "tools", "trigger"]
