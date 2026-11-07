"""What the three finance agents share (design v5 §4): the period in a message, the month's
report rendered from the ledger, the goals computed from it, the standard budget lines. Every
number here is computed; the model only ever writes the caption lines around it."""

from __future__ import annotations

import re
from typing import Any

from sindarin.agents.roles import RoleContext
from sindarin.ledger import Ledger
from sindarin.schema import Kind, money, percent, previous_period, utcnow

PERIOD_RE = re.compile(r"\b(20\d{2})-(0[1-9]|1[0-2])\b")
MONEY_RE = re.compile(r"(\d{1,3}(?:,\d{3})*|\d+)(?:\.(\d{2}))?")
MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
# the chart of accounts the sample data uses (data/acme/*/accounts.csv)
CASH, RECEIVABLE, ACCUM_DEPRECIATION, ACCRUED = "1000", "1100", "1500", "2100"
REVENUE, OPEX, DEPRECIATION, PAYROLL = "4000", "6000", "6100", "6200"
# how the cap is split across departments: share in basis points, the account it lands on
DEPARTMENTS: tuple[tuple[str, int, str], ...] = (
    ("engineering", 4500, PAYROLL),
    ("sales", 2500, OPEX),
    ("marketing", 1500, OPEX),
    ("general-admin", 1500, OPEX),
)


def month_label(period: str) -> str:
    year, month = period.split("-")
    return f"{MONTHS[int(month) - 1]} {year}"


def period_from(text: str, ledger: Ledger | None) -> str:
    """The period a message names (`2026-11`, or a month word), else the month after the
    ledger's newest. A month word with no period in the ledger takes the newest period's year."""
    m = PERIOD_RE.search(text or "")
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    known = ledger.periods() if ledger is not None else []
    low = (text or "").lower()
    for i, name in enumerate(MONTHS, 1):
        if re.search(rf"\b{name.lower()}\b", low):
            for p in reversed(known):
                if p.endswith(f"-{i:02d}"):
                    return p
            year = int(known[-1][:4]) if known else utcnow().year
            return f"{year}-{i:02d}"
    return known[-1] if known else ""


def names_period(text: str) -> bool:
    """Whether a text names a period at all (`2026-11` or a month word)."""
    low = (text or "").lower()
    return bool(PERIOD_RE.search(text or "")) or any(
        re.search(rf"\b{m.lower()}\b", low) for m in MONTHS
    )


def period_from_any(texts: list[str], ledger: Ledger | None) -> str:
    """The period named by the first text that names one (the trigger before the thread, so a
    stage line's `period 2026-11` beats a fence's basis month), else the ledger's fallback."""
    for t in texts:
        if names_period(t):
            return period_from(t, ledger)
    return period_from("", ledger)


def report_period(period: str, ledger: Ledger | None) -> str:
    """The month the analyst reports: the period itself when it has sales rows, else the one
    before it (the basis of a plan for a month that has not started)."""
    if ledger is None or not period:
        return period
    if ledger.sales(period):
        return period
    before = previous_period(period)
    return before if ledger.sales(before) else period


def parse_money(text: str) -> int:
    """`16,000.00 USD` or `16000` → cents; 0 when nothing reads as money."""
    m = MONEY_RE.search(text or "")
    if not m:
        return 0
    whole = int(m.group(1).replace(",", ""))
    return whole * 100 + int(m.group(2) or 0)


REPORT_LINE = 160  # schema.GoalsBody / CloseBody: each report line is 1-160 characters
REPORT_IDS = 3  # duplicate invoice ids named before "and N more"


def report_lines(s: dict[str, Any], currency: str = "USD") -> list[str]:
    """The month in a handful of lines: what the goals carry as their basis."""
    top = list(s["by_customer"].items())[:3]
    t = s.get("targets") or {}
    revenue = f"Revenue {money(s['revenue_cents'], currency)} across {s['invoices']} invoices"
    if t:
        revenue += f" against a target of {money(t['revenue_cents'], currency)}"
    costs = f"Costs {money(s['costs_cents'], currency)} ({len(s['payables'])} payables)"
    if t:
        costs += f" against a cap of {money(t['cost_cap_cents'], currency)}"
    margin = f"Margin {percent(s['margin_bps'])}"
    if t:
        margin += f" against a target of {percent(t['margin_bps'])}"
    open_rows = [p for p in s["payables"] if p["status"] == "open"]
    lines = [
        revenue,
        costs,
        margin,
        "Top customers: " + ", ".join(f"{c} {money(v, currency)}" for c, v in top),
        f"Open payables {money(sum(p['amount_cents'] for p in open_rows), currency)} "
        f"({len(open_rows)} invoices)",
    ]
    dups = list(s["duplicate_invoices"])
    if dups:
        shown = ", ".join(dups[:REPORT_IDS])
        more = f" and {len(dups) - REPORT_IDS} more" if len(dups) > REPORT_IDS else ""
        lines.append(f"Duplicate sales rows excluded from revenue: {shown}{more}")
    # GoalsBody/CloseBody take report lines of at most REPORT_LINE characters each; a month with
    # long customer names or a dirty import must still produce a proposal, not a dead end.
    return [ln if len(ln) <= REPORT_LINE else ln[: REPORT_LINE - 1] + "…" for ln in lines]


def render_report(s: dict[str, Any], currency: str = "USD") -> str:
    """The analyst's post: a table and the anomalies, nothing a model could invent."""
    label = month_label(s["period"])
    t = s.get("targets") or {}
    head = (
        f"**{label}** · revenue **{money(s['revenue_cents'], currency)}** across "
        f"**{s['invoices']}** invoices"
    )
    if t:
        head += f" · target **{money(t['revenue_cents'], currency)}**"
    head += f" · period {s['period_status']}"
    lines = [head, "", "| Customer | Revenue |", "| --- | ---: |"]
    for customer, cents in s["by_customer"].items():
        lines.append(f"| {customer} | {money(cents, currency)} |")
    lines.append("")
    costs = f"Costs: **{money(s['costs_cents'], currency)}** ({len(s['payables'])} payables)"
    if t:
        costs += f" · cap **{money(t['cost_cap_cents'], currency)}**"
    margin = f"Margin: **{percent(s['margin_bps'])}**"
    if t:
        margin += f" · target **{percent(t['margin_bps'])}**"
    lines.append(f"{costs} · {margin}")
    if s["duplicate_invoices"]:
        lines.append(
            "Anomaly: "
            + ", ".join(s["duplicate_invoices"])
            + " appears twice in the sales file; counted once above."
        )
    return "\n".join(lines)


def _round_up(cents: int, step: int = 100000) -> int:
    return ((cents + step - 1) // step) * step


def plan_goals(ledger: Ledger, period: str, basis: str) -> dict[str, int]:
    """The goals for `period` from the basis month's actuals: revenue up five percent, costs
    allowed up ten percent, both rounded up to the thousand; the margin they imply."""
    s = ledger.summary(basis)
    revenue = _round_up(s["revenue_cents"] * 105 // 100)
    cap = _round_up(s["costs_cents"] * 110 // 100)
    if cap >= revenue:
        cap = max(revenue - 100000, 100000)
    margin = round((revenue - cap) * 10000 / revenue) if revenue else 0
    return {
        "revenue_target_cents": int(revenue),
        "margin_target_bps": int(margin),
        "cost_cap_cents": int(cap),
    }


def standard_budget_lines(cap_cents: int) -> list[dict[str, Any]]:
    """The cap split across the departments by their shares, each rounded down to the hundred,
    so the lines always sum under the cap."""
    lines = []
    for department, share_bps, account in DEPARTMENTS:
        cents = (cap_cents * share_bps // 10000) // 10000 * 10000
        if cents > 0:
            lines.append({"department": department, "account": account, "amount_cents": cents})
    return lines


def standard_entries(ledger: Ledger, period: str) -> list[dict[str, Any]]:
    """The month's adjusting entries, computed, never typed: straight-line depreciation,
    accrued payroll, and a reversal of any duplicate sales row the analyst found."""
    s = ledger.summary(period)
    entries: list[dict[str, Any]] = [
        {
            "memo": f"Depreciation {month_label(period)}",
            "lines": [
                {"account": DEPRECIATION, "debit_cents": 120000},
                {"account": ACCUM_DEPRECIATION, "credit_cents": 120000},
            ],
        },
        {
            "memo": f"Accrued payroll {month_label(period)}",
            "lines": [
                {"account": PAYROLL, "debit_cents": 650000},
                {"account": ACCRUED, "credit_cents": 650000},
            ],
        },
    ]
    amounts = {row["invoice"]: row["amount_cents"] for row in ledger.sales(period)}
    for inv in s["duplicate_invoices"]:
        cents = int(amounts.get(inv, 0))
        if cents > 0:
            entries.append(
                {
                    "memo": f"Reverse duplicate sale {inv}",
                    "lines": [
                        {"account": REVENUE, "debit_cents": cents},
                        {"account": RECEIVABLE, "credit_cents": cents},
                    ],
                }
            )
    return entries


def approver_mention(ctx: RoleContext, kind: Kind) -> str:
    """`@Name` of the first member of the group that approves this kind (display only). For
    a kind with a limit, the group that may approve alone below it."""
    p = ctx.policy()
    rule = next(iter(p.rules_for(kind)), None)
    group = rule.approvers_under_limit or rule.approvers if rule else ""
    members = p.group_members(group) if rule else []
    return ctx.mention(members[0]) if members else f"@{group or 'approver'}"


def _between(text: str, start: str, end: str) -> str:
    if start not in text:
        return ""
    rest = text.split(start, 1)[1]
    return rest.split(end, 1)[0] if end in rest else rest.rstrip("]")
