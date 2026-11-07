"""The file is the record: importing it twice leaves the books as one import would, and the
basis report lines always fit the schema (2026-11-07: a doubled sample month made the duplicates
line 224 characters long, the planner's tool refused, and GOALS-1 was never proposed)."""

from __future__ import annotations

from pathlib import Path

from sindarin.agents.finance import REPORT_LINE, report_lines
from sindarin.ledger import Ledger
from sindarin.schema import GoalsBody

SAMPLE = Path(__file__).resolve().parents[2] / "data" / "acme" / "2026-10"


def test_importing_the_same_month_twice_does_not_double_the_sales(tmp_path):
    ledger = Ledger(tmp_path / "books.db")
    first = ledger.import_dir(SAMPLE)
    once = ledger.summary("2026-10")
    second = ledger.import_dir(SAMPLE)
    twice = ledger.summary("2026-10")
    assert first["sales"] == second["sales"]
    assert len(ledger.sales("2026-10")) == first["sales"]
    assert twice["revenue_cents"] == once["revenue_cents"]
    assert twice["invoices"] == once["invoices"]
    assert twice["duplicate_invoices"] == once["duplicate_invoices"] == ["S-2026-1041"]
    # the sales journal was replaced too, so receivables did not double
    assert twice["by_customer"] == once["by_customer"]


def test_report_lines_fit_the_schema_whatever_the_books_hold():
    summary = {
        "revenue_cents": 8_990_500,
        "invoices": 12,
        "costs_cents": 1_425_000,
        "margin_bps": 8420,
        "targets": {"revenue_cents": 8_500_000, "margin_bps": 7500, "cost_cap_cents": 2_000_000},
        "payables": [{"status": "open", "amount_cents": 100} for _ in range(3)],
        "by_customer": {
            ("A very long customer name indeed " * 4).strip() + str(i): 1000 for i in range(3)
        },
        "duplicate_invoices": [f"S-2026-{1000 + i}" for i in range(40)],
    }
    lines = report_lines(summary)
    assert all(1 <= len(ln) <= REPORT_LINE for ln in lines)
    assert any(ln.startswith("Duplicate sales rows") and "and 37 more" in ln for ln in lines)
    GoalsBody.model_validate(
        {
            "title": "Plan November 2026",
            "summary": "a dirty import",
            "period": "2026-11",
            "basis": "2026-10",
            "revenue_target_cents": 1,
            "margin_target_bps": 1,
            "cost_cap_cents": 1,
            "currency": "USD",
            "report": lines,
        }
    )
