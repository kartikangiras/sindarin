"""`sales` (design v5 §4, the sales chain): registers a deal at a price. The list price comes
from the ledger's price book, never from the ask; the discount that follows picks who approves;
`sindarin-ledger` records the won deal and lands its sale in the books. Cannot discount,
cannot approve, cannot book."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from sindarin.adapters.ledger import deal_facts
from sindarin.agents.base import Outgoing, Tool, Turn, strict_tool, tool_history
from sindarin.agents.finance import _between, approver_mention, parse_money, period_from_any
from sindarin.agents.roles import BRIEF_RE, RoleContext, split_fence_message
from sindarin.ledger import Ledger
from sindarin.names import LEDGER_EXECUTOR
from sindarin.propose import build_deal, render
from sindarin.schema import Kind
from sindarin.switch.events import MessageEvent

NAME = "sales"
DEAL_RE = re.compile(r"\bD-\d{4}-\d{2,5}\b")
# the structured ask: "deal Contoso Ltd on Team at 10,200.00 net60"
ASK_RE = re.compile(
    r"deal\s+(?P<account>.+?)\s+on\s+(?P<product>[A-Za-z][A-Za-z0-9-]*)\s+at\s+"
    r"(?P<price>\d{1,3}(?:,\d{3})+|\d+)(?:\.(?P<cents>\d{2}))?"
    r"(?:\s+(?P<terms>standard|net60|sla))?",
    re.IGNORECASE,
)

PROPOSE_DEAL = strict_tool(
    "propose_deal",
    "Validate a deal at a price: the product's list price as the ledger's price book holds it, "
    "the discount that follows, the terms. Returns the block to post, or a one-line reason it "
    "is not allowed.",
    {
        "account": {"type": "string", "description": "the customer, as asked"},
        "product": {"type": "string", "description": "a product in the price book"},
        "price": {"type": "string", "description": "the price asked, e.g. 10,200.00"},
        "terms": {"type": "string", "enum": ["standard", "net60", "sla"]},
        "period": {
            "type": "string",
            "description": "YYYY-MM the sale lands in; empty for the open month",
        },
        "deal_id": {
            "type": "string",
            "description": "D-yyyy-nn from the thread when re-proposing; empty for a new deal",
        },
        "justification": {"type": "string", "description": "one line"},
    },
    ["account", "product", "price", "terms", "period", "deal_id", "justification"],
)


def open_period(ledger: Ledger | None) -> str:
    """The newest month that is not locked: where a deal lands when the ask names none."""
    if ledger is None:
        return ""
    periods = ledger.periods()
    live = [p for p in periods if ledger.period_status(p) != "locked"]
    return (live or periods or [""])[-1]


def tools(ctx: RoleContext) -> list[Tool]:
    def propose_deal(args: dict[str, Any]) -> tuple[str, bool]:
        if ctx.ledger is None:
            return "no ledger configured", False
        period = period_from_any([str(args.get("period", ""))], ctx.ledger) or open_period(
            ctx.ledger
        )
        if not period:
            return "which period? none in the ledger", False
        product = str(args.get("product", "")).strip()
        book = ctx.ledger.price_book()
        if product not in book:
            return (
                f"{product or '?'} is not in the price book ({', '.join(book) or 'empty'})",
                False,
            )
        try:
            price = parse_money(str(args.get("price", "")))
        except ValueError:
            return "which price? e.g. 10,200.00", False
        if price <= 0:
            return "the price must be above zero", False
        deal_id = str(args.get("deal_id", "")).strip().upper() or ctx.ledger.next_deal_id(period)
        rule = next(iter(ctx.policy().rules_for(Kind.DEAL)), None)
        facts = deal_facts(ctx.ledger, period, product, deal_id, book[product] - price)
        try:
            p = build_deal(
                ctx.policy(),
                period=period,
                deal_id=deal_id,
                account=str(args.get("account", "")).strip(),
                product=product,
                list_cents=book[product],
                price_cents=price,
                terms=str(args.get("terms") or "standard").lower(),
                currency=rule.currency if rule else "USD",
                digest=str(facts["digest"]),
                justification=str(args.get("justification", "")),
            )
        except ValueError as exc:
            return f"Not allowed: {exc}", False
        return render(p), True

    return [Tool(PROPOSE_DEAL, propose_deal)]


def context_text(ctx: RoleContext, room_id: str = "") -> str:
    book = ctx.ledger.price_book() if ctx.ledger is not None else {}
    prices = ", ".join(f"{k} {v / 100:,.2f}" for k, v in book.items()) or "empty"
    policy = ctx.policy_path.read_text(encoding="utf-8").strip()
    return (
        f"# The price book\nRead-only. {prices}. The list price, the discount and the deal id "
        "come from `propose_deal`; you never type a figure.\n\n"
        f"# policy.yaml\n```yaml\n{policy}\n```"
    )


def trigger(ev: MessageEvent, ctx: RoleContext, thread: list[Turn]) -> bool:
    if ev.sender == ctx.self_id or ctx.room_key(ev.room_id) != "sales":
        return False
    body = ev.body or ""
    if ev.sender in ctx.executor_ids():
        if BRIEF_RE.search(body):
            return False
        # a `changes DEAL-n`, a changed price book: "@sales please propose again"
        return f"@{NAME}" in body
    return ev.addressed and ctx.is_human(ev.sender)  # a person who names me; chatter does not


def header(ev: MessageEvent, ctx: RoleContext, thread: list[Turn]) -> str:
    body = ev.body or ""
    text = " ".join(t.body for t in thread) or body
    m = ASK_RE.search(body) or ASK_RE.search(text)
    months = [DEAL_RE.sub("", body), DEAL_RE.sub("", text)]  # D-2026-01 is a deal, not January
    period = period_from_any(months, ctx.ledger) or open_period(ctx.ledger)
    deal_id = next(iter(DEAL_RE.findall(text)), "") or next(iter(DEAL_RE.findall(body)), "")
    account = m.group("account").strip() if m else ""
    product = m.group("product") if m else ""
    price = ""
    if m:
        price = m.group("price") + (f".{m.group('cents')}" if m.group("cents") else "")
    terms = (m.group("terms") or "standard").lower() if m else "standard"
    return (
        f"[Room {ctx.room_name(ev.room_id)} (sales) · thread root "
        f"{ev.thread_id or ev.message_id} · period {period or '?'} · deal {deal_id or '-'} · "
        f"account {account} · product {product} · price {price} · terms {terms}]"
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
        deal_id = _between(header_line, "deal ", " ·").strip()
        account = _between(header_line, "account ", " ·").strip()
        product = _between(header_line, "product ", " ·").strip()
        price = _between(header_line, "price ", " ·").strip()
        terms = _between(header_line, "terms ", "]").strip() or "standard"
        history = tool_history(messages)
        if history:
            name, args, out, ok = history[-1]
            if not ok:
                return ("text", f"I can't register that deal: {out}")
            who = approver_mention(ctx, Kind.DEAL)
            return (
                "text",
                f"Deal for {account}: {product} at {price}, priced against the price book.\n"
                f"{out}\nYour call, {who}: `@{LEDGER_EXECUTOR} approve DEAL-n` with the id above.",
            )
        if not (account and product and price):
            return (
                "text",
                "Tell me the deal as `deal <account> on <product> at <price>` "
                "(terms standard, net60 or sla).",
            )
        return (
            "tool",
            "propose_deal",
            {
                "account": account,
                "product": product,
                "price": price,
                "terms": terms,
                "period": "" if period == "?" else period,
                "deal_id": "" if deal_id == "-" else deal_id,
                "justification": "the deal as asked, priced against the price book",
            },
        )

    return step
