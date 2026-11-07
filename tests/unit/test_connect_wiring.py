"""The connectors through the executors: a proposed deal reaches HubSpot, the CFO's yes closes
it won and sends the order form, the signature comes back; a denied deal closes lost; a locked
month gets its board pack, and `pack` builds one on demand."""

from __future__ import annotations

from validation.harness import FINANCE, JOHN, Harness

from sindarin.adapters.base import AdapterError
from sindarin.executor import core
from sindarin.ledger import Ledger
from sindarin.names import SWITCH_EXECUTOR


class _Crm:
    name = "HubSpot"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    def upsert_deal(self, deal_id, *, account, product, amount_cents, stage, known_id=None):
        self.calls.append((deal_id, stage, known_id))
        return {
            "crm_deal_id": "900",
            "crm_stage": {
                "open": "decisionmakerboughtin",
                "won": "closedwon",
                "lost": "closedlost",
            }[stage],
            "crm_url": "https://app.hubspot.com/contacts/4242/record/0-3/900",
        }


class _Esign:
    name = "Dropbox Sign"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, int]] = []
        self.complete = False
        self.declined = False

    def send_order_form(self, deal_id, *, account, pdf):
        self.sent.append((deal_id, account, len(pdf)))
        return {"signature_request_id": "sr_1", "signer": "cfo@contoso.example", "test_mode": True}

    def status(self, request_id):
        return {
            "complete": self.complete,
            "signed_by": "cfo@contoso.example" if self.complete else "",
            "signed_at": "2026-11-08 12:00 UTC" if self.complete else "",
            "declined": self.declined,
        }


class _Pack:
    def __init__(self) -> None:
        self.built: list[tuple[str, int, int]] = []

    def board_pack(self, period, label, *, report_lines, charts, exports, report_text=""):
        self.built.append((period, len(charts), len(exports)))
        return {
            "folder_id": "f1",
            "folder_link": "https://drive.google.com/drive/folders/f1",
            "deck_id": "d1",
            "deck_link": "https://docs.google.com/presentation/d/d1/edit",
            "files": len(charts) + len(exports) + 1,
            "slides": len(charts) + 2,
        }


def _texts(fx):
    return [e.text for e in fx if isinstance(e, core.Post)]


def test_a_deal_reaches_the_crm_and_goes_out_for_signature(harness: Harness):
    h = harness
    crm, esign = _Crm(), _Esign()
    h.ledger.adapters["ledger"].crm = crm
    h.ledger.adapters["ledger"].esign = esign
    fx = h.propose_deal()
    assert (
        "**DEAL-1** tracked in HubSpot at decisionmakerboughtin · https://app.hubspot.com/contacts/4242/record/0-3/900"
        in _texts(fx)
    )
    assert crm.calls == [("D-2026-01", "open", None)]
    fx = h.approve_deal(sender=JOHN)
    texts = _texts(fx)
    assert any("HubSpot deal closed won at 10,200.00 USD" in t for t in texts)
    assert any(
        t.startswith(
            "**DEAL-1** order form sent to cfo@contoso.example for signature via Dropbox Sign (test mode) · request **sr_1**"
        )
        for t in texts
    )
    assert crm.calls[-1] == ("D-2026-01", "won", "900") and esign.sent[0][:2] == (
        "D-2026-01",
        "Contoso Ltd",
    )
    assert esign.sent[0][2] > 1000  # a real PDF went out
    # the signature is polled until it is signed, then announced once
    assert not [t for t in _texts(h.sweep("poll 1")) if "signed by" in t]
    esign.complete = True
    h.clock.advance(seconds=60)
    texts = _texts(h.sweep("poll 2"))
    assert texts == [
        "**DEAL-1** order form signed by cfo@contoso.example at 2026-11-08 12:00 UTC · request **sr_1**."
    ]
    h.clock.advance(seconds=60)
    assert not [e for e in h.sweep("poll 3") if isinstance(e, core.PollSignature)]
    assert "contract_signed" in h.audit_events() and "crm_synced" in h.audit_events()


def test_a_denied_deal_closes_lost_and_a_crm_failure_never_touches_the_books(harness: Harness):
    h = harness
    crm = _Crm()
    h.ledger.adapters["ledger"].crm = crm
    h.propose_deal()
    fx = h.command(
        "m-deny", "deny DEAL-1 too steep", sender=JOHN, room=h.deal().room_id, thread="m-deal-1"
    )
    assert any("HubSpot deal closed lost" in t for t in _texts(fx))
    assert crm.calls[-1] == ("D-2026-01", "lost", "900") and h.won() == []

    class _Broken(_Crm):
        def upsert_deal(self, *a, **kw):
            raise AdapterError("HubSpot: HTTP 401")

    h.ledger.adapters["ledger"].crm = _Broken()
    h.propose_deal(mid="m-deal-2", deal_id="D-2026-02")
    fx = h.approve_deal("m-approve-2", sender=JOHN, aid="DEAL-2", thread="m-deal-2")
    texts = _texts(fx)
    assert any(t.startswith("**DEAL-2** approved by John · won") for t in texts)
    assert "**DEAL-2** CRM not updated — HubSpot: HTTP 401." in texts
    assert "D-2026-02" in h.won()


def test_the_board_pack_on_demand_and_when_the_month_locks(harness: Harness):
    h = harness
    h.switch.ledger = Ledger(h.books.path, readonly=True)
    fx = h.command("p0", "pack 2026-10", sender=JOHN, room=FINANCE, executor=SWITCH_EXECUTOR)
    assert _texts(fx) == [
        "No board pack destination is set: put SINDARIN_GOOGLE_SA and GOOGLE_DRIVE_FOLDER_ID in "
        ".env, then `uv run sindarin connect drive`."
    ]
    pack = _Pack()
    h.switch.pack = pack
    fx = h.command("p1", "pack 2026-10", sender=JOHN, room=FINANCE, executor=SWITCH_EXECUTOR)
    assert _texts(fx) == [
        "**October 2026 board pack** · deck https://docs.google.com/presentation/d/d1/edit · files "
        "https://drive.google.com/drive/folders/f1 · 8 files, 5 slides, from the books."
    ]
    assert pack.built == [("2026-10", 3, 4)]  # three charts, four tables with rows
    fx = h.command("p2", "pack 2027-01", sender=JOHN, room=FINANCE, executor=SWITCH_EXECUTOR)
    assert _texts(fx)[0].startswith("**pack 2027-01** — the ledger has no period 2027-01 yet")
    fx = h.command(
        "p3",
        "pack 2026-10",
        sender=JOHN,
        room=h.deal().room_id if h.deal() else "!room-planning:test",
    )
    assert _texts(fx) == ["The board pack is built by sindarin-switch: type `pack 2026-10` to it."]
    # the month locks: the pack is built once, in the business's thread
    h.run_plan_gate()
    h.run_pay_gate()
    h.propose_lock()
    h.approve_lock("m-ok")
    fx = h.sweep("close done")
    built = [
        e
        for e in fx
        if isinstance(e, core.Post) and "board pack" in e.text and e.room_id == FINANCE
    ]
    assert len(built) == 1 and built[0].text.startswith("**October 2026 board pack** · deck")
    assert pack.built[-1][0] == "2026-10" and len(pack.built) == 2
    assert not [e for e in h.sweep("again") if isinstance(e, core.BuildPack)]
    h.switch.ledger.close()
