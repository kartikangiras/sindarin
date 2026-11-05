"""Test 7 — Expiry (DDD §15.3, new).

A spec pending past its `pending_ttl` expires on the sweep: state `expired`, `product` told in
the thread, no room opened. A late `approve` is refused as expired, and the same on a PR.
"""

from __future__ import annotations

from sindarin.executor import core
from validation.harness import PRODUCT, Harness, write_report


def test_expiry(harness: Harness):
    h = harness
    h.propose_spec()
    assert h.spec().state.value == "pending"

    h.clock.advance(hours=23)
    assert [e for e in h.sweep("sweep (23 h, not due)") if isinstance(e, core.Transition)] == []
    assert h.spec().state.value == "pending"

    h.clock.advance(hours=1, seconds=1)
    fx = h.sweep("sweep (24 h, due)")
    assert [e.state.value for e in fx if isinstance(e, core.Transition)] == ["expired"]
    post = [e for e in fx if isinstance(e, core.Post)][0]
    assert post.room_id == PRODUCT and post.thread_id == "m-spec-1"
    assert (
        post.text
        == "**SPEC-1** expired after 24h without a decision. @product re-propose if still wanted."
    )
    assert h.spec().state.value == "expired"
    assert h.rooms.count("execute") == 0, "no room opened"

    fx = h.approve_spec("m-late")
    post = [e for e in fx if isinstance(e, core.Post)][0]
    assert post.text == "**SPEC-1** is expired, not pending."

    # the product agent re-proposes: a new id, pending again
    h.propose_spec("m-spec-2")
    assert h.spec("SPEC-2").state.value == "pending"

    # a PR left pending past its TTL is caught at approval time as well as by the sweep
    h.approve_spec("m-ok-2", spec="SPEC-2")
    h.open_pull(41)
    h.propose_pr()
    h.clock.advance(hours=25)
    fx = h.approve_pr("m-late-pr")
    texts = [e.text for e in fx if isinstance(e, core.Post)]
    assert texts[0].startswith("**PR-41** expired after 24h")
    assert texts[1] == "**PR-41** is expired, not pending."
    assert h.gh.count("execute") == 0
    h.assert_no_leaks()

    write_report(
        "test_07_expiry",
        "Test 7 · Expiry",
        """
`SPEC-1` is proposed and nobody decides. The clock advances 23 h (sweep: nothing), then past
24 h (sweep: expire). Leonardo approves late. `product` re-proposes. Later a pull request is
left pending for 25 h and Isaac approves it.
""",
        h,
        """
**PASS.** The sweep expired `SPEC-1` exactly when due, told `product` in the spec's thread,
and opened no room. The late approval was refused ("expired, not pending"). The re-proposal got
a new id. The stale pull request was expired at approval time with the same template and no
merge.
""",
        [
            "`expired`; `product` is told; no room opened",
            "a late approval is refused; the same guard covers PR-n",
        ],
    )
