"""Test 4 — Replay (DDD §15.3).

Isaac approves PR-41 twice; then the original approve message is re-delivered (same message
id). Must observe: one merge call; the second approval refused as not pending; the redelivery
produces nothing.
"""

from __future__ import annotations

from sindarin.executor import core
from validation.harness import FEATURE, ISAAC, Harness, write_report


def test_replay(harness: Harness):
    h = harness
    h.run_spec_gate()
    h.open_pull(41)
    h.propose_pr()
    h.review()

    first = h.approve_pr("m-ok-1")
    kinds = [type(e).__name__ for e in first]
    assert kinds.index("Transition") < kinds.index("Execute"), "state is written before the call"
    assert [e.state.value for e in first if isinstance(e, core.Transition)] == [
        "approved",
        "executing",
        "merged",
    ]
    a = h.pr()
    assert a.state.value == "merged" and a.approver_name == "Isaac" and a.snapshot_sha
    assert h.gh.count("execute") == 1 and h.gh.pulls[41].merged
    assert h.posts_to(FEATURE)[-1].text.startswith("**PR-41** approved by Isaac · merged as `")
    assert "applied" in h.audit_events()

    second = h.approve_pr("m-ok-2")
    post = [e for e in second if isinstance(e, core.Post)][0]
    assert post.text == "**PR-41** is merged, not pending."
    assert h.gh.count("execute") == 1

    # A redelivery of the *same message* (reconnect) is dropped entirely.
    dup = h.say(
        FEATURE,
        ISAAC,
        "@sindarin-github approve PR-41",
        mid="m-ok-1",
        thread="m-pr-1",
        label="redelivery of m-ok-1",
    )
    assert dup == []

    assert h.gh.count("execute") == 1, "exactly one merge call total"
    assert h.audit_events().count("refused_approval:not_pending") == 1
    assert len(h.github.store.approvals_for("PR-41")) == 1
    h.assert_no_leaks()

    write_report(
        "test_04_replay",
        "Test 4 · Replay",
        """
`PR-41` is approved by Isaac in the feature room. Isaac posts `approve PR-41` again; the
WebSocket then redelivers the original approval message id.
""",
        h,
        """
**PASS.** The first approval moved `PR-41` `pending → approved → executing → merged` with the
`approved` and `executing` rows written before GitHub was called, and produced exactly one
merge. The second approval was refused with `refused_approval:not_pending` ("PR-41 is merged,
not pending"). The redelivered message id produced no effects at all. One approval row, one
merge call.
""",
        [
            "first → merged (one adapter call)",
            "second → `refused_approval:not_pending`",
            "redelivered message id → nothing",
        ],
    )
