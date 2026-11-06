"""Reports, exports and charts from the ledger (design v5, reporting rail). Deterministic: every
number comes from the ledger rows, no model is involved. The text forms work in any room; the
PNG charts and the CSV files ride Switch attachments when the executor can post files."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from typing import Any

from sindarin.agents.finance import month_label, render_report
from sindarin.ledger import Ledger
from sindarin.schema import money, percent

EXPORTS = ("sales", "payables", "budget", "planned", "targets", "deals", "journal")
CHARTS = ("revenue", "budget", "costs", "target", "deals")


@dataclass(frozen=True)
class Output:
    """What a report command produces: text for the room, and maybe a file to attach."""

    text: str
    filename: str = ""
    data: bytes = b""
    mime: str = ""
    rows: int = 0
    facts: dict[str, Any] = field(default_factory=dict)


class ReportError(Exception):
    """A refusal the room can read: unknown period, nothing to show, unknown export."""


def _summary(ledger: Ledger, period: str) -> dict[str, Any]:
    if period not in ledger.periods():
        raise ReportError(f"the ledger has no period {period}")
    return ledger.summary(period)


# -- report ---------------------------------------------------------------------------------------


def report(ledger: Ledger, period: str, currency: str = "USD") -> Output:
    """The period as a person reads it: the analyst's table, the targets, the recorded budget,
    the planned amounts, and budget against actual once the month has both."""
    s = _summary(ledger, period)
    lines = [render_report(s, currency)]
    if s["budget"]:
        total = sum(int(r["amount_cents"]) for r in s["budget"])
        lines += [
            "",
            f"Budget ({s['budget'][0]['source']}): **{money(total, currency)}** · "
            + ", ".join(
                f"{r['department']} {money(r['amount_cents'], currency)}" for r in s["budget"]
            ),
        ]
    planned = ledger.planned(period)
    if planned:
        lines.append(
            f"Planned in the books ({planned[0]['source']}): "
            f"**{money(sum(int(r['amount_cents']) for r in planned), currency)}** across "
            f"{len(planned)} lines"
        )
    if s["budget"] and s["sales_rows"]:
        spent = s["costs_cents"]
        total = sum(int(r["amount_cents"]) for r in s["budget"])
        lines.append(
            f"Budget vs actual: costs **{money(spent, currency)}** against a budget of "
            f"**{money(total, currency)}** "
            f"({percent(round(spent * 10000 / total) if total else 0)})"
        )
    return Output(text="\n".join(lines), rows=s["sales_rows"], facts={"period": period})


# -- export ---------------------------------------------------------------------------------------


def export(ledger: Ledger, period: str, what: str) -> Output:
    """One table of the period as CSV: sales, payables, budget, planned, targets or journal."""
    what = what.lower()
    if what not in EXPORTS:
        raise ReportError(f"I export {', '.join(EXPORTS)}, not {what}")
    _summary(ledger, period)
    if what == "sales":
        rows = ledger.sales(period)
    elif what == "payables":
        rows = ledger.payables(period=period)
    elif what == "budget":
        rows = ledger.budget(period)
    elif what == "planned":
        rows = ledger.planned(period)
    elif what == "targets":
        t = ledger.targets(period)
        rows = [t] if t else []
    elif what == "deals":
        rows = ledger.deals(period)
    else:
        rows = [
            {**j, **line}
            for j in ledger.journal(period)
            for line in _journal_lines(ledger, j["id"])
        ]
    if not rows:
        raise ReportError(f"no {what} rows in {period}")
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    for r in rows:
        writer.writerow({k: ("" if v is None else v) for k, v in r.items()})
    data = buf.getvalue().encode("utf-8")
    total = sum(int(r.get("amount_cents") or r.get("price_cents") or 0) for r in rows)
    text = f"**{what} {period}** · {len(rows)} row{'s' if len(rows) != 1 else ''}" + (
        f" · {money(total)}" if total else ""
    )
    return Output(
        text=text,
        filename=f"{what}-{period}.csv",
        data=data,
        mime="text/csv",
        rows=len(rows),
        facts={"period": period, "what": what},
    )


def _journal_lines(ledger: Ledger, journal_id: int) -> list[dict[str, Any]]:
    rows = ledger._db.execute(
        "SELECT account, debit_cents, credit_cents FROM lines WHERE journal_id=?", (journal_id,)
    ).fetchall()
    return [dict(r) for r in rows]


# -- charts ---------------------------------------------------------------------------------------


def _series(
    ledger: Ledger, period: str, what: str, currency: str
) -> tuple[str, list[tuple[str, int]]]:
    s = _summary(ledger, period)
    what = what.lower()
    label = month_label(period)
    if what == "revenue":
        items = list(s["by_customer"].items())
        if not items:
            raise ReportError(f"no sales in {period}")
        return f"{label} revenue by customer · {money(s['revenue_cents'], currency)}", items
    if what == "budget":
        rows = s["budget"] or ledger.planned(period)
        if not rows:
            raise ReportError(f"no budget recorded for {period}")
        total = sum(int(r["amount_cents"]) for r in rows)
        return f"{label} budget by department · {money(total, currency)}", [
            (r["department"], int(r["amount_cents"])) for r in rows
        ]
    if what == "costs":
        rows = s["payables"]
        if not rows:
            raise ReportError(f"no payables in {period}")
        return f"{label} costs by vendor · {money(s['costs_cents'], currency)}", [
            (r["vendor"], int(r["amount_cents"])) for r in rows
        ]
    if what == "deals":
        rows = ledger.deals(period)
        if not rows:
            raise ReportError(f"no deals in {period}")
        total = sum(int(r["price_cents"]) for r in rows)
        return f"{label} deals by account · {money(total, currency)}", [
            (f"{r['account']} ({r['product']})", int(r["price_cents"])) for r in rows
        ]
    if what == "target":
        t = s["targets"]
        if not t:
            raise ReportError(f"no targets for {period}")
        return f"{label} actual against target", [
            ("revenue", int(s["revenue_cents"])),
            ("revenue target", int(t["revenue_cents"])),
            ("costs", int(s["costs_cents"])),
            ("cost cap", int(t["cost_cap_cents"])),
        ]
    raise ReportError(f"I chart {', '.join(CHARTS)}, not {what}")


def chart_text(ledger: Ledger, period: str, what: str, currency: str = "USD") -> Output:
    """A bar chart in a code block: readable in every room, needs no file."""
    title, items = _series(ledger, period, what, currency)
    top = max(v for _, v in items) or 1
    width = max(len(name) for name, _ in items)
    lines = [f"**{title}**", "```"]
    for name, value in items:
        bar = "█" * max(1, round(value / top * 28)) if value > 0 else ""
        lines.append(f"{name:<{width}}  {bar:<28}  {money(value, currency)}")
    lines.append("```")
    return Output(text="\n".join(lines), rows=len(items), facts={"period": period, "what": what})


def chart_png(ledger: Ledger, period: str, what: str, currency: str = "USD") -> Output:
    """The same bars as a PNG, drawn with matplotlib, to attach to the room."""
    title, items = _series(ledger, period, what, currency)
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    names = [n for n, _ in items]
    values = [v / 100 for _, v in items]
    fig, ax = plt.subplots(figsize=(7.2, max(2.4, 0.5 * len(items) + 1.2)), dpi=144)
    bars = ax.barh(names, values, color="#2f6fde")
    ax.invert_yaxis()
    ax.set_title(title, loc="left", fontsize=11, fontweight="bold")
    ax.set_xlabel(currency)
    ax.spines[["top", "right"]].set_visible(False)
    for bar, value in zip(bars, values, strict=True):
        ax.text(
            bar.get_width(),
            bar.get_y() + bar.get_height() / 2,
            f" {value:,.2f}",
            va="center",
            fontsize=8,
        )
    ax.margins(x=0.25)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return Output(
        text=f"**{title}** · from the ledger",
        filename=f"{what}-{period}.png",
        data=buf.getvalue(),
        mime="image/png",
        rows=len(items),
        facts={"period": period, "what": what},
    )
