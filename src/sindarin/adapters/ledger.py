"""The ledger adapter (design v5 §5): the one process that writes the ledger.

`read_facts` binds an approval to what the ledger holds now: the basis period's actuals for a
budget (the rows the planner built it on), the recorded budget for its register entries.
`execute` does the one write; `verify` reads it back. Never trusts the fence's numbers.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

from sindarin.adapters.base import ActionResult, AdapterError, Facts, VerifyResult
from sindarin.connect import ConnectorError
from sindarin.ledger import Ledger, LedgerError
from sindarin.payments import BankFileRail, PaymentLine, PaymentRail, RailError
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

    def __init__(
        self,
        ledger: Ledger,
        writer: Any | None = None,
        rail: PaymentRail | None = None,
        billing: Any | None = None,
        crm: Any | None = None,
        esign: Any | None = None,
    ) -> None:
        self.ledger = ledger
        self.writer = writer  # a SheetWriter when the team's sheet is configured
        # how an approved run reaches the vendors: the bank file beside the books by default
        self.rail: PaymentRail = rail or BankFileRail(Path(ledger.path).parent / "payments")
        self.billing = billing  # a StripeBilling when invoicing is on: a won deal is invoiced
        self.crm = crm  # a HubSpot when the CRM is on: the deal lives there too
        self.esign = esign  # a DropboxSign when e-signature is on: the order form goes out

    def crm_sync(self, artifact: Artifact, stage: str, *, known_id: str | None = None) -> dict:
        if self.crm is None:
            raise AdapterError("no CRM configured")
        body: Any = artifact.proposal().body
        try:
            out = self.crm.upsert_deal(
                body.deal_id,
                account=body.account,
                product=body.product,
                amount_cents=int(body.price_cents),
                stage=stage,
                known_id=known_id,
            )
        except ConnectorError as exc:
            raise AdapterError(str(exc)[:160]) from None
        return {"crm_name": self.crm.name, **out}

    def send_contract(self, artifact: Artifact) -> dict[str, Any]:
        if self.esign is None:
            raise AdapterError("no e-signature configured")
        from sindarin.esign import order_form_pdf

        body: Any = artifact.proposal().body
        pdf = order_form_pdf(
            body.deal_id,
            account=body.account,
            product=body.product,
            price_cents=int(body.price_cents),
            list_cents=int(body.list_cents),
            discount_bps=int(body.discount_bps),
            currency=body.currency,
            period=body.period,
            terms=str(getattr(body, "terms", "standard")),
        )
        try:
            out = self.esign.send_order_form(body.deal_id, account=body.account, pdf=pdf)
        except ConnectorError as exc:
            raise AdapterError(str(exc)[:160]) from None
        return {"provider": self.esign.name, **out}

    def poll_contract(self, request_id: str) -> dict[str, Any]:
        if self.esign is None:
            raise AdapterError("no e-signature configured")
        try:
            return self.esign.status(request_id)
        except ConnectorError as exc:
            raise AdapterError(str(exc)[:160]) from None

    def poll_invoice(self, deal_id: str, invoice_id: str) -> dict[str, Any]:
        """Where the deal's invoice stands with the biller; book the receipt once it is paid."""
        if self.billing is None:
            raise AdapterError("no billing configured")
        try:
            seen = self.billing.status(invoice_id)
        except RailError as exc:
            raise AdapterError(str(exc)[:160]) from None
        out = {"invoice_id": invoice_id, **seen}
        if seen.get("paid"):
            try:
                out["receipt"] = self.ledger.collect_sale(deal_id, reference=invoice_id)
            except LedgerError as exc:
                raise AdapterError(str(exc)[:160]) from None
            self._mirror_sales(out["receipt"]["period"])
        return out

    def _mirror_sales(self, period: str) -> None:
        if self.writer is None:
            return
        try:
            self.writer.write_tab("sales", self.ledger.sales(period))
        except Exception as exc:  # the sheet is never the system of record
            import logging

            logging.getLogger("sindarin.sheets").warning(
                "write-back to the sheet failed: %s", str(exc)[:120]
            )

    def _lines(self, body: Any) -> list[PaymentLine]:
        rows = {r["invoice"]: r for r in self.ledger.payables([x.invoice for x in body.run])}
        return [
            PaymentLine(
                invoice=x.invoice,
                vendor=str(rows[x.invoice]["vendor"]) if x.invoice in rows else x.vendor,
                amount_cents=int(rows[x.invoice]["amount_cents"])
                if x.invoice in rows
                else x.amount_cents,
                currency=body.currency,
                due=str(rows[x.invoice]["due"]) if x.invoice in rows else "",
            )
            for x in body.run
        ]

    def confirm(self, run_id: str, reference: str) -> dict[str, Any]:
        """The bank's confirmation, typed by a person: the sent rows become paid and booked."""
        try:
            paid = self.ledger.confirm_paid(run_id, reference=reference)
        except LedgerError as exc:
            raise AdapterError(str(exc)[:160]) from None
        rows = [r for r in self.ledger.payables() if r["run_id"] == run_id]
        out = {
            "paid": sum(1 for r in rows if r["status"] == "paid"),
            "paid_now": paid,
            "reference": reference,
        }
        self._mirror_payables(rows)
        return out

    def poll(self, run_id: str) -> dict[str, Any]:
        """Ask the rail where each sent payable of the run stands; book the ones it calls paid."""
        rows = [r for r in self.ledger.payables() if r["run_id"] == run_id]
        landed: list[str] = []
        failed: list[str] = []
        for r in rows:
            if r["status"] != "sent" or not r["reference"]:
                continue
            try:
                t = self.rail.status(str(r["reference"]))
            except RailError as exc:
                raise AdapterError(str(exc)[:160]) from None
            if t.status == "paid":
                landed.append(str(r["invoice"]))
            elif t.status == "failed":
                failed.append(f"{r['invoice']} ({t.detail or 'failed'})")
        if landed:
            try:
                self.ledger.confirm_paid(run_id, invoices=landed)
            except LedgerError as exc:
                raise AdapterError(str(exc)[:160]) from None
            rows = [r for r in self.ledger.payables() if r["run_id"] == run_id]
            self._mirror_payables(rows)
        return {
            "paid": sum(1 for r in rows if r["status"] == "paid"),
            "paid_now": len(landed),
            "still_sent": sum(1 for r in rows if r["status"] == "sent"),
            "failed": failed,
            "rail": self.rail.name,
        }

    def _mirror_payables(self, rows: list[dict[str, Any]]) -> None:
        if self.writer is None or not rows:
            return
        try:
            self.writer.write_tab("payables", self.ledger.payables(period=rows[0]["period"]))
        except Exception as exc:  # the sheet is never the system of record
            import logging

            logging.getLogger("sindarin.sheets").warning(
                "write-back to the sheet failed: %s", str(exc)[:120]
            )

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
                lines = self._lines(body)
                already = {
                    r["invoice"]: r
                    for r in self.ledger.payables([x.invoice for x in lines])
                    if r["run_id"] == artifact.id and r["status"] in ("sent", "paid")
                }
                todo = [x for x in lines if x.invoice not in already]
                try:
                    transfers = self.rail.send(artifact.id, todo) if todo else {}
                except RailError as exc:
                    raise AdapterError(str(exc)[:160]) from None
                failed = [f"{i} ({t.detail})" for i, t in transfers.items() if t.status == "failed"]
                if failed:
                    raise AdapterError(f"{self.rail.name}: {'; '.join(failed)}"[:160])
                if transfers:
                    self.ledger.send(
                        artifact.id, {i: t.reference for i, t in transfers.items()}, self.rail.name
                    )
                landed = [i for i, t in transfers.items() if t.status == "paid"]
                if landed:
                    self.ledger.confirm_paid(artifact.id, invoices=landed)
                rows = [r for r in self.ledger.payables() if r["run_id"] == artifact.id]
                data: dict[str, Any] = {
                    "run_id": artifact.id,
                    "rail": self.rail.name,
                    "rail_confirms": bool(self.rail.confirms),
                    "sent": sum(1 for r in rows if r["status"] in ("sent", "paid")),
                    "paid": sum(1 for r in rows if r["status"] == "paid"),
                    "total_cents": body.total_cents,
                    "currency": body.currency,
                    "references": {str(r["invoice"]): str(r["reference"] or "") for r in rows},
                }
                doc = self.rail.document(artifact.id, lines)
                if doc:
                    data["document"], data["document_csv"] = doc
                if hasattr(self.rail, "balance"):
                    with contextlib.suppress(Exception):  # a read-back nicety, never a failure
                        data["balance_after_cents"] = int(self.rail.balance())
                return ActionResult(ok=True, data=data)
            if artifact.kind is Kind.DEAL:
                row = self.ledger.record_deal(artifact.id, body.model_dump())
                data = {
                    "deal_id": body.deal_id,
                    "account": body.account,
                    "price_cents": body.price_cents,
                    "currency": body.currency,
                    "period": body.period,
                    "stage": row.get("stage", "won"),
                    "revenue_cents": int(self.ledger.summary(body.period)["revenue_cents"]),
                }
                if self.billing is not None:
                    # the bill goes out once the deal is in the books; a billing failure is
                    # reported in the room, never a reason to un-win the deal
                    try:
                        data.update(
                            self.billing.invoice(
                                body.deal_id,
                                account=body.account,
                                product=body.product,
                                amount_cents=int(body.price_cents),
                                currency=body.currency,
                                period=body.period,
                            )
                        )
                    except RailError as exc:
                        data["invoice_error"] = str(exc)[:160]
                return ActionResult(ok=True, data=data)
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
                    r["status"] in ("sent", "paid") and r["run_id"] == artifact.id for r in rows
                )
                paid = sum(1 for r in rows if r["status"] == "paid")
                return VerifyResult(
                    present=ok, data={"sent": len(rows), "paid": paid} if ok else {}
                )
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
