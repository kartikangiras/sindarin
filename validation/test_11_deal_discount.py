"""Test 11 — The sales chain: a deal at a price, the discount band (design v5 §6).

The list price comes from the ledger's price book, never from the agent. Team lists at
12,000.00 USD; a deal at 10,200.00 USD is a 1,800.00 USD discount, above the 1,000.00 USD limit,
so only the cfo may say yes: Leonardo (controllers) is refused with the discount and the limit,
Vincent (sales) as not in the group, John wins it and the sale lands in November's books. A 5%
discount is the controllers' to approve. A fence that claims another list price or an unknown
product is refused before anyone is asked; a list price edited under a pending deal refuses the
stale approval with both prices and the re-proposal wins; a locked month takes no deal.
"""

from __future__ import annotations

from sindarin.executor import core
from validation.harness import BASIS, JOHN, LEONARDO, PERIOD, VINCENT, Harness, write_report


def _post(fx):
    return [e for e in fx if isinstance(e, core.Post)][0]


def _posts(fx):
    return [e.text for e in fx if isinstance(e, core.Post)]


def test_discount_band_picks_the_group(harness: Harness):
    h = harness
    fx = h.propose_deal()  # Contoso Ltd · Team at 10,200.00 (list 12,000.00, 15.0% off)
    text = _post(fx).text
    assert "**DEAL-1** pending" in text and "needs one cfo approver in #sales" in text
    assert "D-2026-01 · Contoso Ltd · Team at 10,200.00 USD (list 12,000.00 USD, 15.0% off)" in text
    fx = h.approve_deal("m-ctrl", sender=LEONARDO)
    assert _post(fx).text == (
        "**DEAL-1** not applied — a 1,800.00 USD discount needs the cfo group; controllers may "
        "approve up to 1,000.00 USD."
    )
    fx = h.approve_deal("m-sales", sender=VINCENT)
    assert _post(fx).text == "**DEAL-1** not applied — Vincent is not in the cfo group."
    assert h.won() == []
    fx = h.approve_deal("m-cfo", sender=JOHN)
    assert "**DEAL-1** approved by John · won · sale 10,200.00 USD posted to 2026-11" in (
        _post(fx).text
    )
    assert h.deal().state.value == "won" and h.won() == ["D-2026-01"]
    assert h.books.summary(PERIOD)["revenue_cents"] == 1020000
    fx = h.sweep("sweep: stage lines")
    told = [t for t in _posts(fx) if t.startswith("**DEAL-1** won")]
    assert told and "2026-11 revenue is now 10,200.00 USD" in told[0]
    assert not [t for t in _posts(h.sweep()) if t.startswith("**DEAL-1** won")]  # once
    h.assert_no_leaks()
    write_report(
        "test_11_deal_discount",
        "Test 11 — The discount band",
        __doc__ or "",
        h,
        "PASS: the controller was refused above the discount limit with the discount read from "
        "the price book; the cfo's approval won the deal and its sale landed in the books.",
        [
            "the pending line names the group the discount requires",
            "a controller above the discount limit is refused with the discount and the limit",
            "a person in no group is refused by participant id",
            "the cfo's approval wins the deal; the sale is in November; finance is told once",
        ],
    )


def test_under_the_limit_the_controllers_approve(harness: Harness):
    h = harness
    fx = h.propose_deal(price_cents=1140000)  # 5.0% off: a 600.00 USD discount
    assert "one cfo or controllers approver (discount under 1,000.00 USD)" in _post(fx).text
    h.approve_deal("m-ctrl-ok", sender=LEONARDO)
    assert h.deal().state.value == "won" and h.won() == ["D-2026-01"]


def test_the_price_book_is_the_list_price(harness: Harness):
    h = harness
    fx = h.propose_deal(list_cents=1000000, price_cents=900000)  # claims Team lists at 10,000
    assert "the list price of Team is 12,000.00 USD, not 10,000.00 USD" in _post(fx).text
    assert h.deal() is None or h.deal().state.value != "pending"
    fx = h.propose_deal(
        "m-deal-2", deal_id="D-2026-02", product="Gold", list_cents=1000000, price_cents=900000
    )
    assert "Gold is not in the price book" in _post(fx).text
    assert h.won() == []


def test_a_list_price_edited_under_a_pending_deal(harness: Harness):
    h = harness
    h.propose_deal()
    h.books.set_list_price("Team", 1250000)  # the books move: 12,000.00 → 12,500.00
    fx = h.approve_deal("m-stale", sender=JOHN)
    texts = _posts(fx)
    assert any(
        "the ledger changed since the proposal (list price 12,000.00 USD → 12,500.00 USD)" in t
        for t in texts
    ), texts
    assert any("@sales please propose again" in t for t in texts), texts
    assert h.won() == []
    fx = h.propose_deal("m-deal-1b", thread="m-deal-1")  # the same deal, re-read from the book
    assert "(list 12,500.00 USD, 18.4% off)" in _post(fx).text
    h.approve_deal("m-fresh", sender=JOHN)
    assert h.deal().state.value == "won" and h.won() == ["D-2026-01"]
    assert h.books.deal("D-2026-01")["list_cents"] == 1250000


def test_a_locked_month_takes_no_deal(harness: Harness):
    h = harness
    h.run_plan_gate()
    h.run_pay_gate()
    h.propose_lock()
    h.approve_lock()
    assert h.books.period_status(BASIS) == "locked"
    fx = h.propose_deal(period=BASIS)
    assert "period 2026-10 is locked; the deal cannot land in it" in _post(fx).text
    assert h.books.deals(BASIS) == []
