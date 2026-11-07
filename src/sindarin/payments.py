"""Payment rails: how an approved payment run leaves the books and reaches a vendor.

The ledger executor holds one rail. `BankFileRail` writes the run as a CSV batch for the bank
portal and waits for a person to confirm with `paid PAY-n <bank reference>`. `StripeRail` creates
one payout per payable through Stripe's API with an idempotency key per invoice, reads each
payout back before anything is marked sent, and is polled until Stripe says paid. Neither rail
decides anything: the gate decided, the rail moves money and reports what it saw.
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx


class RailError(Exception):
    """A rail failure. The text is shown in a room; it must hold no secret."""


@dataclass(frozen=True)
class PaymentLine:
    invoice: str
    vendor: str
    amount_cents: int
    currency: str = "USD"
    due: str = ""


@dataclass(frozen=True)
class Transfer:
    """What the rail reports for one payable: its reference on the rail and where it stands."""

    reference: str
    status: str  # sent | paid | failed
    detail: str = ""


class PaymentRail(Protocol):
    name: str
    confirms: bool  # True: the executor polls `status`; False: a person confirms in chat

    def send(self, run_id: str, lines: Iterable[PaymentLine]) -> dict[str, Transfer]:
        """Move the money (or hand it to the bank). Idempotent on run_id and invoice."""

    def status(self, reference: str) -> Transfer:
        """Where a transfer stands now, read from the rail."""

    def document(self, run_id: str, lines: Iterable[PaymentLine]) -> tuple[str, str] | None:
        """A file for the room, (filename, text), when the rail produces one."""


# -- the bank file --------------------------------------------------------------------------------


def bank_csv(run_id: str, lines: Iterable[PaymentLine]) -> str:
    """The batch as a bank portal takes it: one payment per row, the run id as the reference."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["reference", "beneficiary", "amount", "currency", "invoice", "due", "memo"])
    for x in lines:
        w.writerow(
            [
                f"{run_id}/{x.invoice}",
                x.vendor,
                f"{x.amount_cents / 100:.2f}",
                x.currency,
                x.invoice,
                x.due,
                f"Sindarin {run_id} {x.invoice}",
            ]
        )
    return buf.getvalue()


class BankFileRail:
    """The payment file the treasury uploads to the bank; the bank's confirmation comes back
    through a person typing `paid PAY-n <reference>`."""

    name = "bank file"
    confirms = False

    def __init__(self, folder: str | Path) -> None:
        self.folder = Path(folder)

    def send(self, run_id: str, lines: Iterable[PaymentLine]) -> dict[str, Transfer]:
        rows = list(lines)
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / f"{run_id}.csv"
        path.write_text(bank_csv(run_id, rows), encoding="utf-8")
        return {x.invoice: Transfer(f"{run_id}.csv", "sent") for x in rows}

    def status(self, reference: str) -> Transfer:
        return Transfer(reference, "sent", "awaiting the bank's confirmation")

    def document(self, run_id: str, lines: Iterable[PaymentLine]) -> tuple[str, str] | None:
        return f"{run_id}.csv", bank_csv(run_id, list(lines))


# -- Stripe ---------------------------------------------------------------------------------------

STRIPE_API = "https://api.stripe.com"
_STRIPE_STATUS = {
    "paid": "paid",
    "pending": "sent",
    "in_transit": "sent",
    "failed": "failed",
    "canceled": "failed",
}


BANK_HINT = (
    "no bank account on the Stripe account; in the Dashboard (test mode) open Settings → "
    "Bank accounts and currencies → Add bank account, routing 110000000, account 000123456789"
)


class StripeRail:
    """One Stripe payout per payable, from the account's balance, with the run and the invoice
    in the payout's metadata and `sindarin-<run>-<invoice>` as the idempotency key. Every
    payout is read back and its amount, currency and metadata compared before it counts as
    sent. In test mode (`sk_test_…`) an empty balance is topped up with a test charge so the
    whole loop runs without real money."""

    name = "Stripe"
    confirms = True

    def __init__(self, secret_key: str, *, transport: httpx.BaseTransport | None = None) -> None:
        if not secret_key:
            raise RailError("Stripe: no secret key")
        self.test_mode = secret_key.startswith("sk_test_")
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

    def _payout(self, run_id: str, x: PaymentLine, key: str) -> dict[str, Any]:
        return self._call(
            "POST",
            "/v1/payouts",
            data={
                "amount": x.amount_cents,
                "currency": x.currency.lower(),
                "description": f"Sindarin {run_id} {x.invoice} {x.vendor}"[:255],
                "metadata[run]": run_id,
                "metadata[invoice]": x.invoice,
                "metadata[vendor]": x.vendor[:200],
            },
            key=key,
        )

    def _top_up(self, run_id: str, amount_cents: int, currency: str) -> None:
        """Test mode only: a charge with Stripe's bypass-pending token makes funds available."""
        self._call(
            "POST",
            "/v1/charges",
            data={
                "amount": amount_cents,
                "currency": currency.lower(),
                "source": "tok_bypassPending",
                "description": f"Sindarin test balance for {run_id}",
            },
            key=f"sindarin-{run_id}-topup",
        )

    def send(self, run_id: str, lines: Iterable[PaymentLine]) -> dict[str, Transfer]:
        out: dict[str, Transfer] = {}
        rows = list(lines)
        for x in rows:
            key = f"sindarin-{run_id}-{x.invoice}"
            try:
                created = self._payout(run_id, x, key)
            except StripeRefusal as exc:
                if "external account" in exc.message:
                    out[x.invoice] = Transfer("", "failed", f"Stripe: {BANK_HINT}")
                    continue
                if exc.code != "balance_insufficient" or not self.test_mode:
                    out[x.invoice] = Transfer("", "failed", f"Stripe: {exc}")
                    continue
                self._top_up(run_id, sum(r.amount_cents for r in rows), x.currency)
                try:
                    created = self._payout(run_id, x, key + "-r")
                except StripeRefusal as again:
                    detail = BANK_HINT if "external account" in again.message else str(again)
                    out[x.invoice] = Transfer("", "failed", f"Stripe: {detail}")
                    continue
            ref = str(created.get("id") or "")
            seen = self._call("GET", f"/v1/payouts/{ref}") if ref else {}
            meta = seen.get("metadata") or {}
            if (
                int(seen.get("amount") or -1) != x.amount_cents
                or str(seen.get("currency") or "").lower() != x.currency.lower()
                or meta.get("run") != run_id
                or meta.get("invoice") != x.invoice
            ):
                out[x.invoice] = Transfer(ref, "failed", "Stripe: read-back does not match")
                continue
            out[x.invoice] = self._transfer(seen)
        return out

    def status(self, reference: str) -> Transfer:
        return self._transfer(self._call("GET", f"/v1/payouts/{reference}"))

    @staticmethod
    def _transfer(payout: Mapping[str, Any]) -> Transfer:
        raw = str(payout.get("status") or "")
        detail = str(payout.get("failure_message") or payout.get("failure_code") or "")
        return Transfer(str(payout.get("id") or ""), _STRIPE_STATUS.get(raw, "sent"), detail or raw)

    def document(self, run_id: str, lines: Iterable[PaymentLine]) -> tuple[str, str] | None:
        return None


# -- Increase -------------------------------------------------------------------------------------

INCREASE_SANDBOX = "https://sandbox.increase.com"
INCREASE_PRODUCTION = "https://api.increase.com"
_DEFAULT_RTN = "101050001"  # the routing number Increase's own examples use for a destination
_INCREASE_FAILED = {"returned", "rejected", "canceled", "requires_attention"}


def provision_increase_sandbox(*, transport: httpx.BaseTransport | None = None) -> str:
    """A fresh, empty Increase sandbox with no signup: one unauthenticated call returns the
    key, which is shown once. Sandbox keys never work in production."""
    with httpx.Client(timeout=20.0, transport=transport) as http:
        try:
            r = http.post(f"{INCREASE_SANDBOX}/groups")
        except httpx.HTTPError as exc:
            raise RailError(f"Increase unreachable: {exc.__class__.__name__}") from None
    body = r.json() if r.content else {}
    key = str(body.get("sandbox_api_key") or "")
    if r.status_code >= 400 or not key:
        raise RailError(f"Increase did not hand out a sandbox key (HTTP {r.status_code})")
    return key


class IncreaseRail:
    """One ACH transfer per payable from an operating account at Increase, to an External
    Account per vendor, with `sindarin-<run>-<invoice>` as the idempotency key. The available
    balance is read before anything is sent; the transfer is read back and its amount,
    destination and descriptor compared before it counts; the status is polled until the
    Federal Reserve settles it. In the sandbox the account is opened and funded on first use
    and settlement is simulated, so the whole loop runs with no signup and no real money."""

    name = "Increase"
    confirms = True

    def __init__(
        self,
        api_key: str,
        *,
        state_path: str | Path,
        sandbox: bool = True,
        vendor_banks: Mapping[str, str] | None = None,
        entity_id: str = "",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise RailError("Increase: no API key")
        self.sandbox = sandbox
        self.entity_id = entity_id
        self.state_path = Path(state_path)
        self.vendor_banks = dict(vendor_banks or {})  # vendor → "routing:account"
        self._http = httpx.Client(
            base_url=INCREASE_SANDBOX if sandbox else INCREASE_PRODUCTION,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=20.0,
            transport=transport,
        )
        self.state: dict[str, Any] = {"external_accounts": {}}
        if self.state_path.exists():
            with contextlib.suppress(ValueError, OSError):
                self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.state.setdefault("external_accounts", {})

    def close(self) -> None:
        self._http.close()

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self.state, indent=2, sort_keys=True), "utf-8")

    def _call(
        self, method: str, path: str, *, body: Mapping[str, Any] | None = None, key: str = ""
    ) -> dict[str, Any]:
        headers = {"Idempotency-Key": key} if key else {}
        try:
            r = self._http.request(method, path, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise RailError(f"Increase unreachable: {exc.__class__.__name__}") from None
        data: dict[str, Any] = {}
        with contextlib.suppress(ValueError):
            data = r.json()
        if r.status_code >= 400:
            what = str(data.get("title") or data.get("type") or r.status_code)
            detail = str(data.get("detail") or "")[:140]
            raise RailError(f"Increase: {what}{' — ' + detail if detail else ''}")
        return data

    # -- the operating account --------------------------------------------------------------

    def ensure_entity(self) -> str:
        """The legal entity that owns the operating account. Increase needs one before an
        account can be opened; the sandbox takes a demo corporation (the one in Increase's own
        examples), production needs the entity Increase verified, set as `increase_entity_id`."""
        if self.state.get("entity_id"):
            return str(self.state["entity_id"])
        if self.entity_id:
            self.state["entity_id"] = self.entity_id
            self._save()
            return self.entity_id
        if not self.sandbox:
            raise RailError("Increase: no entity on file; set increase_entity_id in ledger.yaml")
        address = {"line1": "33 Liberty Street", "city": "New York", "state": "NY", "zip": "10045"}
        made = self._call(
            "POST",
            "/entities",
            body={
                "structure": "corporation",
                "corporation": {
                    "name": "Acme (Sindarin sandbox)",
                    "website": "https://sindarin-nu.vercel.app",
                    "address": address,
                    "incorporation_state": "NY",
                    "legal_identifier": {
                        "category": "us_employer_identification_number",
                        "value": "602214076",
                    },
                    "beneficial_owners": [
                        {
                            "company_title": "CFO",
                            "prongs": ["control"],
                            "individual": {
                                "name": "Ian Crease",
                                "date_of_birth": "1970-01-31",
                                "address": address,
                                "identification": {
                                    "method": "social_security_number",
                                    "number": "078051120",
                                },
                            },
                        }
                    ],
                },
            },
            key="sindarin-entity",
        )
        self.state["entity_id"] = made["id"]
        self._save()
        return str(made["id"])

    def ensure_account(self) -> str:
        """The operating account the runs are paid from, opened once and remembered."""
        if self.state.get("account_id"):
            return str(self.state["account_id"])
        entity = self.ensure_entity()
        account = self._call(
            "POST",
            "/accounts",
            body={"name": "Sindarin operating", "entity_id": entity},
            key="sindarin-operating",
        )
        number = self._call(
            "POST",
            "/account_numbers",
            body={"account_id": account["id"], "name": "Sindarin operating"},
            key="sindarin-operating-number",
        )
        self.state["account_id"] = account["id"]
        self.state["account_number_id"] = number["id"]
        self._save()
        return str(account["id"])

    def balance(self) -> int:
        """The available balance of the operating account, in cents, read from Increase."""
        account = self.ensure_account()
        data = self._call("GET", f"/accounts/{account}/balance")
        return int(data.get("available_balance") or 0)

    def fund(self, amount_cents: int, *, key: str) -> None:
        """Sandbox only: an inbound ACH credit simulated into the operating account."""
        if not self.sandbox:
            raise RailError("Increase: the operating account cannot be funded from here")
        self.ensure_account()
        self._call(
            "POST",
            "/simulations/inbound_ach_transfers",
            body={"account_number_id": self.state["account_number_id"], "amount": amount_cents},
            key=key,
        )

    def external_account(self, vendor: str) -> str:
        """The vendor's bank account at Increase, created once from the vendor directory (or,
        in the sandbox, from a deterministic test account) and remembered by vendor name."""
        known = self.state["external_accounts"].get(vendor)
        if known:
            return str(known)
        routing, number = _DEFAULT_RTN, _test_account_number(vendor)
        if vendor in self.vendor_banks:
            routing, number = self.vendor_banks[vendor].split(":", 1)
        elif not self.sandbox:
            raise RailError(f"Increase: no bank account on file for {vendor}")
        created = self._call(
            "POST",
            "/external_accounts",
            body={
                "description": vendor[:200],
                "routing_number": routing,
                "account_number": number,
                "account_holder": "business",
            },
            key=f"sindarin-vendor-{_slug(vendor)}",
        )
        self.state["external_accounts"][vendor] = created["id"]
        self._save()
        return str(created["id"])

    # -- the rail -----------------------------------------------------------------------------

    def send(self, run_id: str, lines: Iterable[PaymentLine]) -> dict[str, Transfer]:
        rows = list(lines)
        out: dict[str, Transfer] = {}
        if not rows:
            return out
        total = sum(x.amount_cents for x in rows)
        try:
            account = self.ensure_account()
            available = self.balance()
            if available < total:
                if self.sandbox:
                    self.fund(total - available, key=f"sindarin-{run_id}-fund")
                else:
                    short = (
                        f"available balance {available / 100:,.2f} is short of {total / 100:,.2f}"
                    )
                    raise RailError(f"Increase: {short}")
        except RailError as exc:
            # nothing left the account: every line is reported failed with the one reason
            return {x.invoice: Transfer("", "failed", str(exc)[:160]) for x in rows}
        for x in rows:
            try:
                destination = self.external_account(x.vendor)
                created = self._call(
                    "POST",
                    "/ach_transfers",
                    body={
                        "account_id": account,
                        "external_account_id": destination,
                        "amount": x.amount_cents,
                        "statement_descriptor": f"Sindarin {run_id} {x.invoice}"[:200],
                        "require_approval": False,
                    },
                    key=f"sindarin-{run_id}-{x.invoice}",
                )
                ref = str(created.get("id") or "")
                seen = self._call("GET", f"/ach_transfers/{ref}") if ref else {}
                if (
                    int(seen.get("amount") or -1) != x.amount_cents
                    or str(seen.get("external_account_id") or "") != destination
                    or str(seen.get("statement_descriptor") or "")
                    != f"Sindarin {run_id} {x.invoice}"[:200]
                ):
                    out[x.invoice] = Transfer(ref, "failed", "Increase: read-back does not match")
                    continue
                if self.sandbox and str(seen.get("status")) in (
                    "pending_approval",
                    "pending_submission",
                    "submitted",
                ):
                    # the Federal Reserve's settlement, simulated; production settles on its own
                    with contextlib.suppress(RailError):
                        self._call(
                            "POST",
                            f"/simulations/ach_transfers/{ref}/settle",
                            body={"inbound_funds_hold_behavior": "release_immediately"},
                        )
                    seen = self._call("GET", f"/ach_transfers/{ref}")
                out[x.invoice] = self._transfer(seen)
            except RailError as exc:
                out[x.invoice] = Transfer("", "failed", str(exc)[:160])
        return out

    def status(self, reference: str) -> Transfer:
        return self._transfer(self._call("GET", f"/ach_transfers/{reference}"))

    @staticmethod
    def _transfer(obj: Mapping[str, Any]) -> Transfer:
        raw = str(obj.get("status") or "")
        ref = str(obj.get("id") or "")
        if raw == "submitted" and obj.get("settlement"):
            return Transfer(ref, "paid", "settled")
        if raw in _INCREASE_FAILED:
            back = obj.get("return") or {}
            reason = str((back or {}).get("return_reason_code") or raw)
            return Transfer(ref, "failed", reason)
        return Transfer(ref, "sent", raw)

    def document(self, run_id: str, lines: Iterable[PaymentLine]) -> tuple[str, str] | None:
        return None


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in text.lower()).strip("-")[:60]


def _test_account_number(vendor: str) -> str:
    """A stable nine-digit test account number per vendor, for the sandbox only."""
    import hashlib

    digest = hashlib.sha256(vendor.encode("utf-8")).hexdigest()
    return str(100000000 + int(digest[:12], 16) % 900000000)


class StripeRefusal(RailError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message


def build_rail(
    kind: str,
    *,
    folder: str | Path,
    stripe_key: str = "",
    increase_key: str = "",
    sandbox: bool = True,
    vendor_banks: Mapping[str, str] | None = None,
    entity_id: str = "",
) -> PaymentRail:
    """The rail the ledger executor runs: `stripe` or `increase` with a key, else the bank file."""
    if kind == "stripe":
        if not stripe_key:
            raise RailError("payment_rail is stripe but STRIPE_SECRET_KEY is empty")
        return StripeRail(stripe_key)
    if kind == "increase":
        if not increase_key:
            raise RailError("payment_rail is increase but INCREASE_API_KEY is empty")
        return IncreaseRail(
            increase_key,
            state_path=Path(folder) / "increase.json",
            sandbox=sandbox,
            vendor_banks=vendor_banks,
            entity_id=entity_id,
        )
    return BankFileRail(folder)
