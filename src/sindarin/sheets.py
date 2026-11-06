"""The team's own workbook as the ledger's source (design v5, real data): a Google Sheet read
through its CSV export, an Excel file read with openpyxl, and the write-back of what the
executors recorded, through a Google service account when one is configured."""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

TABS = ("accounts", "sales", "payables", "targets", "price_book")  # the last two are optional
WRITE_TABS = ("budget", "planned", "payables", "deals", "sales")
_SHEET_ID = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]+)")


class SheetError(Exception):
    """A refusal a person can read: a private sheet, a missing tab, a bad link."""


def sheet_id(url: str) -> str:
    m = _SHEET_ID.search(url or "")
    if not m:
        raise SheetError("that is not a Google Sheets link (expected /spreadsheets/d/<id>)")
    return m.group(1)


def tab_csv_url(url: str, tab: str) -> str:
    """The CSV export of one tab by name, for a sheet shared by link (no credential)."""
    return (
        f"https://docs.google.com/spreadsheets/d/{sheet_id(url)}/gviz/tq"
        f"?tqx=out:csv&sheet={quote(tab)}"
    )


def read_sheet(
    url: str, tabs: tuple[str, ...] = TABS, *, client: httpx.Client | None = None
) -> dict[str, list[dict[str, Any]]]:
    """Every tab of the workbook as rows keyed by its header. A tab that does not exist is
    skipped; a sheet that is not shared by link raises."""
    own = client is None
    client = client or httpx.Client(follow_redirects=True, timeout=30)
    out: dict[str, list[dict[str, Any]]] = {}
    try:
        for tab in tabs:
            r = client.get(tab_csv_url(url, tab))
            text = r.text
            if r.status_code != 200 or text.lstrip().startswith("<"):
                if "accounts.googleapis" in text or r.status_code in (401, 403):
                    raise SheetError(
                        "the sheet is not shared by link; share it as 'anyone with the link' "
                        "or configure a service account"
                    )
                if tab == tabs[0]:
                    raise SheetError(f"could not read the sheet ({r.status_code})")
                continue
            # Google pads the header with empty columns; keep only the named ones
            rows = [
                {k.strip(): v for k, v in x.items() if isinstance(k, str) and k.strip()}
                for x in csv.DictReader(io.StringIO(text))
            ]
            rows = [r for r in rows if any(str(v).strip() for v in r.values())]
            if rows:
                out[tab] = rows
    finally:
        if own:
            client.close()
    if not out:
        raise SheetError(f"no tab named {', '.join(tabs)} in that sheet")
    return out


def read_xlsx(path: str | Path, tabs: tuple[str, ...] = TABS) -> dict[str, list[dict[str, Any]]]:
    """The same tabs from an Excel workbook (the header row names the columns)."""
    from openpyxl import load_workbook

    wb = load_workbook(Path(path), read_only=True, data_only=True)
    out: dict[str, list[dict[str, Any]]] = {}
    for tab in tabs:
        if tab not in wb.sheetnames:
            continue
        ws = wb[tab]
        rows = ws.iter_rows(values_only=True)
        header = [str(h).strip() for h in next(rows, ()) if h is not None]
        if not header:
            continue
        out[tab] = [
            dict(zip(header, r, strict=False))
            for r in rows
            if r is not None and any(v is not None and str(v).strip() for v in r)
        ]
    wb.close()
    if not out:
        raise SheetError(f"no sheet named {', '.join(tabs)} in {Path(path).name}")
    return out


def write_xlsx(path: str | Path, tabs: dict[str, list[dict[str, Any]]]) -> Path:
    """An Excel workbook with one sheet per tab (the write-back form for Excel teams)."""
    from openpyxl import Workbook

    wb = Workbook()
    first = True
    for name, rows in tabs.items():
        ws = wb.active if first else wb.create_sheet()
        first = False
        ws.title = name
        if rows:
            header = list(rows[0].keys())
            ws.append(header)
            for r in rows:
                ws.append([r.get(k) for k in header])
    out = Path(path)
    wb.save(out)
    return out


class SheetWriter:
    """Writes rows back into the team's sheet through a Google service account (gspread). The
    key file is named in `.env` and handed only to sindarin-ledger."""

    def __init__(self, url: str, service_account_file: str) -> None:
        import gspread

        self.url = url
        self._gc = gspread.service_account(filename=service_account_file)
        self._book = self._gc.open_by_key(sheet_id(url))

    def write_tab(self, tab: str, rows: list[dict[str, Any]]) -> int:
        """Replace the tab's contents with `rows` (header first); creates the tab if needed."""
        try:
            ws = self._book.worksheet(tab)
        except Exception:
            ws = self._book.add_worksheet(title=tab, rows=max(10, len(rows) + 1), cols=12)
        ws.clear()
        if not rows:
            return 0
        header = list(rows[0].keys())
        values = [header] + [[r.get(k, "") for k in header] for r in rows]
        ws.update(values, "A1")
        return len(rows)

    def read_tab(self, tab: str) -> list[dict[str, Any]]:
        ws = self._book.worksheet(tab)
        return [dict(r) for r in ws.get_all_records()]
