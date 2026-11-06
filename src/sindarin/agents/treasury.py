"""`treasury` (design v5 §4): proposes the payment run for an approved month's plan, from the
open payables the ledger holds. Cannot pay, cannot approve."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sindarin.agents.base import Outgoing, Tool, Turn, strict_tool, tool_history
from sindarin.agents.finance import _between, approver_mention, month_label, period_from_any
from sindarin.agents.roles import BRIEF_RE, CLOSE_ID_RE, RoleContext, split_fence_message
from sindarin.ledger import Ledger
from sindarin.names import LEDGER_EXECUTOR
from sindarin.propose import build_pay, render
from sindarin.schema import Kind
from sindarin.switch.events import MessageEvent

NAME = "treasury"

PROPOSE_PAYMENT_RUN = strict_tool(
    "propose_payment_run",
    "Validate a payment run for an approved plan: the open payables of the period as the ledger "
    "holds them, vendor, invoice, amount and total. Returns the block to post, or a one-line "
    "reason it is not allowed.",
    {
        "period": {"type": "string", "description": "YYYY-MM"},
        "plan": {"type": "string", "description": "CLOSE-n, from the stage line"},
        "justification": {"type": "string", "description": "one line"},
    },
    ["period", "plan", "justification"],
)


def tools(ctx: RoleContext) -> list[Tool]:
    def propose_payment_run(args: dict[str, Any]) -> tuple[str, bool]:
        if ctx.ledger is None:
            return "no ledger configured", False
        period = period_from_any([str(args.get("period", ""))], ctx.ledger)
        if not period:
            return "which period? none in the ledger", False
        rows = ctx.ledger.open_payables(period)
        if not rows:
            return f"no open payables in {period}", False
        rule = next(iter(ctx.policy().rules_for(Kind.PAY)), None)
        try:
            p = build_pay(
                ctx.policy(),
                period=period,
                plan=str(args.get("plan", "")),
                run=[
                    {
                        "invoice": r["invoice"],
                        "vendor": r["vendor"],
                        "amount_cents": r["amount_cents"],
                    }
                    for r in rows
                ],
                currency=rule.currency if rule else "USD",
                digest=Ledger.payables_digest(rows),
                justification=str(args.get("justification", "")),
            )
        except ValueError as exc:
            return f"Not allowed: {exc}", False
        return render(p), True

    return [Tool(PROPOSE_PAYMENT_RUN, propose_payment_run)]


def context_text(ctx: RoleContext, room_id: str = "") -> str:
    periods = ", ".join(ctx.ledger.periods()) if ctx.ledger is not None else "none"
    policy = ctx.policy_path.read_text(encoding="utf-8").strip()
    return (
        f"# The ledger\nRead-only. Periods it holds: {periods}. The run's rows and total come "
        "from `propose_payment_run`; you never type a figure.\n\n"
        f"# policy.yaml\n```yaml\n{policy}\n```"
    )


def trigger(ev: MessageEvent, ctx: RoleContext, thread: list[Turn]) -> bool:
    if ev.sender == ctx.self_id or ctx.room_key(ev.room_id) != "payments":
        return False
    body = ev.body or ""
    if ev.sender in ctx.executor_ids():
        if BRIEF_RE.search(body):
            return False  # the brief is context, not a call to act
        # the stage line ("@treasury propose the payment run"), a `changes`, a changed ledger
        return f"@{NAME}" in body
    return ev.addressed and ctx.is_human(ev.sender)  # a person who names me; chatter does not


def header(ev: MessageEvent, ctx: RoleContext, thread: list[Turn]) -> str:
    body = ev.body or ""
    text = " ".join(t.body for t in thread) or body
    period = period_from_any([body, text], ctx.ledger)
    plan = next(iter(CLOSE_ID_RE.findall(text)), "")
    return (
        f"[Room {ctx.room_name(ev.room_id)} (payments) · thread root "
        f"{ev.thread_id or ev.message_id} · period {period or '?'} · plan {plan or '-'}]"
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
        plan = _between(header_line, "plan ", "]").strip()
        history = tool_history(messages)
        if history:
            name, args, out, ok = history[-1]
            if not ok:
                return ("text", f"I can't propose that run: {out}")
            who = approver_mention(ctx, Kind.PAY)
            return (
                "text",
                f"Payment run for {month_label(period)}: the open payables as the ledger holds "
                f"them.\n{out}\nYour call, {who}: `@{LEDGER_EXECUTOR} approve PAY-n` with the id "
                "above.",
            )
        return (
            "tool",
            "propose_payment_run",
            {
                "period": period,
                "plan": plan if plan != "-" else "",
                "justification": "the approved plan names these payables",
            },
        )

    return step
