"""The ledger adapter (design v5 §5): the one process that writes the ledger.

`read_facts` binds an approval to what the ledger holds now: the basis period's actuals for a
budget (the rows the planner built it on), the recorded budget for its register entries.
`execute` does the one write; `verify` reads it back. Never trusts the fence's numbers.
"""

from __future__ import annotations

from typing import Any

from sindarin.adapters.base import ActionResult, AdapterError, Facts, VerifyResult
from sindarin.ledger import Ledger, LedgerError
from sindarin.schema import Artifact, Kind, Proposal, canonical_json, sha256_hex


def budget_facts(ledger: Ledger, period: str, basis: str, total_cents: int) -> dict[str, Any]:
    """What a budget binds to: the basis period's actuals (revenue, rows) and the chart. The
    goals' cap is added by the executor from its own record of the approved goals."""
    s = ledger.summary(basis)
    return {
        "basis": basis,
        "basis_status": "unknown" if not s["sales_rows"] else s["period_status"],
        "revenue_cents": int(s["revenue_cents"]),
        "sales_rows": int(s["sales_rows"]),
        "costs_cents": int(s["costs_cents"]),
        "accounts": sorted(ledger.accounts()),
        "period_status": ledger.period_status(period),
        "total_cents": int(total_cents),
        "digest": ledger.actuals_digest(basis),
    }


def pay_facts(ledger: Ledger, invoices: list[str]) -> dict[str, Any]:
    rows = ledger.payables(invoices)
    return {
        "payables": [
            {
                "invoice": r["invoice"],
                "vendor": r["vendor"],
                "amount_cents": int(r["amount_cents"]),
                "status": r["status"],
            }
            for r in rows
        ],
        "total_cents": sum(int(r["amount_cents"]) for r in rows),
        "digest": Ledger.payables_digest(rows),
    }


def lock_facts(ledger: Ledger, period: str) -> dict[str, Any]:
    tb = ledger.trial_balance(period)
    return {
        "period_status": ledger.period_status(period),
        "open_payables": len(ledger.open_payables(period)),
        "digest": Ledger.balance_digest(tb),
    }


def deal_facts(
    ledger: Ledger, period: str, product: str, deal_id: str, discount_cents: int
) -> dict:
    """What a deal binds to: the product's list price as the price book holds it now, the
    deal's state, the period's state. `total_cents` is the discount, which picks the group."""
    book = ledger.price_book()
    existing = ledger.deal(deal_id)
    data = {
        "product": product,
        "list_cents": int(book.get(product, 0)),
        "period_status": ledger.period_status(period),
        "deal_stage": existing["stage"] if existing else "",
        "total_cents": int(discount_cents),
    }
    data["digest"] = sha256_hex(
        canonical_json({k: v for k, v in data.items() if k != "total_cents"})
    )
    return data


def entries_facts(ledger: Ledger, period: str) -> dict[str, Any]:
    rows = ledger.budget(period)
    return {
        "period_status": ledger.period_status(period),
        "budget_id": rows[0]["source"] if rows else "",
        "budget_lines": [
            {
                "department": r["department"],
                "account": r["account"],
                "amount_cents": int(r["amount_cents"]),
            }
            for r in rows
        ],
        "digest": Ledger.budget_digest(rows),
    }


class LedgerAdapter:
    platform = "ledger"
    kinds = frozenset({Kind.BUDGET, Kind.ENTRIES, Kind.PAY, Kind.LOCK, Kind.DEAL})

    def __init__(self, ledger: Ledger, writer: Any | None = None) -> None:
        self.ledger = ledger
        self.writer = writer  # a SheetWriter when the team's sheet is configured

    def _mirror(self, artifact: Artifact) -> None:
        """Write what just landed back into the team's sheet: the budget, the planned lines,
        the payables. Best effort and logged: the ledger is the record, the sheet its mirror."""
        if self.writer is None:
            return
        body: Any = artifact.proposal().body
        try:
            if artifact.kind is Kind.BUDGET:
                self.writer.write_tab("budget", self.ledger.budget(body.period))
            elif artifact.kind is Kind.ENTRIES:
                self.writer.write_tab("planned", self.ledger.planned(body.period))
            elif artifact.kind is Kind.DEAL:
                self.writer.write_tab("deals", self.ledger.deals())
                self.writer.write_tab("sales", self.ledger.sales(body.period))
            else:
                self.writer.write_tab("payables", self.ledger.payables(period=body.period))
        except Exception as exc:  # the sheet is never the system of record
            import logging

            logging.getLogger("sindarin.sheets").warning(
                "write-back to the sheet failed: %s", str(exc)[:120]
            )

    def read_facts(self, proposal: Proposal) -> Facts:
        body: Any = proposal.body
        try:
            if proposal.kind is Kind.BUDGET:
                from sindarin.schema import previous_period

                return Facts(
                    ok=True,
                    data=budget_facts(
                        self.ledger, body.period, previous_period(body.period), body.total_cents
                    ),
                )
            if proposal.kind is Kind.ENTRIES:
                return Facts(ok=True, data=entries_facts(self.ledger, body.period))
            if proposal.kind is Kind.PAY:
                return Facts(ok=True, data=pay_facts(self.ledger, [x.invoice for x in body.run]))
            if proposal.kind is Kind.LOCK:
                return Facts(ok=True, data=lock_facts(self.ledger, body.period))
            if proposal.kind is Kind.DEAL:
                return Facts(
                    ok=True,
                    data=deal_facts(
                        self.ledger, body.period, body.product, body.deal_id, body.discount_cents
                    ),
                )
        except (LedgerError, OSError) as exc:
            return Facts(ok=False, error=str(exc)[:160])
        return Facts(ok=False, error=f"{proposal.kind.value} is not a ledger action")

    def load_books(self, tabs: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
        """A person loads the team's file into the books. Not gated: an event on the record,
        under their id; locked months and paid payables are kept."""
        try:
            return self.ledger.load_tabs(tabs)
        except LedgerError as exc:
            raise AdapterError(str(exc)[:160]) from None
        except (KeyError, ValueError, TypeError) as exc:
            raise AdapterError(
                f"a row the books cannot read ({exc.__class__.__name__}: {exc})"
            ) from None

    def set_price(self, product: str, amount_cents: int) -> dict[str, Any]:
        """A person edits a list price. Not gated: an event on the record, under their id."""
        was = self.ledger.price_book().get(product)
        if was is None:
            raise AdapterError(f"{product} is not in the price book")
        try:
            self.ledger.set_list_price(product, int(amount_cents))
        except LedgerError as exc:
            raise AdapterError(str(exc)[:160]) from None
        return {"product": product, "list_cents": int(amount_cents), "was_cents": int(was)}

    def book_sale(self, invoice: str, customer: str, amount_cents: int) -> dict[str, Any]:
        """A late sale lands in the newest month that has sales (the basis of the plan being
        made). Not gated: a person's event on the record, audited under their id."""
        periods = [p for p in self.ledger.periods() if self.ledger.sales(p)]
        if not periods:
            raise AdapterError("the ledger has no month to land a sale in")
        period = periods[-1]
        try:
            self.ledger.add_sale(invoice, f"{period}-28", customer, int(amount_cents))
        except LedgerError as exc:
            raise AdapterError(str(exc)[:160]) from None
        s = self.ledger.summary(period)
        return {
            "period": period,
            "revenue_cents": int(s["revenue_cents"]),
            "sales_rows": int(s["sales_rows"]),
        }

    def execute(self, artifact: Artifact) -> ActionResult:
        result = self._execute(artifact)
        self._mirror(artifact)
        return result

    def _execute(self, artifact: Artifact) -> ActionResult:
        p = artifact.proposal()
        body: Any = p.body
        facts = artifact.facts()
        try:
            if artifact.kind is Kind.BUDGET:
                targets = None
                if facts.get("cap_cents") is not None:
                    targets = {
                        "revenue_cents": int(facts.get("revenue_target_cents", 0)),
                        "margin_bps": int(facts.get("margin_target_bps", 0)),
                        "cost_cap_cents": int(facts["cap_cents"]),
                        "source": str(facts.get("goals") or body.goals),
                    }
                digest = self.ledger.record_budget(
                    body.period,
                    artifact.id,
                    [x.model_dump() for x in body.lines],
                    targets=targets,
                )
                return ActionResult(
                    ok=True,
                    data={
                        "period": body.period,
                        "lines": len(body.lines),
                        "total_cents": body.total_cents,
                        "currency": body.currency,
                        "budget_sha": digest,
                    },
                )
            if artifact.kind is Kind.ENTRIES:
                rows = self.ledger.post_budget_entries(
                    body.period, artifact.id, [x.model_dump() for x in body.lines]
                )
                return ActionResult(
                    ok=True,
                    data={
                        "period": body.period,
                        "lines_posted": len(rows),
                        "total_cents": body.total_cents,
                    },
                )
            if artifact.kind is Kind.PAY:
                paid = self.ledger.pay(artifact.id, [x.invoice for x in body.run])
                return ActionResult(
                    ok=True,
                    data={
                        "run_id": artifact.id,
                        "paid": paid,
                        "total_cents": body.total_cents,
                        "currency": body.currency,
                    },
                )
            if artifact.kind is Kind.DEAL:
                row = self.ledger.record_deal(artifact.id, body.model_dump())
                return ActionResult(
                    ok=True,
                    data={
                        "deal_id": body.deal_id,
                        "account": body.account,
                        "price_cents": body.price_cents,
                        "currency": body.currency,
                        "period": body.period,
                        "stage": row.get("stage", "won"),
                        "revenue_cents": int(self.ledger.summary(body.period)["revenue_cents"]),
                    },
                )
            if artifact.kind is Kind.LOCK:
                posted = self.ledger.post_entries(
                    body.period, [e.model_dump() for e in body.entries], artifact.id
                )
                digest = self.ledger.lock_period(body.period)
                return ActionResult(
                    ok=True,
                    data={
                        "period": body.period,
                        "entries_posted": len(posted),
                        "trial_balance_sha": digest,
                    },
                )
        except LedgerError as exc:
            raise AdapterError(str(exc)[:160]) from None
        raise AdapterError(f"{artifact.kind.value} is not a ledger action")

    def verify(self, artifact: Artifact) -> VerifyResult:
        p = artifact.proposal()
        body: Any = p.body
        try:
            if artifact.kind is Kind.BUDGET:
                rows = self.ledger.budget(body.period)
                ok = len(rows) == len(body.lines) and all(r["source"] == artifact.id for r in rows)
                return VerifyResult(present=ok, data={"lines": len(rows)} if ok else {})
            if artifact.kind is Kind.ENTRIES:
                planned = self.ledger.period_status(body.period) == "planned"
                rows = self.ledger.planned(body.period)
                ok = planned and len(rows) == len(body.lines)
                return VerifyResult(present=ok, data={"lines_posted": len(rows)} if ok else {})
            if artifact.kind is Kind.PAY:
                rows = self.ledger.payables([x.invoice for x in body.run])
                ok = len(rows) == len(body.run) and all(
                    r["status"] == "paid" and r["run_id"] == artifact.id for r in rows
                )
                return VerifyResult(present=ok, data={"paid": len(rows)} if ok else {})
            if artifact.kind is Kind.DEAL:
                row = self.ledger.deal(body.deal_id)
                ok = bool(row) and row["stage"] == "won" and row["source"] == artifact.id
                sold = any(s["invoice"] == body.deal_id for s in self.ledger.sales(body.period))
                return VerifyResult(present=ok and sold, data={"stage": "won"} if ok else {})
            if artifact.kind is Kind.LOCK:
                locked = self.ledger.period_status(body.period) == "locked"
                posted = self.ledger.journal_by_source(artifact.id)
                ok = locked and len(posted) == len(body.entries)
                return VerifyResult(present=ok, data={"entries_posted": len(posted)} if ok else {})
        except (LedgerError, OSError) as exc:
            return VerifyResult(present=None, error=str(exc)[:160])
        return VerifyResult(present=None, error="not a ledger action")
