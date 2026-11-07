"""Dropbox Sign: the order form goes out for signature when the deal is approved.

The ledger executor renders a one-page order form from the approved deal, sends it to the
customer's signer with the deal id in the request's metadata, and polls the request until it
is signed. Test mode is one flag: requests are watermarked and never binding.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from typing import Any

import httpx

from sindarin.connect import ConnectorError, call
from sindarin.schema import money, percent

DROPBOX_SIGN_API = "https://api.hellosign.com/v3"


def order_form_pdf(
    deal_id: str,
    *,
    account: str,
    product: str,
    price_cents: int,
    list_cents: int,
    discount_bps: int,
    currency: str,
    period: str,
    terms: str = "standard",
) -> bytes:
    """A one-page order form drawn with matplotlib, so no new dependency is needed."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig = plt.figure(figsize=(8.27, 11.69))
    fig.patch.set_facecolor("white")
    y = 0.92
    fig.text(0.1, y, f"Order form {deal_id}", fontsize=22, fontweight="bold")
    y -= 0.04
    fig.text(
        0.1,
        y,
        f"Prepared {datetime.now(tz=UTC):%Y-%m-%d} · period {period}",
        fontsize=10,
        color="#555",
    )
    y -= 0.07
    rows = [
        ("Customer", account),
        ("Product", product),
        ("List price", money(list_cents, currency)),
        ("Discount", percent(discount_bps)),
        ("Price", money(price_cents, currency)),
        ("Terms", terms),
    ]
    for label, value in rows:
        fig.text(0.1, y, label, fontsize=12, color="#555")
        fig.text(0.35, y, value, fontsize=12, fontweight="bold")
        y -= 0.04
    y -= 0.04
    fig.text(
        0.1,
        y,
        "Approved in Sindarin by the finance team; the price above is the price on the books.",
        fontsize=10,
        color="#555",
    )
    y -= 0.12
    fig.text(0.1, y, "Signature: ______________________________", fontsize=12)
    y -= 0.04
    fig.text(0.1, y, "Name and date: __________________________", fontsize=12)
    buf = io.BytesIO()
    fig.savefig(buf, format="pdf")
    plt.close(fig)
    return buf.getvalue()


class DropboxSign:
    name = "Dropbox Sign"

    def __init__(
        self,
        api_key: str,
        *,
        signer_email: str,
        signer_name: str = "",
        test_mode: bool = True,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ConnectorError("Dropbox Sign: no API key")
        if not signer_email:
            raise ConnectorError("Dropbox Sign: no signer email")
        self.signer_email = signer_email
        self.signer_name = signer_name
        self.test_mode = test_mode
        self._http = httpx.Client(
            base_url=DROPBOX_SIGN_API, auth=(api_key, ""), timeout=30.0, transport=transport
        )

    def close(self) -> None:
        self._http.close()

    def _call(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        return call(self._http, method, path, who="Dropbox Sign", **kw)

    def send_order_form(self, deal_id: str, *, account: str, pdf: bytes) -> dict[str, Any]:
        """One signature request for the order form; the deal id rides in its metadata."""
        sent = self._call(
            "POST",
            "/signature_request/send",
            data={
                "title": f"Order form {deal_id} · {account}"[:255],
                "subject": f"{account}: order form {deal_id} for signature"[:255],
                "message": "The price on this order form is the price approved on the books.",
                "signers[0][email_address]": self.signer_email,
                "signers[0][name]": self.signer_name or account[:100],
                "test_mode": "1" if self.test_mode else "0",
                "metadata[deal]": deal_id,
            },
            files={"file[0]": (f"{deal_id}-order-form.pdf", pdf, "application/pdf")},
        )
        req = sent.get("signature_request") or {}
        request_id = str(req.get("signature_request_id") or "")
        if not request_id:
            raise ConnectorError("Dropbox Sign: no signature request id in the reply")
        signers = req.get("signatures") or []
        signer = str(
            (signers[0] if signers else {}).get("signer_email_address") or self.signer_email
        )
        return {
            "signature_request_id": request_id,
            "signer": signer,
            "test_mode": self.test_mode,
            "signing_url": str(req.get("signing_url") or ""),
        }

    def status(self, request_id: str) -> dict[str, Any]:
        seen = self._call("GET", f"/signature_request/{request_id}")
        req = seen.get("signature_request") or {}
        signatures = req.get("signatures") or []
        signed = [s for s in signatures if str(s.get("status_code")) == "signed"]
        when = ""
        if signed and signed[0].get("signed_at"):
            when = datetime.fromtimestamp(int(signed[0]["signed_at"]), tz=UTC).strftime(
                "%Y-%m-%d %H:%M UTC"
            )
        return {
            "complete": bool(req.get("is_complete")),
            "signed_by": str((signed[0] if signed else {}).get("signer_email_address") or ""),
            "signed_at": when,
            "declined": bool(req.get("is_declined")),
        }
