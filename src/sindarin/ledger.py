"""The ledger (design v5 §5): a SQLite file with accounts, sales, payables, journal entries,
targets, budgets, the planned amounts and periods.

`sindarin-ledger` is the only process that writes it; the agents open it read-only. In a real
deployment the same adapter sits in front of the accounting system's API. Every number the
demo shows comes from here, never from a model.
"""

from __future__ import annotations

import csv
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sindarin.schema import canonical_json, sha256_hex

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts(code TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sales(
  n INTEGER PRIMARY KEY, invoice TEXT NOT NULL, date TEXT NOT NULL, customer TEXT NOT NULL,
  amount_cents INTEGER NOT NULL, period TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS payables(
  invoice TEXT PRIMARY KEY, vendor TEXT NOT NULL, amount_cents INTEGER NOT NULL, due TEXT NOT NULL,
  period TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', run_id TEXT, paid_at TEXT);
CREATE TABLE IF NOT EXISTS journal(
  id INTEGER PRIMARY KEY, period TEXT NOT NULL, date TEXT NOT NULL, memo TEXT NOT NULL,
  source TEXT NOT NULL, posted_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lines(
  journal_id INTEGER NOT NULL REFERENCES journal(id), account TEXT NOT NULL,
  debit_cents INTEGER NOT NULL DEFAULT 0, credit_cents INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS periods(
  period TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'open', planned_at TEXT,
  budget_sha TEXT, locked_at TEXT, trial_balance_sha TEXT);
CREATE TABLE IF NOT EXISTS targets(
  period TEXT PRIMARY KEY, revenue_cents INTEGER NOT NULL, margin_bps INTEGER NOT NULL,
  cost_cap_cents INTEGER NOT NULL, source TEXT NOT NULL, recorded_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS budgets(
  period TEXT NOT NULL, department TEXT NOT NULL, account TEXT NOT NULL,
  amount_cents INTEGER NOT NULL, source TEXT NOT NULL, recorded_at TEXT NOT NULL,
  PRIMARY KEY (period, department));
CREATE TABLE IF NOT EXISTS price_book(product TEXT PRIMARY KEY, list_cents INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS deals(
  id TEXT PRIMARY KEY, account TEXT NOT NULL, product TEXT NOT NULL, list_cents INTEGER NOT NULL,
  price_cents INTEGER NOT NULL, discount_bps INTEGER NOT NULL, terms TEXT NOT NULL,
  period TEXT NOT NULL, stage TEXT NOT NULL, source TEXT NOT NULL, won_at TEXT);
CREATE TABLE IF NOT EXISTS planned(
  period TEXT NOT NULL, department TEXT NOT NULL, account TEXT NOT NULL,
  amount_cents INTEGER NOT NULL, source TEXT NOT NULL, posted_at TEXT NOT NULL,
  PRIMARY KEY (period, department));
"""
CASH, RECEIVABLE, REVENUE, EXPENSE = "1000", "1100", "4000", "6000"
_TABLES = (
    "deals",
    "price_book",
    "planned",
    "budgets",
    "targets",
    "lines",
    "journal",
    "payables",
    "sales",
    "periods",
    "accounts",
)


class LedgerError(Exception):
    """A ledger refusal. The text is shown in a room; it holds no secret."""


def _now() -> str:
    return datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def period_of(date_text: str) -> str:
    return date_text[:7]


def _date(value: Any) -> str:
    """A date cell as `YYYY-MM-DD`: an ISO string, or a spreadsheet date object."""
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    return str(value).strip()[:10]


def _cents(row: Mapping[str, Any], name: str) -> int:
    """`<name>_cents` when the sheet keeps cents, else `<name>` as a decimal amount."""
    if row.get(f"{name}_cents") not in (None, ""):
        return int(float(str(row[f"{name}_cents"]).replace(",", "")))
    value = row.get(name, row.get(f"{name}_usd", 0))
    return round(float(str(value or 0).replace(",", "").replace("$", "")) * 100)


def _bps(row: Mapping[str, Any]) -> int:
    """`margin_bps`, or `margin` as a percentage (`75` or `75%`)."""
    if row.get("margin_bps") not in (None, ""):
        return int(float(str(row["margin_bps"])))
    value = str(row.get("margin", "0")).replace("%", "").strip() or "0"
    return round(float(value) * 100)


class Ledger:
    def __init__(self, path: str | Path, *, readonly: bool = False) -> None:
        self.path = Path(path)
        self.readonly = readonly
        # the executors call adapters from a worker thread; sqlite's same-thread guard is off and
        # every write here is a single short transaction
        if readonly:
            self._db = sqlite3.connect(
                f"file:{self.path}?mode=ro", uri=True, check_same_thread=False
            )
        else:
            self._db = sqlite3.connect(self.path, check_same_thread=False)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(SCHEMA)
        self._db.row_factory = sqlite3.Row

    def close(self) -> None:
        self._db.close()

    # -- import --------------------------------------------------------------------------------

    def import_dir(self, folder: str | Path) -> dict[str, int]:
        """Load a period's data: accounts.csv, sales.csv, payables.csv and, when present, the
        targets the period was given (targets.csv). Idempotent on a fresh file; returns counts."""
        folder = Path(folder)
        tabs: dict[str, list[dict[str, Any]]] = {}
        for name in ("accounts", "sales", "payables", "targets", "price_book"):
            path = folder / f"{name}.csv"
            if path.exists():
                with open(path, newline="", encoding="utf-8") as f:
                    tabs[name] = [dict(r) for r in csv.DictReader(f)]
        return self.import_tabs(tabs)

    def import_tabs(self, tabs: Mapping[str, list[Mapping[str, Any]]]) -> dict[str, int]:
        """Load the tabs of a workbook (a folder of CSVs, a Google Sheet, an Excel file): accounts,
        sales, payables and targets, each a list of rows keyed by the header. Money may come as
        cents (`amount_cents`) or as a decimal amount (`amount`)."""
        self._check_write()
        counts = {"accounts": 0, "sales": 0, "payables": 0, "targets": 0, "price_book": 0}
        for row in tabs.get("price_book", []):
            self._db.execute(
                "INSERT OR REPLACE INTO price_book(product, list_cents) VALUES (?,?)",
                (str(row["product"]).strip(), _cents(row, "list")),
            )
            counts["price_book"] += 1
        for row in tabs.get("accounts", []):
            self._db.execute(
                "INSERT OR REPLACE INTO accounts(code, name, type) VALUES (?,?,?)",
                (str(row["code"]).strip(), str(row["name"]).strip(), str(row["type"]).strip()),
            )
            counts["accounts"] += 1
        for row in tabs.get("sales", []):
            self._sale(
                str(row["invoice"]).strip(),
                _date(row["date"]),
                str(row["customer"]).strip(),
                _cents(row, "amount"),
            )
            counts["sales"] += 1
        for row in tabs.get("payables", []):
            due = _date(row["due"])
            period = period_of(due)
            self._db.execute(
                "INSERT OR REPLACE INTO payables(invoice, vendor, amount_cents, due, period) "
                "VALUES (?,?,?,?,?)",
                (
                    str(row["invoice"]).strip(),
                    str(row["vendor"]).strip(),
                    _cents(row, "amount"),
                    due,
                    period,
                ),
            )
            self._ensure_period(period)
            counts["payables"] += 1
        for row in tabs.get("targets", []):
            period = str(row["period"]).strip()[:7]
            self._db.execute(
                "INSERT OR REPLACE INTO targets(period, revenue_cents, margin_bps, "
                "cost_cap_cents, source, recorded_at) VALUES (?,?,?,?,?,?)",
                (
                    period,
                    _cents(row, "revenue"),
                    _bps(row),
                    _cents(row, "cost_cap"),
                    "import",
                    _now(),
                ),
            )
            self._ensure_period(period)
            counts["targets"] += 1
        self._db.commit()
        return counts

    def load_tabs(self, tabs: Mapping[str, list[Mapping[str, Any]]]) -> dict[str, Any]:
        """A person loads the team's file into the books from chat. The file's tabs replace what
        they describe: sales and open payables of the open months are replaced, accounts,
        targets and prices upserted. Paid payables and locked months are kept and the file's
        rows for them skipped and reported."""
        self._check_write()
        locked = {p for p in self.periods() if self.period_status(p) == "locked"}
        skipped: list[dict[str, str]] = []  # {"invoice", "reason"}
        sales = []
        for r in tabs.get("sales", []):
            p = period_of(_date(r["date"]))
            if p in locked:
                skipped.append({"invoice": str(r["invoice"]), "reason": f"{p} is locked"})
            else:
                sales.append(r)
        paid = {r["invoice"] for r in self.payables() if r["status"] == "paid"}
        payables = []
        for r in tabs.get("payables", []):
            inv = str(r["invoice"]).strip()
            due_period = period_of(_date(r["due"]))
            if inv in paid:
                skipped.append({"invoice": inv, "reason": "paid"})
            elif due_period in locked:
                skipped.append({"invoice": inv, "reason": f"{due_period} is locked"})
            else:
                payables.append(r)
        try:
            if "sales" in tabs:
                for p in [p for p in self.periods() if p not in locked]:
                    self._db.execute(
                        "DELETE FROM lines WHERE journal_id IN "
                        "(SELECT id FROM journal WHERE source='sales' AND period=?)",
                        (p,),
                    )
                    self._db.execute("DELETE FROM journal WHERE source='sales' AND period=?", (p,))
                    self._db.execute("DELETE FROM sales WHERE period=?", (p,))
            if "payables" in tabs:
                self._db.execute("DELETE FROM payables WHERE status='open'")
            rest = {k: v for k, v in tabs.items() if k not in ("sales", "payables")}
            counts = self.import_tabs({**rest, "sales": sales, "payables": payables})
        except Exception:
            self._db.rollback()
            raise
        touched = sorted(
            {period_of(_date(r["date"])) for r in sales}
            | {period_of(_date(r["due"])) for r in payables}
            | {str(r["period"]).strip()[:7] for r in tabs.get("targets", [])}
        )
        return {"counts": counts, "skipped": skipped, "periods": touched}

    def reset(self, folder: str | Path) -> dict[str, int]:
        self._check_write()
        for table in _TABLES:
            self._db.execute(f"DELETE FROM {table}")
        self._db.commit()
        return self.import_dir(folder)

    def clear(self) -> None:
        self._check_write()
        for table in _TABLES:
            self._db.execute(f"DELETE FROM {table}")
        self._db.commit()

    # -- reads ---------------------------------------------------------------------------------

    def accounts(self) -> dict[str, dict[str, str]]:
        rows = self._db.execute("SELECT code, name, type FROM accounts ORDER BY code").fetchall()
        return {r["code"]: {"name": r["name"], "type": r["type"]} for r in rows}

    def periods(self) -> list[str]:
        rows = self._db.execute("SELECT period FROM periods ORDER BY period").fetchall()
        return [r["period"] for r in rows]

    def period_status(self, period: str) -> str:
        row = self._db.execute("SELECT status FROM periods WHERE period=?", (period,)).fetchone()
        return row["status"] if row else "unknown"

    def sales(self, period: str) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT invoice, date, customer, amount_cents FROM sales WHERE period=? ORDER BY n",
            (period,),
        ).fetchall()
        return [dict(r) for r in rows]

    def payables(self, invoices: Iterable[str] | None = None, *, period: str | None = None):
        """Rows as the ledger holds them, sorted by invoice."""
        sql = "SELECT invoice, vendor, amount_cents, due, period, status, run_id FROM payables"
        args: list[Any] = []
        where = []
        if invoices is not None:
            ids = sorted(set(invoices))
            if not ids:
                return []
            where.append(f"invoice IN ({','.join('?' * len(ids))})")
            args += ids
        if period is not None:
            where.append("period=?")
            args.append(period)
        if where:
            sql += " WHERE " + " AND ".join(where)
        rows = self._db.execute(sql + " ORDER BY invoice", args).fetchall()
        return [dict(r) for r in rows]

    def targets(self, period: str) -> dict[str, Any] | None:
        row = self._db.execute(
            "SELECT period, revenue_cents, margin_bps, cost_cap_cents, source FROM targets "
            "WHERE period=?",
            (period,),
        ).fetchone()
        return dict(row) if row else None

    def budget(self, period: str) -> list[dict[str, Any]]:
        """The recorded budget lines of a period (what the entries bind to), by department."""
        rows = self._db.execute(
            "SELECT period, department, account, amount_cents, source FROM budgets "
            "WHERE period=? ORDER BY department",
            (period,),
        ).fetchall()
        return [dict(r) for r in rows]

    def planned(self, period: str) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT period, department, account, amount_cents, source FROM planned "
            "WHERE period=? ORDER BY department",
            (period,),
        ).fetchall()
        return [dict(r) for r in rows]

    def price_book(self) -> dict[str, int]:
        rows = self._db.execute("SELECT product, list_cents FROM price_book ORDER BY product")
        return {r["product"]: int(r["list_cents"]) for r in rows.fetchall()}

    def deal(self, deal_id: str) -> dict[str, Any] | None:
        row = self._db.execute("SELECT * FROM deals WHERE id=?", (deal_id,)).fetchone()
        return dict(row) if row else None

    def deals(self, period: str | None = None) -> list[dict[str, Any]]:
        if period is None:
            rows = self._db.execute("SELECT * FROM deals ORDER BY id").fetchall()
        else:
            rows = self._db.execute(
                "SELECT * FROM deals WHERE period=? ORDER BY id", (period,)
            ).fetchall()
        return [dict(r) for r in rows]

    def next_deal_id(self, period: str) -> str:
        """`D-<year>-<n>`, the next number for the year of the period."""
        year = period[:4]
        rows = self._db.execute(
            "SELECT id FROM deals WHERE id LIKE ? ORDER BY id", (f"D-{year}-%",)
        ).fetchall()
        used = [int(r["id"].rsplit("-", 1)[1]) for r in rows]
        return f"D-{year}-{(max(used) + 1) if used else 1:02d}"

    def trial_balance(self, period: str) -> dict[str, int]:
        """Balance per account (debits minus credits) over everything posted up to `period`."""
        rows = self._db.execute(
            "SELECT l.account, SUM(l.debit_cents) AS d, SUM(l.credit_cents) AS c FROM lines l "
            "JOIN journal j ON j.id=l.journal_id WHERE j.period<=? GROUP BY l.account "
            "ORDER BY l.account",
            (period,),
        ).fetchall()
        return {r["account"]: int(r["d"] or 0) - int(r["c"] or 0) for r in rows}

    def journal_by_source(self, source: str) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT id, period, date, memo FROM journal WHERE source=? ORDER BY id", (source,)
        ).fetchall()
        return [dict(r) for r in rows]

    def journal(self, period: str) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT id, period, date, memo, source FROM journal WHERE period=? ORDER BY id",
            (period,),
        ).fetchall()
        return [dict(r) for r in rows]

    def summary(self, period: str) -> dict[str, Any]:
        """The month's numbers for the analyst: revenue, by customer, duplicates, the costs the
        ledger saw, and the targets the month was given."""
        sales = self.sales(period)
        seen: dict[str, int] = {}
        duplicates: list[str] = []
        by_customer: dict[str, int] = {}
        revenue = 0
        for s in sales:
            if s["invoice"] in seen:
                duplicates.append(s["invoice"])
                continue
            seen[s["invoice"]] = s["amount_cents"]
            revenue += s["amount_cents"]
            by_customer[s["customer"]] = by_customer.get(s["customer"], 0) + s["amount_cents"]
        payables = self.payables(period=period)
        costs = sum(p["amount_cents"] for p in payables)
        margin_bps = round((revenue - costs) * 10000 / revenue) if revenue else 0
        targets = self.targets(period)
        return {
            "period": period,
            "sales_rows": len(sales),
            "invoices": len(seen),
            "revenue_cents": revenue,
            "by_customer": dict(sorted(by_customer.items(), key=lambda kv: -kv[1])),
            "duplicate_invoices": duplicates,
            "payables": payables,
            "costs_cents": costs,
            "margin_bps": margin_bps,
            "targets": targets,
            "budget": self.budget(period),
            "period_status": self.period_status(period),
        }

    # -- digests (what approvals bind to) ------------------------------------------------------

    def actuals_digest(self, period: str) -> str:
        """The rows a budget is built on: the basis period's sales and payables as they stand."""
        return sha256_hex(
            canonical_json(
                {
                    "sales": [
                        {
                            "invoice": s["invoice"],
                            "date": s["date"],
                            "customer": s["customer"],
                            "amount_cents": int(s["amount_cents"]),
                        }
                        for s in self.sales(period)
                    ],
                    "payables": [
                        {
                            "invoice": r["invoice"],
                            "vendor": r["vendor"],
                            "amount_cents": int(r["amount_cents"]),
                            "status": r["status"],
                        }
                        for r in self.payables(period=period)
                    ],
                }
            )
        )

    @staticmethod
    def payables_digest(rows: list[Mapping[str, Any]]) -> str:
        return sha256_hex(
            canonical_json(
                [
                    {
                        "invoice": r["invoice"],
                        "vendor": r["vendor"],
                        "amount_cents": int(r["amount_cents"]),
                        "status": r["status"],
                    }
                    for r in sorted(rows, key=lambda r: r["invoice"])
                ]
            )
        )

    @staticmethod
    def balance_digest(tb: Mapping[str, int]) -> str:
        return sha256_hex(canonical_json(dict(sorted(tb.items()))))

    def open_payables(self, period: str) -> list[dict[str, Any]]:
        return [p for p in self.payables(period=period) if p["status"] == "open"]

    @staticmethod
    def budget_digest(rows: list[Mapping[str, Any]]) -> str:
        return sha256_hex(
            canonical_json(
                [
                    {
                        "department": r["department"],
                        "account": r["account"],
                        "amount_cents": int(r["amount_cents"]),
                    }
                    for r in sorted(rows, key=lambda r: r["department"])
                ]
            )
        )

    # -- writes (sindarin-ledger only) ---------------------------------------------------------

    def record_budget(
        self,
        period: str,
        source: str,
        lines: Iterable[Mapping[str, Any]],
        *,
        targets: Mapping[str, Any] | None = None,
    ) -> str:
        """Record the approved budget lines of a period (and the targets they answer) under
        `source`, the artifact id. Idempotent on the source; a budget recorded by another
        artifact is a refusal."""
        self._check_write()
        existing = self.budget(period)
        if existing and any(r["source"] != source for r in existing):
            other = sorted({r["source"] for r in existing if r["source"] != source})
            raise LedgerError(f"{period} already has a budget recorded by {', '.join(other)}")
        if self.period_status(period) == "planned":
            raise LedgerError(f"period {period} is already planned")
        known = self.accounts()
        rows = [dict(x) for x in lines]
        for x in rows:
            if str(x["account"]) not in known:
                raise LedgerError(f"unknown account {x['account']}")
        try:
            self._ensure_period(period)
            self._db.execute("DELETE FROM budgets WHERE period=? AND source=?", (period, source))
            for x in rows:
                self._db.execute(
                    "INSERT INTO budgets(period, department, account, amount_cents, source, "
                    "recorded_at) VALUES (?,?,?,?,?,?)",
                    (
                        period,
                        str(x["department"]),
                        str(x["account"]),
                        int(x["amount_cents"]),
                        source,
                        _now(),
                    ),
                )
            if targets:
                self._db.execute(
                    "INSERT OR REPLACE INTO targets(period, revenue_cents, margin_bps, "
                    "cost_cap_cents, source, recorded_at) VALUES (?,?,?,?,?,?)",
                    (
                        period,
                        int(targets["revenue_cents"]),
                        int(targets["margin_bps"]),
                        int(targets["cost_cap_cents"]),
                        str(targets.get("source") or source),
                        _now(),
                    ),
                )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        return self.budget_digest(self.budget(period))

    def post_budget_entries(
        self, period: str, source: str, lines: Iterable[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        """Post the budget register entries: the period's planned amounts per department and
        account, then mark the period planned. Idempotent on `source`."""
        self._check_write()
        existing = self.planned(period)
        if existing:
            if all(r["source"] == source for r in existing):
                return existing
            other = sorted({r["source"] for r in existing if r["source"] != source})
            raise LedgerError(f"{period} was already planned by {', '.join(other)}")
        recorded = {r["department"]: r for r in self.budget(period)}
        rows = [dict(x) for x in lines]
        try:
            for x in rows:
                r = recorded.get(str(x["department"]))
                if r is None:
                    raise LedgerError(f"{x['department']} is not in the recorded budget")
                if r["account"] != str(x["account"]) or int(r["amount_cents"]) != int(
                    x["amount_cents"]
                ):
                    raise LedgerError(f"{x['department']} differs from the recorded budget")
                self._db.execute(
                    "INSERT INTO planned(period, department, account, amount_cents, source, "
                    "posted_at) VALUES (?,?,?,?,?,?)",
                    (
                        period,
                        str(x["department"]),
                        str(x["account"]),
                        int(x["amount_cents"]),
                        source,
                        _now(),
                    ),
                )
            self._ensure_period(period)
            self._db.execute(
                "UPDATE periods SET status='planned', planned_at=COALESCE(planned_at, ?), "
                "budget_sha=? WHERE period=?",
                (_now(), self.budget_digest(list(recorded.values())), period),
            )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        return self.planned(period)

    def pay(self, run_id: str, invoices: Iterable[str], date: str | None = None) -> int:
        """Mark the payables paid under `run_id` and book the payments. Idempotent on the run:
        rows already paid by this run are left alone; rows paid by another run or missing are
        a refusal."""
        self._check_write()
        ids = sorted(set(invoices))
        rows = {r["invoice"]: r for r in self.payables(ids)}
        missing = [i for i in ids if i not in rows]
        if missing:
            raise LedgerError(f"unknown payables: {', '.join(missing)}")
        other = [i for i in ids if rows[i]["status"] == "paid" and rows[i]["run_id"] != run_id]
        if other:
            raise LedgerError(f"already paid by another run: {', '.join(other)}")
        when = date or _now()[:10]
        paid = 0
        for i in ids:
            r = rows[i]
            if r["status"] == "paid":
                continue
            if self.period_status(r["period"]) == "locked":
                raise LedgerError(f"period {r['period']} is locked")
            self._db.execute(
                "UPDATE payables SET status='paid', run_id=?, paid_at=? WHERE invoice=?",
                (run_id, _now(), i),
            )
            jid = self._journal(r["period"], when, f"paid {i} {r['vendor']}", run_id)
            self._line(jid, EXPENSE, int(r["amount_cents"]), 0)
            self._line(jid, CASH, 0, int(r["amount_cents"]))
            paid += 1
        self._db.commit()
        return paid

    def post_entries(
        self,
        period: str,
        entries: Iterable[Mapping[str, Any]],
        source: str,
        date: str | None = None,
    ) -> list[int]:
        """Post balanced entries into an open period in one transaction. Idempotent on `source`:
        when entries from this source exist, nothing is posted again."""
        self._check_write()
        if self.period_status(period) == "locked":
            raise LedgerError(f"period {period} is locked")
        existing = self.journal_by_source(source)
        if existing:
            return [e["id"] for e in existing]
        known = self.accounts()
        ids: list[int] = []
        when = date or _now()[:10]
        try:
            for e in entries:
                lines = list(e["lines"])
                debits = sum(int(x.get("debit_cents", 0)) for x in lines)
                credits = sum(int(x.get("credit_cents", 0)) for x in lines)
                if debits != credits:
                    raise LedgerError(f"entry {e.get('memo', '')!r} is not balanced")
                for x in lines:
                    if str(x["account"]) not in known:
                        raise LedgerError(f"unknown account {x['account']}")
                self._ensure_period(period)
                jid = self._journal(period, when, str(e["memo"]), source)
                for x in lines:
                    self._line(
                        jid,
                        str(x["account"]),
                        int(x.get("debit_cents", 0)),
                        int(x.get("credit_cents", 0)),
                    )
                ids.append(jid)
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        return ids

    def lock_period(self, period: str) -> str:
        """Lock the period and record the trial balance it closed with. Idempotent."""
        self._check_write()
        self._ensure_period(period)
        digest = self.balance_digest(self.trial_balance(period))
        self._db.execute(
            "UPDATE periods SET status='locked', locked_at=COALESCE(locked_at, ?), "
            "trial_balance_sha=? WHERE period=?",
            (_now(), digest, period),
        )
        self._db.commit()
        return digest

    def record_deal(self, source: str, deal: Mapping[str, Any]) -> dict[str, Any]:
        """Record a won deal and post its sale into the period's books. Idempotent on the deal
        id; a deal already won by another artifact is a refusal; a locked period refuses."""
        self._check_write()
        existing = self.deal(str(deal["deal_id"]))
        if existing is not None:
            if existing["source"] == source:
                return existing
            raise LedgerError(
                f"{deal['deal_id']} is already {existing['stage']} ({existing['source']})"
            )
        period = str(deal["period"])
        if self.period_status(period) == "locked":
            raise LedgerError(f"period {period} is locked")
        try:
            self._db.execute(
                "INSERT INTO deals(id, account, product, list_cents, price_cents, discount_bps, "
                "terms, period, stage, source, won_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(deal["deal_id"]),
                    str(deal["account"]),
                    str(deal["product"]),
                    int(deal["list_cents"]),
                    int(deal["price_cents"]),
                    int(deal["discount_bps"]),
                    str(deal.get("terms", "standard")),
                    period,
                    "won",
                    source,
                    _now(),
                ),
            )
            self._sale(
                str(deal["deal_id"]),
                f"{period}-{min(int(_now()[8:10] or 1), 28):02d}",
                str(deal["account"]),
                int(deal["price_cents"]),
            )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise
        return self.deal(str(deal["deal_id"])) or {}

    def set_list_price(self, product: str, list_cents: int) -> dict[str, Any]:
        """Edit a list price (the books move under a pending deal)."""
        self._check_write()
        if product not in self.price_book():
            raise LedgerError(f"unknown product {product}")
        self._db.execute(
            "UPDATE price_book SET list_cents=? WHERE product=?", (int(list_cents), product)
        )
        self._db.commit()
        return {"product": product, "list_cents": int(list_cents)}

    def add_sale(self, invoice: str, date: str, customer: str, amount_cents: int) -> dict:
        """A late sale lands in the books (the demo's 'changed after the proposal' moment)."""
        self._check_write()
        if amount_cents <= 0:
            raise LedgerError("the amount must be positive")
        if self.period_status(period_of(date)) == "locked":
            raise LedgerError(f"period {period_of(date)} is locked; its books take no sale")
        self._sale(invoice, date, customer, int(amount_cents))
        self._db.commit()
        return {
            "invoice": invoice,
            "date": date,
            "customer": customer,
            "amount_cents": amount_cents,
        }

    def amend_payable(self, invoice: str, amount_cents: int) -> dict[str, Any]:
        """Edit an open payable (the other way the books can move under a proposal)."""
        self._check_write()
        rows = self.payables([invoice])
        if not rows:
            raise LedgerError(f"unknown payable {invoice}")
        if rows[0]["status"] != "open":
            raise LedgerError(f"{invoice} is {rows[0]['status']}, not open")
        self._db.execute(
            "UPDATE payables SET amount_cents=? WHERE invoice=?", (int(amount_cents), invoice)
        )
        self._db.commit()
        return self.payables([invoice])[0]

    # -- internals -----------------------------------------------------------------------------

    def _check_write(self) -> None:
        if self.readonly:
            raise LedgerError("this ledger handle is read-only")

    def _ensure_period(self, period: str) -> None:
        self._db.execute("INSERT OR IGNORE INTO periods(period) VALUES (?)", (period,))

    def _sale(self, invoice: str, date: str, customer: str, amount_cents: int) -> None:
        period = period_of(date)
        self._db.execute(
            "INSERT INTO sales(invoice, date, customer, amount_cents, period) VALUES (?,?,?,?,?)",
            (invoice, date, customer, amount_cents, period),
        )
        self._ensure_period(period)
        jid = self._journal(period, date, f"sale {invoice} {customer}", "sales")
        self._line(jid, RECEIVABLE, amount_cents, 0)
        self._line(jid, REVENUE, 0, amount_cents)

    def _journal(self, period: str, date: str, memo: str, source: str) -> int:
        cur = self._db.execute(
            "INSERT INTO journal(period, date, memo, source, posted_at) VALUES (?,?,?,?,?)",
            (period, date, memo, source, _now()),
        )
        return int(cur.lastrowid)

    def _line(self, journal_id: int, account: str, debit: int, credit: int) -> None:
        self._db.execute(
            "INSERT INTO lines(journal_id, account, debit_cents, credit_cents) VALUES (?,?,?,?)",
            (journal_id, account, debit, credit),
        )


def render_trial_balance(ledger: Ledger, period: str, currency: str = "USD") -> str:
    from sindarin.schema import money

    names = ledger.accounts()
    lines = []
    for code, bal in ledger.trial_balance(period).items():
        lines.append(f"{code} {names.get(code, {}).get('name', '?')}: {money(bal, currency)}")
    return "\n".join(lines) or "(empty)"
