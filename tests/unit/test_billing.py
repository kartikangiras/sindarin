"""A won deal becomes a Stripe invoice; the receipt is booked when the customer pays."""

from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest
from validation.harness import JOHN, Harness

from sindarin.billing import StripeBilling
from sindarin.executor import core
from sindarin.ledger import Ledger, LedgerError
from sindarin.payments import RailError

SAMPLE_PERIOD = "2026-10"


class _StripeBilling:
    """A fake Stripe: customers, invoice items, invoices that finalize and can be paid."""

    def __init__(self) -> None:
        self.customers: dict[str, dict] = {}
        self.items: list[dict] = []
        self.invoices: dict[str, dict] = {}
        self.keys: list[str] = []
        self.tamper = False  # when set, every read-back comes back with the wrong amount

    def __call__(self, req: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()} if req.content else {}
        if req.headers.get("idempotency-key"):
            self.keys.append(req.headers["idempotency-key"])
        p, m = req.url.path, req.method
        if m == "POST" and p == "/v1/customers":
            cid = f"cus_{len(self.customers) + 1}"
            self.customers[cid] = {"id": cid, **form}
            return httpx.Response(200, json=self.customers[cid])
        if m == "POST" and p == "/v1/invoiceitems":
            self.items.append(form)
            if form.get("invoice"):  # attached by id: the only way the amount reaches the invoice
                self.invoices[form["invoice"]]["amount_due"] += int(form["amount"])
            return httpx.Response(200, json={"id": f"ii_{len(self.items)}", **form})
        if m == "POST" and p == "/v1/invoices":
            iid = f"in_{len(self.invoices) + 1}"
            assert form.get("pending_invoice_items_behavior") == "exclude"
            self.invoices[iid] = {
                "id": iid,
                "status": "draft",
                "amount_due": 0,
                "amount_paid": 0,
                "paid": False,
                "hosted_invoice_url": None,
                "metadata": {
                    "deal": form.get("metadata[deal]"),
                    "period": form.get("metadata[period]"),
                },
            }
            return httpx.Response(200, json=self.invoices[iid])
        if m == "POST" and p.endswith("/finalize"):
            inv = self.invoices[p.split("/")[3]]
            inv["status"], inv["hosted_invoice_url"] = (
                "open",
                f"https://invoice.stripe.com/i/{inv['id']}",
            )
            return httpx.Response(200, json=inv)
        if m == "GET" and p.startswith("/v1/invoices/"):
            inv = dict(self.invoices[p.rsplit("/", 1)[1]])
            if self.tamper:
                inv["amount_due"] = 5
            return httpx.Response(200, json=inv)
        return httpx.Response(404, json={"error": {"code": "resource_missing", "message": p}})

    def pay(self, iid: str) -> None:
        inv = self.invoices[iid]
        inv.update(status="paid", paid=True, amount_paid=inv["amount_due"])


def test_an_invoice_is_created_finalized_and_read_back():
    fake = _StripeBilling()
    billing = StripeBilling("sk_test_x", transport=httpx.MockTransport(fake))
    out = billing.invoice(
        "D-2026-01",
        account="Contoso Ltd",
        product="Team",
        amount_cents=1_020_000,
        currency="USD",
        period="2026-11",
    )
    assert out["invoice_id"] == "in_1" and out["invoice_url"] == "https://invoice.stripe.com/i/in_1"
    assert out["invoice_status"] == "open" and out["due_days"] == 30
    assert fake.customers["cus_1"]["name"] == "Contoso Ltd"
    assert fake.customers["cus_1"]["email"] == "billing+contoso-ltd@example.com"
    assert fake.items[0]["amount"] == "1020000" and fake.items[0]["currency"] == "usd"
    assert fake.keys == [
        "sindarin-customer-contoso-ltd-v2",
        "sindarin-D-2026-01-invoice-v2",
        "sindarin-D-2026-01-item-v2",
        "sindarin-D-2026-01-finalize-v2",
    ]
    assert fake.items[0]["invoice"] == "in_1"
    assert billing.status("in_1")["paid"] is False
    fake.pay("in_1")
    assert billing.status("in_1") == {
        "status": "paid",
        "amount_paid_cents": 1_020_000,
        "paid": True,
    }
    # a read-back that does not match the deal is refused, not reported
    fake.tamper = True
    with pytest.raises(RailError, match="read-back"):
        billing.invoice(
            "D-2026-01",
            account="Contoso Ltd",
            product="Team",
            amount_cents=1_020_000,
            currency="USD",
            period="2026-11",
        )


def test_the_ledger_books_a_receipt_once(tmp_path):
    from pathlib import Path

    books = Ledger(tmp_path / "books.db")
    books.import_dir(Path(__file__).resolve().parents[2] / "data" / "acme" / SAMPLE_PERIOD)
    sale = books.sales(SAMPLE_PERIOD)[0]
    out = books.collect_sale(sale["invoice"], reference="in_9")
    assert out["receipt"] == "in_9" and out["amount_cents"] == sale["amount_cents"]
    assert len(books.journal_by_source(f"receipt:{sale['invoice']}")) == 1
    books.collect_sale(sale["invoice"], reference="in_9")  # idempotent
    assert len(books.journal_by_source(f"receipt:{sale['invoice']}")) == 1
    row = books.sale(sale["invoice"])
    assert row["receipt"] == "in_9" and row["collected_at"]
    with pytest.raises(LedgerError, match="unknown sale"):
        books.collect_sale("S-0000", reference="x")
    books.close()


def test_a_won_deal_is_invoiced_and_the_receipt_lands_when_paid(harness: Harness):
    h = harness
    fake = _StripeBilling()
    h.ledger.adapters["ledger"].billing = StripeBilling(
        "sk_test_x", transport=httpx.MockTransport(fake)
    )
    h.propose_deal()
    fx = h.approve_deal(sender=JOHN)
    texts = [e.text for e in fx if isinstance(e, core.Post)]
    assert "invoice **in_1** sent, due in 30 days" in texts[0]
    assert texts[1].startswith(
        "**DEAL-1** invoice for Contoso Ltd: https://invoice.stripe.com/i/in_1"
    )
    assert (
        h.deal().result()["invoice_id"] == "in_1"
        and fake.invoices["in_1"]["metadata"]["deal"] == "D-2026-01"
    )
    # not paid yet: the sweep asks and says nothing
    fx = h.sweep("poll 1")
    assert not [e for e in fx if isinstance(e, core.Post) and "paid by" in e.text]
    assert h.ledger.store.get_value("collected:DEAL-1") is None
    fake.pay("in_1")
    h.clock.advance(seconds=60)
    fx = h.sweep("poll 2")
    paid = [e.text for e in fx if isinstance(e, core.Post) and "paid by Contoso Ltd" in e.text]
    assert paid == [
        "**DEAL-1** paid by Contoso Ltd · 10,200.00 USD received on invoice **in_1** · receipt "
        f"booked to 2026-11 (cash up, receivables down) at **{h.clock.now:%H:%M:%S} UTC**."
    ]
    assert len(h.books.journal_by_source("receipt:D-2026-01")) == 1
    assert h.ledger.store.get_value("collected:DEAL-1") is not None
    # polled no more
    h.clock.advance(seconds=60)
    fx = h.sweep("poll 3")
    assert not [e for e in fx if isinstance(e, core.PollInvoice)]
