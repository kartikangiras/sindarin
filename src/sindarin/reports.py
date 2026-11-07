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
        held = ", ".join(ledger.periods()) or "no period at all"
        raise ReportError(
            f"the ledger has no period {period} yet; it holds {held}. A month exists once it is "
            "planned or has rows"
        )
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


@dataclass(frozen=True)
class ChartSpec:
    """One chart as the room reads it: a title and subtitle drawn inside the image, the bars,
    an optional marker (a target or a cap, drawn as a line with a label box), the caption
    posted above the image and the one-line reading posted after it."""

    title: str
    subtitle: str
    items: list[tuple[str, int]]
    caption: str
    reading: str
    marker: tuple[int, str] | None = None
    marker_good_above: bool = True  # revenue over its target is good; costs over the cap are not
    total: tuple[str, int] | None = None  # the bar the marker applies to, drawn last and darker
    pairs: list[tuple[str, int, int]] | None = None  # actual against target: (name, actual, ref)


def _pct(part: int, whole: int) -> str:
    return f"{part / whole * 100:.0f}%" if whole else "0%"


def _delta(
    actual: int, ref: int, currency: str, *, up: str, down: str, even: str = "exactly at"
) -> str:
    """`8,885.00 USD (10.5%) over`, `5,750.00 USD (28.7%) under`, or `exactly at` when equal."""
    diff = actual - ref
    if diff == 0:
        return even
    pct = f"{abs(diff) / ref * 100:.1f}%" if ref else "—"
    return f"{money(abs(diff), currency)} ({pct}) {up if diff > 0 else down}"


def _note(actual: int, ref: int, what: str) -> str:
    """The bold note drawn beside the total bar: no currency, the axis carries it."""
    diff = actual - ref
    if diff == 0:
        return f"exactly {'on' if what == 'target' else 'at'} the {what}"
    pct = f"{abs(diff) / ref * 100:.1f}%" if ref else "—"
    return f"{abs(diff) / 100:,.2f} ({pct}) {'over' if diff > 0 else 'under'} the {what}"


def _chart(ledger: Ledger, period: str, what: str, currency: str) -> ChartSpec:
    s = _summary(ledger, period)
    what = what.lower()
    label = month_label(period)
    t = s.get("targets") or {}
    if what == "revenue":
        items = sorted(s["by_customer"].items(), key=lambda kv: -kv[1])
        if not items:
            raise ReportError(f"no sales in {period}")
        total = int(s["revenue_cents"])
        n = int(s["invoices"])
        top, top_v = items[0]
        top3 = sum(v for _, v in items[:3])
        subtitle = f"{n} invoice{'s' if n != 1 else ''} · {money(total, currency)}"
        caption = f"**{label} — revenue by customer.** {money(total, currency)} across {n} invoices"
        marker = None
        if t:
            ref = int(t["revenue_cents"])
            subtitle += f" · target {money(ref, currency)}"
            caption += f", {_delta(total, ref, currency, up='over', down='under')} the target"
            marker = (ref, f"target {money(ref, currency)}")
        reading = (
            f"{top} is {_pct(top_v, total)} of the month; the top three customers carry "
            f"{_pct(top3, total)}."
        )
        return ChartSpec(
            f"{label} — revenue by customer",
            subtitle,
            items,
            caption + ".",
            reading,
            marker=marker,
            marker_good_above=True,
            total=("all customers", total),
        )
    if what == "budget":
        rows = s["budget"] or ledger.planned(period)
        if not rows:
            raise ReportError(f"no budget recorded for {period}")
        items = sorted(
            ((r["department"], int(r["amount_cents"])) for r in rows), key=lambda kv: -kv[1]
        )
        total = sum(v for _, v in items)
        top, top_v = items[0]
        subtitle = (
            f"{len(items)} department{'s' if len(items) != 1 else ''} · {money(total, currency)}"
        )
        caption = (
            f"**{label} — budget by department.** {money(total, currency)} over {len(items)} lines"
        )
        marker = None
        if t:
            cap = int(t["cost_cap_cents"])
            subtitle += f" · cap {money(cap, currency)}"
            caption += f", {_delta(total, cap, currency, up='over', down='under')} the cap"
            marker = (cap, f"cap {money(cap, currency)}")
        reading = f"{top} takes {_pct(top_v, total)} of the budget."
        return ChartSpec(
            f"{label} — budget by department",
            subtitle,
            items,
            caption + ".",
            reading,
            marker=marker,
            marker_good_above=False,
            total=("all departments", total),
        )
    if what == "costs":
        rows = s["payables"]
        if not rows:
            raise ReportError(f"no payables in {period}")
        by_vendor: dict[str, int] = {}
        for r in rows:
            by_vendor[r["vendor"]] = by_vendor.get(r["vendor"], 0) + int(r["amount_cents"])
        items = sorted(by_vendor.items(), key=lambda kv: -kv[1])
        total = int(s["costs_cents"])
        open_rows = [r for r in rows if r["status"] == "open"]
        top, top_v = items[0]
        subtitle = f"{len(rows)} payable{'s' if len(rows) != 1 else ''} · {money(total, currency)}"
        caption = (
            f"**{label} — costs by vendor.** {money(total, currency)} across {len(rows)} payables"
        )
        marker = None
        if t:
            cap = int(t["cost_cap_cents"])
            subtitle += f" · cap {money(cap, currency)}"
            caption += f", {_delta(total, cap, currency, up='over', down='under')} the cap"
            marker = (cap, f"cap {money(cap, currency)}")
        reading = f"{top} is {_pct(top_v, total)} of the month's costs"
        if open_rows:
            reading += (
                f"; {money(sum(int(r['amount_cents']) for r in open_rows), currency)} in "
                f"{len(open_rows)} payable{'s' if len(open_rows) != 1 else ''} still open."
            )
        else:
            reading += "; every payable is paid."
        return ChartSpec(
            f"{label} — costs by vendor",
            subtitle,
            items,
            caption + ".",
            reading,
            marker=marker,
            marker_good_above=False,
            total=("all vendors", total),
        )
    if what == "deals":
        rows = ledger.deals(period)
        if not rows:
            raise ReportError(f"no deals in {period}")
        items = [(f"{r['account']} ({r['product']})", int(r["price_cents"])) for r in rows]
        total = sum(v for _, v in items)
        listed = sum(int(r["list_cents"]) for r in rows)
        biggest = max(rows, key=lambda r: int(r["discount_bps"]))
        subtitle = (
            f"{len(rows)} deal{'s' if len(rows) != 1 else ''} · {money(total, currency)} · "
            f"list {money(listed, currency)}"
        )
        caption = (
            f"**{label} — deals by account.** {money(total, currency)} won against "
            f"{money(listed, currency)} of list price."
        )
        reading = (
            f"{biggest['account']} took {percent(int(biggest['discount_bps']))} off list"
            f"{' on ' + biggest['product'] if biggest.get('product') else ''}."
        )
        return ChartSpec(
            f"{label} — deals by account",
            subtitle,
            items,
            caption,
            reading,
            total=("all deals", total),
        )
    if what == "target":
        if not t:
            raise ReportError(f"no targets for {period}")
        rev, rev_t = int(s["revenue_cents"]), int(t["revenue_cents"])
        cost, cap = int(s["costs_cents"]), int(t["cost_cap_cents"])
        items = [
            ("revenue", rev),
            ("revenue target", rev_t),
            ("costs", cost),
            ("cost cap", cap),
        ]
        caption = (
            f"**{label} — actual against target.** Revenue {money(rev, currency)} against a "
            f"{money(rev_t, currency)} target; costs {money(cost, currency)} against a "
            f"{money(cap, currency)} cap."
        )
        reading = (
            f"Revenue is {_delta(rev, rev_t, currency, up='over', down='under')} the target; "
            f"costs are {_delta(cost, cap, currency, up='over', down='under')} the cap; margin "
            f"{percent(int(s['margin_bps']))} against {percent(int(t['margin_bps']))}."
        )
        return ChartSpec(
            f"{label} — actual against target",
            f"margin {percent(int(s['margin_bps']))} · target {percent(int(t['margin_bps']))}",
            items,
            caption,
            reading,
            pairs=[("revenue", rev, rev_t), ("costs", cost, cap)],
        )
    raise ReportError(f"I chart {', '.join(CHARTS)}, not {what}")


def _series(
    ledger: Ledger, period: str, what: str, currency: str
) -> tuple[str, list[tuple[str, int]]]:
    """The chart as a title and bars, for the text form."""
    spec = _chart(ledger, period, what, currency)
    s = _summary(ledger, period)
    label = month_label(period)
    what = what.lower()
    if what == "target":
        return f"{label} actual against target", spec.items
    totals = {
        "revenue": ("revenue by customer", int(s["revenue_cents"])),
        "budget": ("budget by department", spec.total[1] if spec.total else 0),
        "costs": ("costs by vendor", int(s["costs_cents"])),
        "deals": ("deals by account", spec.total[1] if spec.total else 0),
    }
    name, total = totals[what]
    return f"{label} {name} · {money(total, currency)}", spec.items


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


# the look of a chart in the room: title and subtitle inside the image, a light frame, one
# accent, the total bar darker, a target or cap as a black-labelled line, the region past it
# shaded green when that is good and red when it is not
_ACCENT, _ACCENT_DARK, _REF = "#2563eb", "#1e3a8a", "#9ca3af"
_FRAME, _GRID, _INK, _MUTED = "#f3f4f6", "#e5e7eb", "#111111", "#555555"
_GOOD, _BAD = "#e3f4e8", "#fde2e2"
_GOOD_INK, _BAD_INK = "#166534", "#991b1b"


def chart_png(ledger: Ledger, period: str, what: str, currency: str = "USD") -> Output:
    """The chart as a PNG to post inline in the thread, drawn with matplotlib."""
    spec = _chart(ledger, period, what, currency)
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter

    rows = len(spec.pairs) * 2 if spec.pairs else len(spec.items) + (1 if spec.total else 0)
    height = max(3.4, 0.46 * rows + 1.9)
    fig = plt.figure(figsize=(8.4, height), dpi=144)
    fig.patch.set_facecolor(_FRAME)
    inch = 1 / height  # figure fractions from inches, top down
    fig.text(0.035, 1 - 0.42 * inch, spec.title, fontsize=16, fontweight="bold", color=_INK)
    fig.text(0.035, 1 - 0.68 * inch, spec.subtitle, fontsize=10, color=_MUTED)
    axes_top = 1 - 1.02 * inch
    ax = fig.add_axes((0.24, 0.62 * inch, 0.70, axes_top - 0.62 * inch))
    ax.set_facecolor("white")
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _p: f"{v:,.0f}"))
    ax.grid(axis="x", color=_GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.spines[["top", "right", "left"]].set_visible(False)
    ax.spines["bottom"].set_color(_GRID)
    ax.tick_params(axis="both", colors=_MUTED, labelsize=9, length=0)
    ax.set_xlabel(currency, color=_MUTED, fontsize=9)

    def label(bar: Any, value: float, *, inside: bool = False) -> None:
        if inside:
            ax.text(
                bar.get_width(),
                bar.get_y() + bar.get_height() / 2,
                f"{value:,.2f}  ",
                va="center",
                ha="right",
                fontsize=9,
                fontweight="bold",
                color="white",
            )
        else:
            ax.text(
                bar.get_width(),
                bar.get_y() + bar.get_height() / 2,
                f"  {value:,.2f}",
                va="center",
                fontsize=9,
                color=_INK,
            )

    if spec.pairs:
        names: list[str] = []
        values: list[float] = []
        colors: list[str] = []
        for name, actual, ref in spec.pairs:
            names += [name, f"{name} {'target' if name == 'revenue' else 'cap'}"]
            values += [actual / 100, ref / 100]
            colors += [_ACCENT, _REF]
        bars = ax.barh(names, values, color=colors, height=0.68)
        ax.invert_yaxis()
        for bar, value in zip(bars, values, strict=True):
            label(bar, value)
        for i, (name, actual, ref) in enumerate(spec.pairs):
            good = actual >= ref if name == "revenue" else actual <= ref
            note = _note(actual, ref, "target" if name == "revenue" else "cap")
            ax.text(
                max(values) * 1.5 * 0.99,
                i * 2 + 0.5,
                note,
                ha="right",
                va="center",
                fontsize=9,
                fontweight="bold",
                color=_GOOD_INK if good else _BAD_INK,
            )
        ax.set_xlim(0, max(values) * 1.5)
    else:
        items = list(spec.items) + ([spec.total] if spec.total else [])
        names = [n for n, _ in items]
        values = [v / 100 for _, v in items]
        colors = [_ACCENT] * len(spec.items) + ([_ACCENT_DARK] if spec.total else [])
        bars = ax.barh(names, values, color=colors, height=0.68)
        ax.invert_yaxis()
        xmax = max(values + ([spec.marker[0] / 100] if spec.marker else [])) * (
            1.55 if spec.marker else 1.3
        )
        ax.set_xlim(0, xmax)
        if spec.marker:
            ref = spec.marker[0] / 100
            total_v = values[-1] if spec.total else max(values)
            above = total_v > ref
            good = above == spec.marker_good_above or total_v == ref
            if above:
                ax.axvspan(ref, total_v, color=_GOOD if good else _BAD, zorder=0)
            ax.axvline(ref, color=_INK, linewidth=1.6, zorder=3)
            ax.text(
                ref,
                -0.62,
                f" {spec.marker[1]} ",
                ha="center",
                va="bottom",
                fontsize=9.5,
                fontweight="bold",
                color="white",
                bbox={"boxstyle": "round,pad=0.35", "fc": _INK, "ec": "none"},
                zorder=4,
            )
            if spec.total:
                note = _note(
                    spec.total[1], spec.marker[0], "target" if spec.marker_good_above else "cap"
                )
                ax.text(
                    xmax * 0.99,
                    len(values) - 1 - 0.52,
                    note,
                    ha="right",
                    va="center",
                    fontsize=9,
                    fontweight="bold",
                    color=_GOOD_INK if good else _BAD_INK,
                    zorder=5,
                )
        for i, (bar, value) in enumerate(zip(bars, values, strict=True)):
            label(bar, value, inside=bool(spec.total) and i == len(values) - 1)
        if spec.total:
            ax.get_yticklabels()[-1].set_fontweight("bold")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    return Output(
        text=spec.caption,
        filename=f"{what}-{period}.png",
        data=buf.getvalue(),
        mime="image/png",
        rows=len(spec.items),
        facts={"period": period, "what": what, "reading": spec.reading},
    )
