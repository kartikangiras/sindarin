"""The payment rails: the bank file always, Stripe when a key is given; the ledger's send and
confirm steps; the `paid` command."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from sindarin.executor import commands as cmd
from sindarin.ledger import Ledger, LedgerError
from sindarin.payments import BankFileRail, PaymentLine, RailError, StripeRail, build_rail

SAMPLE = Path(__file__).resolve().parents[2] / "data" / "acme" / "2026-10"
LINES = [
    PaymentLine("INV-1042", "Amazon Web Services", 480000, "USD", "2026-11-05"),
    PaymentLine("INV-1043", "Figma", 145000, "USD", "2026-11-10"),
]


def test_the_bank_file_is_a_batch_the_portal_takes(tmp_path):
    rail = BankFileRail(tmp_path / "payments")
    out = rail.send("PAY-1", LINES)
    assert out == {
        "INV-1042": rail.status("PAY-1.csv").__class__("PAY-1.csv", "sent"),
        "INV-1043": out["INV-1043"],
    }
    text = (tmp_path / "payments" / "PAY-1.csv").read_text()
    assert text.splitlines()[0] == "reference,beneficiary,amount,currency,invoice,due,memo"
    assert "PAY-1/INV-1042,Amazon Web Services,4800.00,USD,INV-1042,2026-11-05," in text
    name, doc = rail.document("PAY-1", LINES)
    assert name == "PAY-1.csv" and doc == text and rail.confirms is False


class _Stripe:
    """A fake Stripe: payouts need balance; a test charge adds it; reads return what was made."""

    def __init__(self, *, balance: int = 0, status: str = "pending", bank: bool = True) -> None:
        self.balance = balance
        self.status = status
        self.bank = bank  # whether the account has an external account in usd
        self.payouts: dict[str, dict] = {}
        self.keys: list[str] = []
        self.charges: list[dict] = []
        self.external: list[dict] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        assert req.headers["authorization"].startswith("Basic ")
        form = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
        if req.method == "POST" and req.url.path == "/v1/charges":
            self.charges.append(form)
            self.balance += int(form["amount"])
            return httpx.Response(200, json={"id": "ch_1", "status": "succeeded"})
        if req.method == "GET" and req.url.path == "/v1/account":
            return httpx.Response(200, json={"id": "acct_test"})
        if req.method == "POST" and req.url.path == "/v1/accounts/acct_test/external_accounts":
            self.external.append(form)
            self.bank = True
            return httpx.Response(200, json={"id": "ba_1", "object": "bank_account"})
        if req.method == "POST" and req.url.path == "/v1/payouts":
            self.keys.append(req.headers.get("idempotency-key", ""))
            if not self.bank:
                return httpx.Response(
                    400,
                    json={
                        "error": {
                            "type": "invalid_request_error",
                            "message": "Sorry, you don't have any external accounts in that currency (usd).",
                        }
                    },
                )
            if int(form["amount"]) > self.balance:
                return httpx.Response(
                    400,
                    json={
                        "error": {
                            "type": "invalid_request_error",
                            "code": "balance_insufficient",
                            "message": "You have insufficient funds in your Stripe account.",
                        }
                    },
                )
            self.balance -= int(form["amount"])
            pid = f"po_{len(self.payouts) + 1}"
            self.payouts[pid] = {
                "id": pid,
                "object": "payout",
                "amount": int(form["amount"]),
                "currency": form["currency"],
                "status": self.status,
                "metadata": {
                    "run": form["metadata[run]"],
                    "invoice": form["metadata[invoice]"],
                    "vendor": form["metadata[vendor]"],
                },
            }
            return httpx.Response(200, json=self.payouts[pid])
        if req.method == "GET" and req.url.path.startswith("/v1/payouts/"):
            pid = req.url.path.rsplit("/", 1)[1]
            if pid in self.payouts:
                return httpx.Response(200, json=self.payouts[pid])
            return httpx.Response(
                404, json={"error": {"code": "resource_missing", "message": "No such payout"}}
            )
        return httpx.Response(500, json={"error": {"message": "unexpected"}})


def test_stripe_creates_one_payout_per_payable_with_an_idempotency_key():
    fake = _Stripe(balance=10_000_00)
    rail = StripeRail("sk_test_x", transport=httpx.MockTransport(fake))
    out = rail.send("PAY-1", LINES)
    assert [t.status for t in out.values()] == ["sent", "sent"]
    assert out["INV-1042"].reference == "po_1" and out["INV-1043"].reference == "po_2"
    assert fake.keys == ["sindarin-PAY-1-INV-1042", "sindarin-PAY-1-INV-1043"]
    assert fake.payouts["po_1"]["metadata"] == {
        "run": "PAY-1",
        "invoice": "INV-1042",
        "vendor": "Amazon Web Services",
    }
    assert rail.status("po_1").status == "sent" and rail.confirms is True
    fake.payouts["po_1"]["status"] = "paid"
    assert rail.status("po_1").status == "paid"
    fake.payouts["po_2"]["status"] = "failed"
    fake.payouts["po_2"]["failure_message"] = "account closed"
    assert rail.status("po_2") == rail.status("po_2").__class__("po_2", "failed", "account closed")


def test_stripe_tops_up_an_empty_test_balance_and_reads_every_payout_back():
    fake = _Stripe(balance=0, status="paid")
    rail = StripeRail("sk_test_x", transport=httpx.MockTransport(fake))
    out = rail.send("PAY-1", LINES)
    assert [t.status for t in out.values()] == ["paid", "paid"]
    assert len(fake.charges) == 1 and fake.charges[0]["source"] == "tok_bypassPending"
    assert int(fake.charges[0]["amount"]) == 480000 + 145000
    assert fake.keys[:2] == ["sindarin-PAY-1-INV-1042", "sindarin-PAY-1-INV-1042-r"]
    # a live key never tops up: the refusal is reported, not papered over
    fake = _Stripe(balance=0)
    rail = StripeRail("sk_live_x", transport=httpx.MockTransport(fake))
    out = rail.send("PAY-1", LINES[:1])
    assert out["INV-1042"].status == "failed" and "balance_insufficient" in out["INV-1042"].detail
    assert fake.charges == []


def test_stripe_without_a_bank_account_says_where_to_add_one():
    fake = _Stripe(balance=10_000_00, bank=False)
    rail = StripeRail("sk_test_x", transport=httpx.MockTransport(fake))
    out = rail.send("PAY-1", LINES[:1])
    assert out["INV-1042"].status == "failed"
    assert "Bank accounts and currencies" in out["INV-1042"].detail
    assert fake.external == [] and fake.charges == []  # the API cannot add one with a test key


def test_stripe_read_back_mismatch_is_a_failure():
    fake = _Stripe(balance=10_000_00)

    def tampered(req: httpx.Request) -> httpx.Response:
        r = fake(req)
        if req.method == "GET":
            body = json.loads(r.content)
            body["amount"] = 1
            return httpx.Response(200, json=body)
        return r

    rail = StripeRail("sk_test_x", transport=httpx.MockTransport(tampered))
    out = rail.send("PAY-1", LINES[:1])
    assert out["INV-1042"].status == "failed" and "read-back" in out["INV-1042"].detail


def test_build_rail_needs_a_key_for_stripe(tmp_path):
    assert build_rail("file", folder=tmp_path).name == "bank file"
    with pytest.raises(RailError):
        build_rail("stripe", folder=tmp_path)
    assert build_rail("stripe", folder=tmp_path, stripe_key="sk_test_1").name == "Stripe"


def test_ledger_send_then_confirm_books_once(tmp_path):
    books = Ledger(tmp_path / "books.db")
    books.import_dir(SAMPLE)
    assert books.send("PAY-1", {"INV-1042": "po_1", "INV-1043": "po_2"}, "Stripe") == 2
    rows = {r["invoice"]: r for r in books.payables(["INV-1042", "INV-1043"])}
    assert rows["INV-1042"]["status"] == "sent" and rows["INV-1042"]["reference"] == "po_1"
    assert rows["INV-1042"]["rail"] == "Stripe" and books.journal_by_source("PAY-1") == []
    assert books.send("PAY-1", {"INV-1042": "po_1"}, "Stripe") == 0  # idempotent on the run
    with pytest.raises(LedgerError, match="already taken by another run"):
        books.send("PAY-2", {"INV-1042": "x"}, "Stripe")
    with pytest.raises(LedgerError, match="nothing sent under PAY-1"):
        books.confirm_paid("PAY-1", invoices=["INV-1044"])  # INV-1044 is not in this run
    assert books.confirm_paid("PAY-1", invoices=["INV-1042"]) == 1
    assert books.confirm_paid("PAY-1", reference="BANK-1") == 1  # the rest; the first untouched
    rows = {r["invoice"]: r for r in books.payables(["INV-1042", "INV-1043"])}
    assert rows["INV-1042"]["reference"] == "po_1" and rows["INV-1043"]["reference"] == "BANK-1"
    assert len(books.journal_by_source("PAY-1")) == 2
    assert books.confirm_paid("PAY-1") == 0
    with pytest.raises(LedgerError, match="nothing sent under PAY-9"):
        books.confirm_paid("PAY-9")
    books.close()


def test_an_older_books_file_gains_the_rail_columns(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE payables(invoice TEXT PRIMARY KEY, vendor TEXT NOT NULL, amount_cents "
        "INTEGER NOT NULL, due TEXT NOT NULL, period TEXT NOT NULL, status TEXT NOT NULL "
        "DEFAULT 'open', run_id TEXT, paid_at TEXT)"
    )
    db.execute(
        "INSERT INTO payables(invoice, vendor, amount_cents, due, period) VALUES "
        "('INV-1', 'Acme', 100, '2026-10-05', '2026-10')"
    )
    db.commit()
    db.close()
    books = Ledger(path)
    assert books.payables(["INV-1"])[0]["reference"] is None
    books.close()


def test_the_paid_command_parses_and_the_help_names_it():
    assert cmd.parse_command("@sindarin-ledger paid PAY-1 BANK-7781") == cmd.Paid(
        "PAY-1", "BANK-7781"
    )
    assert cmd.parse_command("paid pay-2 ref/2026-11-08.") == cmd.Paid("PAY-2", "ref/2026-11-08")
    assert cmd.parse_command("paid PAY-1") is None
    assert cmd.parse_command("paid BUDGET-1 x") is None
    assert "`paid <PAY-n> <bank reference>`" in cmd.HELP_TEXT


# -- Increase ---------------------------------------------------------------------------------


class _Increase:
    """A fake Increase sandbox: an account opened and funded on demand, external accounts per
    vendor, ACH transfers that settle when the simulation says so."""

    def __init__(self) -> None:
        self.balance = 0
        self.accounts: list[dict] = []
        self.entities: list[dict] = []
        self.numbers: list[dict] = []
        self.external: dict[str, dict] = {}
        self.transfers: dict[str, dict] = {}
        self.keys: list[str] = []
        self.funded: list[int] = []
        self.calls: list[str] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(f"{req.method} {req.url.path}")
        if req.url.path == "/groups":
            return httpx.Response(200, json={"sandbox_api_key": "sk_sandbox_fake"})
        assert req.headers["authorization"] == "Bearer sk_sandbox_fake"
        body = json.loads(req.content) if req.content else {}
        if req.headers.get("idempotency-key"):
            self.keys.append(req.headers["idempotency-key"])
        p, m = req.url.path, req.method
        if m == "POST" and p == "/entities":
            ent = {"id": f"entity_{len(self.entities) + 1}", "structure": body["structure"]}
            self.entities.append(ent)
            return httpx.Response(200, json=ent)
        if m == "POST" and p == "/accounts":
            if not body.get("entity_id"):
                return httpx.Response(
                    400,
                    json={
                        "status": 400,
                        "type": "invalid_operation_error",
                        "title": "The action you specified can't be performed on the object in its current state.",
                        "detail": "No entity_id provided or default entity available.",
                    },
                )
            acc = {
                "id": f"account_{len(self.accounts) + 1}",
                "name": body["name"],
                "entity_id": body["entity_id"],
            }
            self.accounts.append(acc)
            return httpx.Response(200, json=acc)
        if m == "POST" and p == "/account_numbers":
            num = {
                "id": f"account_number_{len(self.numbers) + 1}",
                "account_id": body["account_id"],
            }
            self.numbers.append(num)
            return httpx.Response(200, json=num)
        if m == "GET" and p.endswith("/balance"):
            return httpx.Response(
                200, json={"available_balance": self.balance, "current_balance": self.balance}
            )
        if m == "POST" and p == "/simulations/inbound_ach_transfers":
            self.balance += int(body["amount"])
            self.funded.append(int(body["amount"]))
            return httpx.Response(200, json={"id": "inbound_ach_transfer_1", "status": "accepted"})
        if m == "POST" and p == "/external_accounts":
            ext = {"id": f"external_account_{len(self.external) + 1}", **body}
            self.external[ext["id"]] = ext
            return httpx.Response(200, json=ext)
        if m == "POST" and p == "/ach_transfers":
            if int(body["amount"]) > self.balance:
                return httpx.Response(
                    400,
                    json={
                        "status": 400,
                        "type": "insufficient_funds_error",
                        "title": "Insufficient funds",
                        "detail": "The account does not have enough available balance.",
                    },
                )
            self.balance -= int(body["amount"])
            tid = f"ach_transfer_{len(self.transfers) + 1}"
            self.transfers[tid] = {
                "id": tid,
                "status": "pending_submission",
                "settlement": None,
                "return": None,
                **body,
            }
            return httpx.Response(200, json=self.transfers[tid])
        if m == "GET" and p.startswith("/ach_transfers/"):
            return httpx.Response(200, json=self.transfers[p.rsplit("/", 1)[1]])
        if m == "POST" and p.startswith("/simulations/ach_transfers/"):
            tid, action = p.split("/")[3], p.split("/")[4]
            t = self.transfers[tid]
            if action == "settle":
                t["status"], t["settlement"] = "submitted", {"settled_at": "2026-11-08T00:00:00Z"}
            elif action == "return":
                t["status"], t["return"] = "returned", {"return_reason_code": "no_account"}
            return httpx.Response(200, json=t)
        return httpx.Response(404, json={"status": 404, "title": "not found"})


def test_increase_opens_funds_pays_and_reads_back(tmp_path):
    from sindarin.payments import IncreaseRail, provision_increase_sandbox

    fake = _Increase()
    transport = httpx.MockTransport(fake)
    assert provision_increase_sandbox(transport=transport) == "sk_sandbox_fake"
    rail = IncreaseRail(
        "sk_sandbox_fake", state_path=tmp_path / "increase.json", transport=transport
    )
    out = rail.send("PAY-1", LINES)
    assert [t.status for t in out.values()] == ["paid", "paid"]
    assert out["INV-1042"].reference == "ach_transfer_1" and rail.confirms is True
    # one demo entity, one operating account under it, funded once with exactly the shortfall,
    # one external account per vendor
    assert len(fake.entities) == 1 and fake.accounts[0]["entity_id"] == "entity_1"
    assert len(fake.accounts) == 1 and fake.funded == [480000 + 145000]
    assert {e["description"] for e in fake.external.values()} == {"Amazon Web Services", "Figma"}
    assert all(e["routing_number"] == "101050001" for e in fake.external.values())
    assert "sindarin-PAY-1-INV-1042" in fake.keys and "sindarin-PAY-1-INV-1043" in fake.keys
    assert fake.transfers["ach_transfer_1"]["statement_descriptor"] == "Sindarin PAY-1 INV-1042"
    assert rail.balance() == 0
    # the state is remembered, so a second rail reuses the account and the vendors
    again = IncreaseRail(
        "sk_sandbox_fake", state_path=tmp_path / "increase.json", transport=transport
    )
    assert (
        again.ensure_account() == "account_1"
        and again.external_account("Figma") == "external_account_2"
    )
    assert len(fake.accounts) == 1 and len(fake.external) == 2
    # a returned transfer reads as failed with the reason
    fake.transfers["ach_transfer_1"].update(
        status="returned", **{"return": {"return_reason_code": "no_account"}}
    )
    assert rail.status("ach_transfer_1").status == "failed"
    assert rail.status("ach_transfer_1").detail == "no_account"


def test_increase_in_production_refuses_a_short_balance_and_unknown_vendors(tmp_path):
    from sindarin.payments import IncreaseRail

    fake = _Increase()
    rail = IncreaseRail(
        "sk_sandbox_fake",
        state_path=tmp_path / "increase.json",
        sandbox=False,
        vendor_banks={"Figma": "021000021:123456789"},
        transport=httpx.MockTransport(fake),
    )
    out = rail.send("PAY-1", LINES)
    assert all(t.status == "failed" for t in out.values())
    assert "no entity on file" in out["INV-1042"].detail and fake.funded == []
    rail.entity_id = "entity_verified"
    out = rail.send("PAY-1", LINES)
    assert "short of 6,250.00" in out["INV-1042"].detail and fake.funded == []
    assert fake.accounts[0]["entity_id"] == "entity_verified" and fake.entities == []
    fake.balance = 10_000_00
    out = rail.send("PAY-1", LINES)
    assert out["INV-1043"].status == "sent"  # Figma is on file; production settles on its own
    assert fake.external["external_account_1"]["routing_number"] == "021000021"
    assert (
        out["INV-1042"].status == "failed" and "no bank account on file" in out["INV-1042"].detail
    )
    assert "simulations" not in " ".join(fake.calls)


def test_the_books_id_namespaces_the_keys_and_changes_on_reset(tmp_path):
    from sindarin.adapters.ledger import LedgerAdapter

    books = Ledger(tmp_path / "books.db")
    books.import_dir(SAMPLE)
    first = books.books_id()
    assert len(first) == 10 and books.books_id() == first
    fake = _Stripe(balance=10_000_00, status="paid")
    rail = StripeRail("sk_test_x", transport=httpx.MockTransport(fake))
    LedgerAdapter(books, rail=rail)
    assert rail.key_ns == first
    rail.send("PAY-1", LINES[:1])
    assert fake.keys == [f"sindarin-{first}-PAY-1-INV-1042"]
    books.reset(SAMPLE)
    assert books.books_id() != first
    books.close()
