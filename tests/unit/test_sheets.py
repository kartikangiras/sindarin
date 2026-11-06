"""The team's workbook as the ledger's source (design v5): a Google Sheet by its CSV export, an
Excel file by openpyxl, money as cents or as decimals, and the Excel write-back."""

from __future__ import annotations

import httpx
import pytest
from typer.testing import CliRunner
from validation.harness import DATA

from sindarin import sheets
from sindarin.cli import app
from sindarin.executor.config import ExecutorConfig, save_config
from sindarin.ledger import Ledger
from sindarin.names import LEDGER_EXECUTOR

URL = "https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789/edit#gid=0"
SHEET = {
    "accounts": (
        "code,name,type\n1000,Cash,asset\n1100,Accounts receivable,asset\n"
        "4000,Revenue,revenue\n6000,Operating expenses,expense\n"
    ),
    "sales": 'invoice,date,customer,amount\nS-1,2026-10-02,Northwind,"12,450.00"\nS-2,2026-10-05,Contoso,8600\n',
    "payables": "invoice,vendor,amount_cents,due\nINV-1,AWS,480000,2026-10-31\n",
    "targets": "period,revenue,margin,cost_cap\n2026-10,85000,75%,20000\n",
}


def _transport(request: httpx.Request) -> httpx.Response:
    tab = request.url.params.get("sheet")
    if tab in SHEET:
        return httpx.Response(200, text=SHEET[tab])
    return httpx.Response(200, text="<html>no such tab</html>")


def test_google_sheet_read_by_tab(tmp_path):
    assert sheets.sheet_id(URL) == "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
    assert sheets.tab_csv_url(URL, "sales").endswith("gviz/tq?tqx=out:csv&sheet=sales")
    with pytest.raises(sheets.SheetError, match="not a Google Sheets link"):
        sheets.sheet_id("https://example.com/x")
    client = httpx.Client(transport=httpx.MockTransport(_transport))
    tabs = sheets.read_sheet(URL, client=client)
    assert set(tabs) == {"accounts", "sales", "payables", "targets"}  # no price_book tab: skipped
    ledger = Ledger(tmp_path / "books.db")
    counts = ledger.import_tabs(tabs)
    assert counts == {"accounts": 4, "sales": 2, "payables": 1, "targets": 1, "price_book": 0}
    s = ledger.summary("2026-10")
    assert s["revenue_cents"] == 2105000 and s["costs_cents"] == 480000
    assert s["targets"]["margin_bps"] == 7500 and s["targets"]["cost_cap_cents"] == 2000000
    ledger.close()


def test_excel_round_trip(tmp_path):
    books = Ledger(tmp_path / "a.db")
    books.import_dir(DATA / "2026-10")
    path = sheets.write_xlsx(
        tmp_path / "acme.xlsx",
        {
            "accounts": [{"code": k, **v} for k, v in books.accounts().items()],
            "sales": books.sales("2026-10"),
            "payables": books.payables(period="2026-10"),
            "targets": [books.targets("2026-10")],
        },
    )
    tabs = sheets.read_xlsx(path)
    again = Ledger(tmp_path / "b.db")
    assert again.import_tabs(tabs) == {
        "accounts": 8,
        "sales": 13,
        "payables": 3,
        "targets": 1,
        "price_book": 0,
    }
    assert again.summary("2026-10")["revenue_cents"] == books.summary("2026-10")["revenue_cents"]
    with pytest.raises(sheets.SheetError, match="no sheet named"):
        sheets.read_xlsx(sheets.write_xlsx(tmp_path / "empty.xlsx", {"notes": [{"a": 1}]}))
    books.close()
    again.close()


def test_cli_imports_an_excel_workbook_and_exports_one(tmp_path):
    exec_dir = tmp_path / "executor"
    exec_dir.mkdir()
    save_config(
        exec_dir / "ledger.yaml",
        ExecutorConfig(
            executor=LEDGER_EXECUTOR, self_id="@switch-agent-l:test", data_dir=str(DATA)
        ),
    )
    runner = CliRunner()
    r = runner.invoke(app, ["ledger", "import", "--executor-dir", str(exec_dir)])
    assert r.exit_code == 0, r.output
    r = runner.invoke(
        app, ["ledger", "export-xlsx", str(tmp_path / "acme.xlsx"), "--executor-dir", str(exec_dir)]
    )
    assert r.exit_code == 0 and "sales 13" in r.output, r.output
    r = runner.invoke(app, ["ledger", "reset", "--executor-dir", str(exec_dir)])
    assert r.exit_code == 0
    r = runner.invoke(
        app,
        [
            "ledger",
            "import",
            "--xlsx",
            str(tmp_path / "acme.xlsx"),
            "--executor-dir",
            str(exec_dir),
        ],
    )
    assert r.exit_code == 0 and "13 sales rows, 3 payables" in r.output, r.output
