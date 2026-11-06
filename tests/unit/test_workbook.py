"""The reading rail: any file a person links is fetched, described and checked against the
books' layout; `read` and `load` from chat go through the executors."""

from __future__ import annotations

import httpx
import pytest
from validation.harness import BASIS, DATA, FINANCE, JOHN, PLANNER_AGENT, PLANNING, VINCENT, Harness

from sindarin import workbook
from sindarin.executor import core
from sindarin.ledger import Ledger
from sindarin.names import SWITCH_EXECUTOR
from sindarin.sheets import write_xlsx

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _books_xlsx(tmp_path, extra_sales=()):
    books = Ledger(tmp_path / "src.db")
    books.import_dir(DATA / "2026-10")
    path = write_xlsx(
        tmp_path / "acme.xlsx",
        {
            "accounts": [{"code": k, **v} for k, v in books.accounts().items()],
            "sales": [*books.sales(BASIS), *extra_sales],
            "payables": books.payables(period=BASIS),
            "targets": [books.targets(BASIS)],
            "price_book": [{"product": k, "list_cents": v} for k, v in books.price_book().items()],
        },
    )
    books.close()
    return path.read_bytes()


def _book(name, data, ct=XLSX, url="http://files.local/acme.xlsx", source="link"):
    return workbook.Workbook(name, source, url, workbook.parse_bytes(name, data, ct), len(data))


def test_the_books_workbook_is_described_and_loadable(tmp_path):
    ins = workbook.inspect(_book("acme.xlsx", _books_xlsx(tmp_path)))
    assert ins.loadable and ins.periods == ["2026-10"] and ins.missing == []
    text = workbook.render(ins)
    assert text.startswith("**acme.xlsx** · link · 5 tabs")
    assert "**sales** · 13 rows · invoice, date, customer, amount_cents" in text
    assert "· 93,885.00 USD (89,905.00 USD with each invoice counted once)" in text
    assert "· 2026-10 (13 rows)" in text
    assert "⚠ S-2026-1041 appears 2 times (rows 12, 13); the books keep both" in text
    assert "**payables** · 3 rows" in text and "· 14,250.00 USD open" in text
    assert "· 2026-10: revenue 85,000.00 USD, margin 75.0%, cost cap 20,000.00 USD" in text
    assert "· Enterprise 36,000.00 USD, Starter 2,400.00 USD, Team 12,000.00 USD" in text
    assert "**As the books:** loadable · 2026-10 · `load <link>` to sindarin-ledger" in text
    assert ins.problems == []
    assert ins.warnings == ["sales: S-2026-1041 appears 2 times (rows 12, 13); the books keep both"]


def test_problems_are_named_and_any_tab_is_summarized():
    tabs = {
        "Sales": [
            {"invoice": "A-1", "date": "2026-10-03", "customer": "X", "amount": "10.00"},
            {"invoice": "A-1", "date": "", "customer": "Y", "amount": "abc"},
        ],
        "notes": [
            {"who": "me", "n": 1, "when": "2026-10-01"},
            {"who": "you", "n": 2, "when": "2026-10-09"},
        ],
        "empty": [],
    }
    ins = workbook.inspect(workbook.Workbook("x.xlsx", "Drive file", "http://x", tabs, 10))
    assert not ins.loadable and ins.missing == ["accounts"]
    joined = " ".join(ins.problems + ins.warnings)
    assert "A-1 appears 2 times" in joined and "without a date" in joined
    assert "1 with no readable amount (row 3)" in joined
    text = workbook.render(ins)
    assert "**notes** · 2 rows · who, n, when" in text
    assert "· n: total 3" in text and "· when: 2026-10-01 → 2026-10-09" in text
    assert "**empty** · 0 rows\n· empty" in text
    assert "not loadable — no `accounts` tab; Sales:" in text and "I expect tabs accounts" in text


def test_fetch_google_sheets_drive_and_links(tmp_path):
    data = _books_xlsx(tmp_path)
    good, private, gone = "GOOD" + "A" * 24, "PRIV" + "B" * 24, "GONE" + "C" * 24

    def transport(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if f"/spreadsheets/d/{good}/export?format=xlsx" in url:
            return httpx.Response(
                200,
                content=data,
                headers={
                    "content-type": XLSX,
                    "content-disposition": 'attachment; filename="Acme books.xlsx"',
                },
            )
        if private in url:
            return httpx.Response(
                200, text="<html>sign in</html>", headers={"content-type": "text/html"}
            )
        if gone in url:
            return httpx.Response(404, text="<html>not found</html>")
        if "drive.google.com/uc?export=download&id=" in url:
            return httpx.Response(200, content=data, headers={"content-type": XLSX})
        if url.endswith("/page"):
            return httpx.Response(
                200, text="<!doctype html><html>", headers={"content-type": "text/html"}
            )
        if url.endswith("/rows.csv"):
            return httpx.Response(
                200, text="invoice,amount\nA,1\n", headers={"content-type": "text/csv"}
            )
        if url.endswith("/big.bin"):
            return httpx.Response(
                200, content=b"\x00" * 64, headers={"content-type": "application/octet-stream"}
            )
        return httpx.Response(500)

    client = httpx.Client(transport=httpx.MockTransport(transport))
    book = workbook.fetch(
        f"https://docs.google.com/spreadsheets/d/{good}/edit#gid=0", client=client
    )
    assert book.name == "Acme books.xlsx" and book.source == "Google Sheet"
    assert set(book.tabs) == {"accounts", "sales", "payables", "targets", "price_book"}
    with pytest.raises(workbook.FileError, match="not shared by link: in Google Sheets"):
        workbook.fetch(f"https://docs.google.com/spreadsheets/d/{private}/edit", client=client)
    with pytest.raises(workbook.FileError, match="404"):
        workbook.fetch(f"https://docs.google.com/spreadsheets/d/{gone}/edit", client=client)
    drive = workbook.fetch(
        f"https://drive.google.com/file/d/{good}/view?usp=sharing", client=client
    )
    assert drive.source == "Drive file" and "sales" in drive.tabs
    with pytest.raises(workbook.FileError, match="web page, not a file"):
        workbook.fetch("https://example.com/page", client=client)
    csv_book = workbook.fetch("https://example.com/rows.csv", client=client)
    assert (
        csv_book.tabs == {"rows": [{"invoice": "A", "amount": "1"}]} and csv_book.name == "rows.csv"
    )
    with pytest.raises(workbook.FileError, match="I read Google Sheets"):
        workbook.fetch("https://example.com/big.bin", client=client)
    with pytest.raises(workbook.FileError, match="not a link"):
        workbook.fetch("ftp://example.com/x.xlsx", client=client)
    with pytest.raises(workbook.FileError, match="could not reach"):
        workbook.fetch(
            "https://example.com/boom",
            client=httpx.Client(
                transport=httpx.MockTransport(
                    lambda r: (_ for _ in ()).throw(httpx.ConnectError("x"))
                )
            ),
        )
    assert workbook.link_in("see <https://a.b/c>, now") == "https://a.b/c"
    assert workbook.link_in("no link") == ""


def test_read_and_load_from_chat(harness: Harness, tmp_path, monkeypatch):
    h = harness
    late = {
        "invoice": "S-2026-1099",
        "date": "2026-10-30",
        "customer": "Fabrikam",
        "amount_cents": 123400,
    }
    good = _book("acme.xlsx", _books_xlsx(tmp_path, extra_sales=[late]))
    bad = workbook.Workbook(
        "notes.xlsx", "link", "http://files.local/notes.xlsx", {"notes": [{"a": 1}]}, 9
    )
    books = {"http://files.local/acme.xlsx": good, "http://files.local/notes.xlsx": bad}

    def fake_fetch(url, *, client=None):
        if url == "http://files.local/private":
            raise workbook.FileError("the Google Sheet is not shared by link: share it")
        return books[url]

    monkeypatch.setattr(workbook, "fetch", fake_fetch)

    def posts(fx):
        return [e.text for e in fx if isinstance(e, core.Post)]

    # read: sindarin-switch describes; the ledger points there
    fx = h.command(
        "m-read",
        "read http://files.local/acme.xlsx",
        sender=JOHN,
        room=FINANCE,
        executor=SWITCH_EXECUTOR,
    )
    assert posts(fx)[0].startswith("**acme.xlsx** · link · 5 tabs") and "14 rows" in posts(fx)[0]
    fx = h.command(
        "m-read-2",
        "read http://files.local/private",
        sender=JOHN,
        room=FINANCE,
        executor=SWITCH_EXECUTOR,
    )
    assert posts(fx) == ["**read** — the Google Sheet is not shared by link: share it."]
    fx = h.command("m-read-3", "read http://files.local/acme.xlsx", sender=JOHN, room=PLANNING)
    assert posts(fx) == ["Files are read by sindarin-switch: type `read <link>` to it."]
    # load: an agent and a person in no group are refused; the switch executor points to the ledger
    fx = h.command(
        "m-load-a", "load http://files.local/acme.xlsx", sender=PLANNER_AGENT, room=PLANNING
    )
    assert "is an agent" in posts(fx)[0]
    fx = h.command("m-load-v", "load http://files.local/acme.xlsx", sender=VINCENT, room=PLANNING)
    assert "Vincent is in no approver group" in posts(fx)[0]
    fx = h.command(
        "m-load-s",
        "load http://files.local/acme.xlsx",
        sender=JOHN,
        room=FINANCE,
        executor=SWITCH_EXECUTOR,
    )
    assert posts(fx)[0].startswith("The books are loaded by sindarin-ledger")
    before = h.books.summary(BASIS)["revenue_cents"]
    fx = h.command("m-load-bad", "load http://files.local/notes.xlsx", sender=JOHN, room=PLANNING)
    assert posts(fx)[0].startswith(
        "**load** refused — notes.xlsx is not loadable as the books: no `accounts` tab; no `sales` tab."
    )
    assert h.books.summary(BASIS)["revenue_cents"] == before
    # a loadable file replaces the open rows: the late sale is now in the books
    fx = h.command("m-load", "load http://files.local/acme.xlsx", sender=JOHN, room=PLANNING)
    text = posts(fx)[0]
    assert text.startswith(
        "**acme.xlsx** loaded into the books by John · 8 accounts, 14 sales, 3 payables, "
        "1 targets, 3 price book · 2026-10."
    )
    assert h.books.summary(BASIS)["revenue_cents"] == before + 123400
    assert h.books.summary(BASIS)["sales_rows"] == 14
    assert any(isinstance(e, core.Audit) and e.event == "books_loaded" for e in fx)
    # loading again is idempotent: the rows are replaced, not doubled
    h.command("m-load-again", "load http://files.local/acme.xlsx", sender=JOHN, room=PLANNING)
    assert h.books.summary(BASIS)["sales_rows"] == 14


def test_locked_months_and_paid_payables_are_kept(harness: Harness, tmp_path, monkeypatch):
    h = harness
    h.run_plan_gate()
    h.run_pay_gate()
    h.propose_lock()
    h.approve_lock()
    assert h.books.period_status(BASIS) == "locked"
    good = _book("acme.xlsx", _books_xlsx(tmp_path))
    monkeypatch.setattr(workbook, "fetch", lambda url, *, client=None: good)
    fx = h.command("m-load", "load http://files.local/acme.xlsx", sender=JOHN, room=PLANNING)
    text = [e.text for e in fx if isinstance(e, core.Post)][0]
    assert (
        "kept as they were: 13 rows (2026-10 is locked), INV-1042, INV-1043, INV-1044 (paid)"
        in text
    )
    assert h.books.period_status(BASIS) == "locked" and len(h.books.sales(BASIS)) == 13
    assert all(r["status"] == "paid" for r in h.books.payables(period=BASIS))


def test_a_tab_is_listed_as_a_table_and_a_csv(tmp_path):
    book = _book("acme.xlsx", _books_xlsx(tmp_path))
    text, data, filename = workbook.rows_table(book, "Sales", limit=5)
    lines = text.splitlines()
    assert lines[0] == "**sales** of acme.xlsx · 13 rows"
    assert lines[1] == "| invoice | date | customer | amount (USD) |"
    assert lines[3] == "| S-2026-1031 | 2026-10-01 | Northwind Traders | 12,450.00 |"
    assert lines[-1] == "… 8 more rows; the whole tab is attached."
    assert filename == "acme-sales.csv" and data.startswith(
        b"invoice,date,customer,amount_cents\r\n"
    )
    assert data.count(b"\n") == 14
    with pytest.raises(workbook.FileError, match="has no tab 'deals'; its tabs: accounts, sales"):
        workbook.rows_table(book, "deals")
    empty = workbook.Workbook("e.xlsx", "link", "http://x", {"notes": []}, 1)
    assert workbook.rows_table(empty, "notes")[0] == "**notes** of e.xlsx · 0 rows · empty"
    assert "`read <link> <tab>` lists a tab's entries." in workbook.render(workbook.inspect(book))


def test_read_a_tab_from_chat(harness: Harness, tmp_path, monkeypatch):
    h = harness
    good = _book("acme.xlsx", _books_xlsx(tmp_path))
    monkeypatch.setattr(workbook, "fetch", lambda url, *, client=None: good)
    fx = h.command(
        "m-read-tab",
        "read http://files.local/acme.xlsx payables",
        sender=JOHN,
        room=FINANCE,
        executor=SWITCH_EXECUTOR,
    )
    text = [e.text for e in fx if isinstance(e, core.Post)][0]
    assert text.startswith("**payables** of acme.xlsx · 3 rows") and "| INV-1042 |" in text
    fx = h.command(
        "m-read-none",
        "read http://files.local/acme.xlsx deals",
        sender=JOHN,
        room=FINANCE,
        executor=SWITCH_EXECUTOR,
    )
    assert [e.text for e in fx if isinstance(e, core.Post)] == [
        "**read** — acme.xlsx has no tab 'deals'; its tabs: accounts, sales, payables, targets, price_book."
    ]
