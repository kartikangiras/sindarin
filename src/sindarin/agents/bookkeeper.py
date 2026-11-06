"""`bookkeeper` (design v5 §4): proposes the budget register entries once the period's budget
is recorded, from the lines the ledger holds. Cannot post, approve or plan anything."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sindarin.agents.base import Outgoing, Tool, Turn, strict_tool, tool_history
from sindarin.agents.finance import _between, approver_mention, month_label, period_from
from sindarin.agents.roles import (
    BRIEF_RE,
    BUDGET_ID_RE,
    PLAN_ID_RE,
    RoleContext,
    split_fence_message,
)
from sindarin.ledger import Ledger
from sindarin.names import LEDGER_EXECUTOR
from sindarin.propose import build_entries, render
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


def tools(ctx: RoleContext) -> list[Tool]:
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

    return [Tool(PROPOSE_ENTRIES, propose_entries)]


def context_text(ctx: RoleContext, room_id: str = "") -> str:
    periods = ", ".join(ctx.ledger.periods()) if ctx.ledger is not None else "none"
    brief = ctx.brief(room_id)
    policy = ctx.policy_path.read_text(encoding="utf-8").strip()
    return (
        f"# The ledger\nRead-only. Periods it holds: {periods}. Lines and totals come from the "
        "tool; you never type a figure.\n\n"
        f"# The goals (room instructions)\n{brief or '(not a plan room, or not seen yet)'}\n\n"
        f"# policy.yaml\n```yaml\n{policy}\n```"
    )


def trigger(ev: MessageEvent, ctx: RoleContext, thread: list[Turn]) -> bool:
    if ev.sender == ctx.self_id:
        return False
    body = ev.body or ""
    if ev.sender in ctx.executor_ids():
        if BRIEF_RE.search(body):
            return False  # the brief is context, not a call to act
        # the stage line in the plan room ("@bookkeeper propose the budget register entries"),
        # or a `changes` sent back naming me
        return f"@{NAME}" in body and ctx.is_period_room(ev.room_id)
    return False


def header(ev: MessageEvent, ctx: RoleContext, thread: list[Turn]) -> str:
    body = ev.body or ""
    text = " ".join(t.body for t in thread) or body
    period = period_from(text + " " + ctx.brief(ev.room_id), ctx.ledger)
    key = ctx.room_key(ev.room_id) or "period"
    goals = next(iter(PLAN_ID_RE.findall(ctx.brief(ev.room_id) + " " + text)), "")
    budget = next(iter(BUDGET_ID_RE.findall(text)), "")
    root_id = ev.thread_id or ev.message_id
    return (
        f"[Room {ctx.room_name(ev.room_id)} ({key}) · thread root {root_id} · period "
        f"{period or '?'} · goals {goals or '-'} · budget {budget or '-'}]"
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
        budget = _between(header_line, "budget ", "]").strip()
        history = tool_history(messages)
        if history:
            name, args, out, ok = history[-1]
            if not ok:
                return ("text", f"I can't propose that: {out}")
            who = approver_mention(ctx, Kind.ENTRIES)
            return (
                "text",
                f"Budget register entries for {month_label(period)}: the recorded lines into "
                f"the books.\n{out}\nYour call, {who}: `@{LEDGER_EXECUTOR} approve ENTRIES-n` "
                "with the id above.",
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
