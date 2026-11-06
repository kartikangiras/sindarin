"""The reporting rail (design v5): reports, exports and charts come from the ledger rows, as
text in any room and as files where Switch can carry them."""

from __future__ import annotations

import csv
import io

import pytest
from validation.harness import (
    BASIS,
    DATA,
    FINANCE,
    JOHN,
    PERIOD,
    PLANNING,
    Harness,
)

from sindarin import reports
from sindarin.agents.finance import standard_budget_lines
from sindarin.executor import core
from sindarin.ledger import Ledger
from sindarin.names import SWITCH_EXECUTOR


@pytest.fixture
def books(tmp_path):
    ledger = Ledger(tmp_path / "books.db")
    ledger.import_dir(DATA / BASIS)
    ledger.record_budget(PERIOD, "BUDGET-1", standard_budget_lines(1600000))
    yield ledger
    ledger.close()


def test_report_export_and_charts_come_from_the_rows(books):
    out = reports.report(books, BASIS)
    assert "**October 2026** · revenue **89,905.00 USD**" in out.text
    assert "target **85,000.00 USD**" in out.text and out.rows == 13
    nov = reports.report(books, PERIOD)
    assert "Budget (BUDGET-1): **16,000.00 USD**" in nov.text
    with pytest.raises(reports.ReportError, match="no period 2027-01"):
        reports.report(books, "2027-01")

    csv_out = reports.export(books, BASIS, "sales")
    rows = list(csv.DictReader(io.StringIO(csv_out.data.decode())))
    assert len(rows) == 13 and rows[0]["invoice"] == "S-2026-1031"
    assert csv_out.filename == "sales-2026-10.csv" and csv_out.mime == "text/csv"
    assert "13 rows" in csv_out.text and "89,905.00 USD" not in csv_out.text  # dupes included
    budget = reports.export(books, PERIOD, "budget")
    assert len(list(csv.DictReader(io.StringIO(budget.data.decode())))) == 4
    with pytest.raises(reports.ReportError, match="I export"):
        reports.export(books, BASIS, "secrets")
    with pytest.raises(reports.ReportError, match="no planned rows"):
        reports.export(books, PERIOD, "planned")

    text = reports.chart_text(books, BASIS, "revenue")
    assert text.text.startswith("**October 2026 revenue by customer · 89,905.00 USD**")
    assert "Northwind Traders" in text.text and "█" in text.text and "```" in text.text
    png = reports.chart_png(books, PERIOD, "budget")
    assert png.data[:8] == b"\x89PNG\r\n\x1a\n" and png.filename == "budget-2026-11.png"
    assert png.rows == 4 and png.mime == "image/png"
    target = reports.chart_text(books, BASIS, "target")
    assert "revenue target" in target.text and "cost cap" in target.text
    with pytest.raises(reports.ReportError, match="I chart"):
        reports.chart_text(books, BASIS, "moods")


class _Files:
    def __init__(self) -> None:
        self.posted: list[tuple[str, str, str, int]] = []

    def post_file(self, room_id, text, filename, data, mime, thread_id) -> bool:
        self.posted.append((room_id, filename, mime, len(data)))
        return True


def test_commands_render_through_sindarin_switch(harness: Harness):
    h = harness
    fx = h.command("r1", "report 2026-10", sender=JOHN, room=PLANNING)
    assert [e.text for e in fx if isinstance(e, core.Post)] == [
        "Reports are kept by sindarin-switch: type `report 2026-10` to it."
    ]
    # without a ledger handle the switch executor says so
    fx = h.command("r2", "report 2026-10", sender=JOHN, room=FINANCE, executor=SWITCH_EXECUTOR)
    assert [e.text for e in fx if isinstance(e, core.Post)] == ["No ledger here to report from."]
    h.switch.ledger = Ledger(h.books.path, readonly=True)
    fx = h.command("r3", "report 2026-10", sender=JOHN, room=FINANCE, executor=SWITCH_EXECUTOR)
    posts = [e for e in fx if isinstance(e, core.Post)]
    assert posts and "**October 2026** · revenue **89,905.00 USD**" in posts[0].text
    assert "report_posted" in h.audit_events(SWITCH_EXECUTOR)
    # no file poster: a chart is a code block, an export is inline
    fx = h.command(
        "r4", "chart 2026-10 revenue", sender=JOHN, room=FINANCE, executor=SWITCH_EXECUTOR
    )
    assert "█" in [e for e in fx if isinstance(e, core.Post)][0].text
    fx = h.command(
        "r5", "export 2026-10 payables", sender=JOHN, room=FINANCE, executor=SWITCH_EXECUTOR
    )
    text = [e for e in fx if isinstance(e, core.Post)][0].text
    assert text.startswith("**payables 2026-10** · 3 rows") and "```csv" in text
    # with a file poster the chart and the export go out as attachments, with their caption
    files = _Files()
    h.switch.files = files
    fx = h.command("r6", "chart 2026-10 costs", sender=JOHN, room=FINANCE, executor=SWITCH_EXECUTOR)
    assert [e for e in fx if isinstance(e, core.Post)] == []
    assert files.posted[-1][1:3] == ("costs-2026-10.png", "image/png")
    h.command("r7", "export 2026-10 sales", sender=JOHN, room=FINANCE, executor=SWITCH_EXECUTOR)
    assert files.posted[-1][1:3] == ("sales-2026-10.csv", "text/csv")
    fx = h.command(
        "r8", "chart 2027-01 revenue", sender=JOHN, room=FINANCE, executor=SWITCH_EXECUTOR
    )
    assert [e.text for e in fx if isinstance(e, core.Post)] == [
        "**chart 2027-01** — the ledger has no period 2027-01."
    ]
    h.switch.ledger.close()
