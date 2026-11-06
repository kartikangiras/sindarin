"""Any file a person links in chat (design v5, the reading rail): a Google Sheet, a file on
Drive, a direct link to an Excel workbook or a CSV. Fetched, read tab by tab, described, and
checked against the layout the books expect, every problem named so the person can fix the
file. Deterministic; no model reads the file."""

from __future__ import annotations

import csv
import io
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from urllib.parse import unquote, urlparse

import httpx

from sindarin.ledger import _bps, _cents
from sindarin.schema import money

MAX_BYTES = 15 * 1024 * 1024
MAX_ROWS = 5000
MAX_TABS = 12
SHEET_ID = re.compile(r"docs\.google\.com/spreadsheets/d/([A-Za-z0-9_-]{20,})")
DRIVE_ID = re.compile(
    r"drive\.google\.com/(?:file/d/|open\?id=|uc\?(?:[^\s]*?&)?id=)([A-Za-z0-9_-]{20,})"
)
URL_RE = re.compile(r"https?://[^\s<>()]+")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")
PERIOD = re.compile(r"^\d{4}-\d{2}$")

# the layout the books take (sindarin.ledger.import_tabs): tab → (required columns, money columns)
BOOKS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "accounts": (("code", "name", "type"), ()),
    "sales": (("invoice", "date", "customer"), ("amount",)),
    "payables": (("invoice", "vendor", "due"), ("amount",)),
    "targets": (("period",), ("revenue", "cost_cap")),
    "price_book": (("product",), ("list",)),
}
LAYOUT = (
    "tabs accounts (code, name, type), sales (invoice, date, customer, amount), payables "
    "(invoice, vendor, due, amount), targets (period, revenue, margin, cost_cap) and price_book "
    "(product, list), money as cents (`amount_cents`) or decimals (`amount`)"
)


class FileError(Exception):
    """A refusal a person can read, with what to do about it."""


@dataclass(frozen=True)
class Workbook:
    name: str
    source: str  # "Google Sheet", "Drive file" or "link"
    url: str
    tabs: dict[str, list[dict[str, Any]]]
    size: int


def link_in(text: str) -> str:
    """The first http(s) link in a message, or ''."""
    m = URL_RE.search(text or "")
    return m.group(0).rstrip(".,;:!?") if m else ""


def tab_key(name: str) -> str:
    return re.sub(r"[\s-]+", "_", name.strip().lower())


# -- fetch ----------------------------------------------------------------------------------------


def _not_shared(source: str) -> str:
    where = "Google Sheets" if source == "Google Sheet" else "Drive"
    return (
        f"the {source.lower()} is not shared by link: in {where}, Share → General access → "
        "Anyone with the link (Viewer), then send the link again"
    )


def _filename(r: httpx.Response, fallback: str) -> str:
    cd = r.headers.get("content-disposition", "")
    m = re.search(r"filename\*=UTF-8''([^;]+)", cd) or re.search(r'filename="?([^";]+)"?', cd)
    return unquote(m.group(1)).strip() if m else fallback


def fetch(url: str, *, client: httpx.Client | None = None) -> Workbook:
    """Fetch and read the file behind a link: a Google Sheet (every tab, through its workbook
    export), a file on Drive, or a direct link to a .xlsx or .csv. Nothing is written anywhere."""
    url = (url or "").strip().strip("<>")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise FileError("that is not a link I can open; paste an http(s) link")
    if m := SHEET_ID.search(url):
        source = "Google Sheet"
        get = f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=xlsx"
        fallback = "sheet.xlsx"
    elif m := DRIVE_ID.search(url):
        source = "Drive file"
        get = f"https://drive.google.com/uc?export=download&id={m.group(1)}"
        fallback = "file"
    else:
        source = "link"
        get = url
        fallback = unquote(parsed.path.rsplit("/", 1)[-1]) or parsed.netloc
    own = client is None
    client = client or httpx.Client(follow_redirects=True, timeout=30)
    try:
        r = client.get(get)
        final = str(r.url)
        ct = r.headers.get("content-type", "")
        if r.status_code in (401, 403) or "accounts.google.com" in final or "ServiceLogin" in final:
            raise FileError(_not_shared(source))
        if r.status_code == 404:
            raise FileError(
                f"nothing at that link (404): the {source.lower()} was deleted, or the id in "
                "the link is wrong"
            )
        if r.status_code >= 400:
            raise FileError(f"the {source.lower()} could not be fetched ({r.status_code})")
        data = r.content
        if source == "Drive file" and "text/html" in ct and b"confirm=" in data:
            m2 = re.search(rb"confirm=([0-9A-Za-z_-]+)", data)  # Drive's large-file page
            if m2:
                r = client.get(f"{get}&confirm={m2.group(1).decode()}")
                data, ct = r.content, r.headers.get("content-type", "")
        if source != "link" and "text/html" in ct:
            raise FileError(_not_shared(source))
        if len(data) > MAX_BYTES:
            raise FileError(f"that file is {len(data) / 1e6:.0f} MB; I read files up to 15 MB")
        name = _filename(r, fallback)
        return Workbook(name, source, url, parse_bytes(name, data, ct), len(data))
    except httpx.HTTPError as exc:
        raise FileError(f"could not reach {parsed.netloc} ({exc.__class__.__name__})") from None
    finally:
        if own:
            client.close()


# -- parse ----------------------------------------------------------------------------------------


def _cell(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return v


def _blank(r: Any) -> bool:
    return r is None or not any(v is not None and str(v).strip() for v in r)


def parse_xlsx(name: str, data: bytes) -> dict[str, list[dict[str, Any]]]:
    from openpyxl import load_workbook

    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise FileError(
            f"{name} is not a workbook I can open ({exc.__class__.__name__}); save it as .xlsx"
        ) from None
    tabs: dict[str, list[dict[str, Any]]] = {}
    for ws in wb.worksheets[:MAX_TABS]:
        header: list[str] | None = None
        rows: list[dict[str, Any]] = []
        for r in ws.iter_rows(values_only=True):
            if _blank(r):
                continue
            if header is None:
                header = [str(h).strip() if h is not None else "" for h in r]
                continue
            rows.append({h: _cell(v) for h, v in zip(header, r, strict=False) if h})
            if len(rows) >= MAX_ROWS:
                break
        tabs[ws.title] = rows
    wb.close()
    return tabs


def parse_csv(name: str, text: str) -> dict[str, list[dict[str, Any]]]:
    rows = [
        {k.strip(): v for k, v in x.items() if isinstance(k, str) and k.strip()}
        for x in csv.DictReader(io.StringIO(text))
    ]
    rows = [r for r in rows if any(v is not None and str(v).strip() for v in r.values())]
    stem = re.sub(r"\.[A-Za-z0-9]+$", "", name) or "sheet"
    return {stem: rows[:MAX_ROWS]}


def parse_bytes(name: str, data: bytes, content_type: str = "") -> dict[str, list[dict[str, Any]]]:
    ct = (content_type or "").lower()
    if data.startswith(b"PK\x03\x04"):
        return parse_xlsx(name, data)
    if data.startswith(b"\xd0\xcf\x11\xe0"):
        raise FileError(f"{name} is an old .xls workbook; save it as .xlsx and share that")
    text = data.decode("utf-8-sig", errors="replace")
    if text.lstrip().startswith("<"):
        raise FileError(
            "that link returns a web page, not a file: share the file itself (a Google Sheet, "
            "a file on Drive, or a direct link to a .xlsx or .csv)"
        )
    first = text.strip().splitlines()[0] if text.strip() else ""
    looks_csv = "csv" in ct or ct.startswith("text/") or name.lower().endswith((".csv", ".txt"))
    if looks_csv or ("," in first and len(first) < 2000):
        return parse_csv(name, text)
    raise FileError(
        f"I read Google Sheets, Excel workbooks (.xlsx) and CSV files; {name} is "
        f"{content_type or 'something else'}"
    )


# -- inspect --------------------------------------------------------------------------------------


@dataclass
class TabReport:
    name: str
    rows: int
    columns: list[str]
    notes: list[str] = field(default_factory=list)  # what it holds
    problems: list[str] = field(default_factory=list)  # what to fix before a load
    warnings: list[str] = field(default_factory=list)  # worth a look; the books take it
    known: bool = False  # one of the books' tabs


@dataclass
class Inspection:
    book: Workbook
    tabs: list[TabReport]
    periods: list[str]
    problems: list[str]  # every blocking problem, tab-prefixed
    warnings: list[str]  # every warning, tab-prefixed
    missing: list[str]  # the books' tabs a load needs and the file lacks
    loadable: bool


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or v in (None, ""):
        return None
    if isinstance(v, int | float):
        return float(v)
    try:
        return float(str(v).replace(",", "").replace("$", "").strip())
    except ValueError:
        return None


def _rows_text(rows: list[int]) -> str:
    shown = ", ".join(str(i) for i in rows[:5]) + (", …" if len(rows) > 5 else "")
    return f"row{'s' if len(rows) != 1 else ''} {shown}"


def _money_of(row: dict[str, Any], name: str) -> int | None:
    try:
        return _cents(row, name)
    except (ValueError, TypeError):
        return None


def _known_tab(key: str, rows: list[dict[str, Any]], t: TabReport, periods: set[str]) -> None:
    required, moneys = BOOKS[key]
    cols = {c.lower() for c in t.columns}
    for c in required:
        if c not in cols:
            t.problems.append(f"no `{c}` column")
    for mcol in moneys:
        if not cols & {mcol, f"{mcol}_cents", f"{mcol}_usd"}:
            t.problems.append(f"no `{mcol}` or `{mcol}_cents` column")
    if t.problems or not rows:
        return
    if key in ("sales", "payables"):
        total, repeated, bad_money = 0, 0, []
        first_seen: set[str] = set()
        for i, r in enumerate(rows, 2):
            cents = _money_of(r, "amount")
            if cents is None:
                bad_money.append(i)
                continue
            total += cents
            inv = str(r.get("invoice", "")).strip()
            if inv in first_seen:
                repeated += cents  # the books count an invoice once
            first_seen.add(inv)
        once = f" ({money(total - repeated)} with each invoice counted once)" if repeated else ""
        t.notes.append(f"{money(total)}{' open' if key == 'payables' else ''}{once}")
        if bad_money:
            t.problems.append(f"{len(bad_money)} with no readable amount ({_rows_text(bad_money)})")
        dcol = "date" if key == "sales" else "due"
        per: Counter[str] = Counter()
        bad_date = []
        for i, r in enumerate(rows, 2):
            v = str(r.get(dcol) or "").strip()
            if ISO_DATE.match(v):
                per[v[:7]] += 1
            else:
                bad_date.append(i)
        periods |= set(per)
        if per:
            t.notes.append(
                ", ".join(f"{p} ({n} row{'s' if n != 1 else ''})" for p, n in sorted(per.items()))
            )
        if bad_date:
            t.problems.append(f"{len(bad_date)} without a {dcol} ({_rows_text(bad_date)})")
        seen = Counter(str(r.get("invoice", "")).strip() for r in rows)
        for inv, n in seen.items():
            if n > 1 and inv:
                where = [
                    i for i, r in enumerate(rows, 2) if str(r.get("invoice", "")).strip() == inv
                ]
                if key == "sales":
                    t.warnings.append(
                        f"{inv} appears {n} times ({_rows_text(where)}); the books keep both"
                    )
                else:
                    t.problems.append(
                        f"{inv} appears {n} times ({_rows_text(where)}); one invoice, one row"
                    )
    elif key == "targets":
        for r in rows:
            p = str(r.get("period", "")).strip()[:7]
            if not PERIOD.match(p):
                t.problems.append(f"period {p!r} is not YYYY-MM")
                continue
            periods.add(p)
            rev, cap = _money_of(r, "revenue"), _money_of(r, "cost_cap")
            try:
                margin = f"{_bps(r) / 100:.1f}%"
            except (ValueError, TypeError):
                margin = "?"
            t.notes.append(
                f"{p}: revenue {money(rev or 0)}, margin {margin}, cost cap {money(cap or 0)}"
            )
    elif key == "price_book":
        parts = []
        for r in rows:
            lst = _money_of(r, "list")
            parts.append(f"{r.get('product')} {money(lst) if lst is not None else '?'}")
        t.notes.append(", ".join(parts[:8]))
    elif key == "accounts":
        kinds = Counter(str(r.get("type", "")).strip() for r in rows)
        t.notes.append(", ".join(f"{n} {k}" for k, n in sorted(kinds.items()) if k))


def _any_tab(rows: list[dict[str, Any]], t: TabReport) -> None:
    for c in t.columns[:12]:
        vals = [r.get(c) for r in rows if r.get(c) not in (None, "")]
        if len(vals) < 2:
            continue
        nums = [_num(v) for v in vals]
        if all(n is not None for n in nums):
            total = sum(n for n in nums if n is not None)
            whole = all(float(n).is_integer() for n in nums if n is not None)
            t.notes.append(f"{c}: total {int(total):,}" if whole else f"{c}: total {total:,.2f}")
        elif all(ISO_DATE.match(str(v)) for v in vals):
            ds = sorted(str(v)[:10] for v in vals)
            t.notes.append(f"{c}: {ds[0]} → {ds[-1]}")
        else:
            distinct = Counter(str(v).strip() for v in vals)
            if len(distinct) <= 6 and len(vals) >= 4:
                t.notes.append(f"{c}: " + ", ".join(f"{k} {n}" for k, n in distinct.most_common(3)))


def inspect(book: Workbook) -> Inspection:
    """What the file holds, tab by tab, and whether the books can take it."""
    reports: list[TabReport] = []
    periods: set[str] = set()
    for name, rows in list(book.tabs.items())[:MAX_TABS]:
        columns = list(rows[0].keys()) if rows else []
        t = TabReport(name, len(rows), columns, known=tab_key(name) in BOOKS)
        if not rows:
            t.notes.append("empty")
        if t.known:
            _known_tab(tab_key(name), rows, t, periods)
        else:
            _any_tab(rows, t)
        reports.append(t)
    known = {tab_key(t.name) for t in reports if t.known}
    missing = [k for k in ("accounts", "sales") if k not in known]
    problems = [f"{t.name}: {p}" for t in reports for p in t.problems]
    warnings = [f"{t.name}: {w}" for t in reports for w in t.warnings]
    loadable = not missing and not problems
    return Inspection(book, reports, sorted(periods), problems, warnings, missing, loadable)


def find_tab(book: Workbook, name: str) -> str:
    """The tab called `name` (case and spacing aside), or a FileError that lists the tabs."""
    for tab in book.tabs:
        if tab_key(tab) == tab_key(name):
            return tab
    have = ", ".join(book.tabs) or "none"
    raise FileError(f"{book.name} has no tab {name!r}; its tabs: {have}")


def _show(column: str, value: Any) -> str:
    if value in (None, ""):
        return ""
    if column.lower().endswith("_cents") and _num(value) is not None:
        return money(int(_num(value) or 0)).rsplit(" ", 1)[0]  # 1245000 → 12,450.00
    return str(value).replace("|", "\\|").replace("\n", " ")


def rows_table(book: Workbook, name: str, limit: int = 30) -> tuple[str, bytes, str]:
    """One tab's entries: a markdown table of the first `limit` rows (eight columns at most),
    and the whole tab as CSV bytes to attach, with its file name."""
    tab = find_tab(book, name)
    rows = book.tabs[tab]
    buf = io.StringIO()
    if rows:
        columns = list(rows[0].keys())
        writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow({k: ("" if v is None else v) for k, v in r.items()})
    else:
        columns = []
    data = buf.getvalue().encode("utf-8")
    stem = re.sub(r"\.[A-Za-z0-9]+$", "", book.name) or "file"
    filename = f"{stem}-{tab_key(tab)}.csv"
    n = len(rows)
    head = f"**{tab}** of {book.name} · {n} row{'s' if n != 1 else ''}"
    if not rows:
        return f"{head} · empty", data, filename
    shown = columns[:8]
    more_cols = f" (+{len(columns) - 8} columns)" if len(columns) > 8 else ""
    labels = [f"{c[:-6]} (USD)" if c.lower().endswith("_cents") else c for c in shown]
    lines = [f"{head}{more_cols}", "| " + " | ".join(labels) + " |", "|" + "---|" * len(shown)]
    for r in rows[:limit]:
        lines.append("| " + " | ".join(_show(c, r.get(c)) for c in shown) + " |")
    if n > limit:
        lines.append(
            f"… {n - limit} more row{'s' if n - limit != 1 else ''}; the whole tab is attached."
        )
    return "\n".join(lines), data, filename


def books_tabs(ins: Inspection) -> dict[str, list[dict[str, Any]]]:
    """The books' tabs of the file, keyed as the ledger wants them."""
    return {tab_key(n): rows for n, rows in ins.book.tabs.items() if tab_key(n) in BOOKS}


def render(ins: Inspection, ledger: str = "sindarin-ledger") -> str:
    b = ins.book
    size = f"{b.size / 1024:.0f} KB" if b.size < 1_000_000 else f"{b.size / 1e6:.1f} MB"
    n = len(ins.tabs)
    lines = [f"**{b.name}** · {b.source} · {n} tab{'s' if n != 1 else ''} · {size}"]
    for t in ins.tabs:
        cols = ", ".join(t.columns[:8]) + (
            f" (+{len(t.columns) - 8})" if len(t.columns) > 8 else ""
        )
        head = f"**{t.name}** · {t.rows} row{'s' if t.rows != 1 else ''}"
        lines.append(f"{head} · {cols}" if cols else head)
        lines += [f"· {note}" for note in t.notes[:6]]
        lines += [f"⚠ {w}" for w in t.warnings[:4]]
        lines += [f"⚠ {p}" for p in t.problems[:6]]
    lines.append("`read <link> <tab>` lists a tab's entries.")
    if ins.loadable:
        per = ", ".join(ins.periods) or "no month"
        lines.append(
            f"**As the books:** loadable · {per} · `load <link>` to {ledger} takes it in (open "
            "rows replaced; paid payables and locked months kept)."
        )
    else:
        why = "; ".join([f"no `{m}` tab" for m in ins.missing] + ins.problems[:4])
        lines.append(f"**As the books:** not loadable — {why}. I expect {LAYOUT}.")
    return "\n".join(lines)
