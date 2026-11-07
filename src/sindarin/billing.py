"""Billing: a won deal becomes a real invoice the customer can pay.

`StripeBilling` finds or creates the customer, adds the deal as an invoice item, creates and
finalizes the invoice with the deal id in its metadata, and reads it back before the room is
told. The ledger executor then polls the invoice until Stripe says paid and books the receipt.
In test mode a test card pays the hosted invoice page, so the whole loop runs without real
money. Nothing here decides anything: the gate decided, this sends the bill.
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from typing import Any

import httpx

from sindarin.payments import STRIPE_API, RailError, StripeRefusal

DUE_DAYS = 30


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in text.lower()).strip("-")[:60]


class StripeBilling:
    name = "Stripe"

    def __init__(
        self,
        secret_key: str,
        *,
        customer_email: str = "",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not secret_key:
            raise RailError("Stripe: no secret key")
        self.test_mode = secret_key.startswith("sk_test_")
        # Stripe sends an invoice only to a customer with an email; the billing contact for
        # every account in the demo, or, in test mode, an address made from the account's name
        self.customer_email = customer_email
        self._http = httpx.Client(
            base_url=STRIPE_API, auth=(secret_key, ""), timeout=20.0, transport=transport
        )

    def close(self) -> None:
        self._http.close()

    def _call(
        self, method: str, path: str, *, data: Mapping[str, Any] | None = None, key: str = ""
    ) -> dict[str, Any]:
        headers = {"Idempotency-Key": key} if key else {}
        try:
            r = self._http.request(method, path, data=data, headers=headers)
        except httpx.HTTPError as exc:
            raise RailError(f"Stripe unreachable: {exc.__class__.__name__}") from None
        body: dict[str, Any] = {}
        with contextlib.suppress(ValueError):
            body = r.json()
        if r.status_code >= 400:
            err = body.get("error") or {}
            code = str(err.get("code") or err.get("type") or r.status_code)
            raise StripeRefusal(code, str(err.get("message") or "")[:160])
        return body

    def email_for(self, account: str) -> str:
        if self.customer_email:
            return self.customer_email
        if self.test_mode:
            return f"billing+{_slug(account)}@example.com"
        raise RailError(f"Stripe: no billing email for {account}; set BILLING_CUSTOMER_EMAIL")

    def customer(self, account: str) -> str:
        """The customer for an account, created once (the idempotency key is the account)."""
        made = self._call(
            "POST",
            "/v1/customers",
            data={
                "name": account[:256],
                "email": self.email_for(account),
                "metadata[account]": account[:200],
            },
            key=f"sindarin-customer-{_slug(account)}-v2",
        )
        return str(made["id"])

    def invoice(
        self,
        deal_id: str,
        *,
        account: str,
        product: str,
        amount_cents: int,
        currency: str,
        period: str,
    ) -> dict[str, Any]:
        """One finalized invoice for the deal, read back before it is reported."""
        customer = self.customer(account)
        # the invoice first, then the line attached to it by id: newer API versions no longer
        # sweep a customer's pending items into a new invoice on their own
        created = self._call(
            "POST",
            "/v1/invoices",
            data={
                "customer": customer,
                "collection_method": "send_invoice",
                "days_until_due": DUE_DAYS,
                "description": f"Sindarin {deal_id} · {product}"[:500],
                "metadata[deal]": deal_id,
                "metadata[period]": period,
                "pending_invoice_items_behavior": "exclude",
            },
            key=f"sindarin-{deal_id}-invoice-v2",
        )
        self._call(
            "POST",
            "/v1/invoiceitems",
            data={
                "customer": customer,
                "invoice": str(created["id"]),
                "amount": amount_cents,
                "currency": currency.lower(),
                "description": f"{product} · {deal_id}"[:500],
                "metadata[deal]": deal_id,
            },
            key=f"sindarin-{deal_id}-item-v2",
        )
        invoice_id = str(created["id"])
        final = self._call(
            "POST", f"/v1/invoices/{invoice_id}/finalize", key=f"sindarin-{deal_id}-finalize-v2"
        )
        seen = self._call("GET", f"/v1/invoices/{invoice_id}")
        meta = seen.get("metadata") or {}
        if int(seen.get("amount_due") or -1) != amount_cents or meta.get("deal") != deal_id:
            raise RailError(f"Stripe: invoice {invoice_id} read-back does not match the deal")
        return {
            "invoice_id": invoice_id,
            "invoice_url": str(
                seen.get("hosted_invoice_url") or final.get("hosted_invoice_url") or ""
            ),
            "invoice_status": str(seen.get("status") or "open"),
            "customer_id": customer,
            "due_days": DUE_DAYS,
        }

    def status(self, invoice_id: str) -> dict[str, Any]:
        seen = self._call("GET", f"/v1/invoices/{invoice_id}")
        return {
            "status": str(seen.get("status") or ""),
            "amount_paid_cents": int(seen.get("amount_paid") or 0),
            "paid": bool(seen.get("paid")) or str(seen.get("status")) == "paid",
        }
