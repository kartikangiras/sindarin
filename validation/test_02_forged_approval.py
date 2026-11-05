"""Test 2 — Forged approval (DDD §15.3).

`coder` (the proposing agent) posts `@sindarin-github approve PR-41`; Vincent (no group) posts
the same; an impostor whose display name is "Isaac" but whose id is not Isaac's posts the same.
Must observe: refused with the agent template and the not-approver template; no adapter call.
"""

from __future__ import annotations

from sindarin.executor import core
from validation.harness import CODER_AGENT, FEATURE, VINCENT, Harness, write_report


def _post(fx):
    return [e for e in fx if isinstance(e, core.Post)][0]


def test_forged_approval(harness: Harness):
    h = harness
    h.run_spec_gate()
    h.open_pull(41)
    h.propose_pr()
    h.review()
    assert h.pr().state.value == "pending"
    executes_before = h.gh.count("execute")

    fx = h.approve_pr("m-forge-1", sender=CODER_AGENT)
    assert _post(fx).text == "**PR-41** not applied — @coder is an agent, not an approver."
    assert _post(fx).room_id == FEATURE and _post(fx).thread_id == "m-pr-1"

    fx = h.approve_pr("m-forge-2", sender=VINCENT)
    assert _post(fx).text == "**PR-41** not applied — Vincent is not in the engineering group."

    fx = h.say(
        FEATURE,
        "@switch-user-imposter:test",
        "@sindarin-github approve PR-41",
        mid="m-forge-3",
        name="Isaac",
    )
    assert _post(fx).text == "**PR-41** not applied — Isaac is not in the engineering group."

    refusals = [e for e in h.github.store.audit_all() if e.event.startswith("refused_approval")]
    assert [e.event for e in refusals] == [
        "refused_approval:agent",
        "refused_approval:not_approver",
        "refused_approval:not_approver",
    ]
    assert [e.actor_id for e in refusals] == [CODER_AGENT, VINCENT, "@switch-user-imposter:test"]
    assert h.gh.count("execute") == executes_before == 0
    assert h.pr().state.value == "pending"
    assert h.github.store.approvals_for("PR-41") == []
    h.assert_no_leaks()

    write_report(
        "test_02_forged_approval",
        "Test 2 · Forged approval",
        """
With `PR-41` pending in the feature room (reviewer reported 4 of 4), three non-approvers type
`@sindarin-github approve PR-41` there: `coder`, the agent that proposed the merge; Vincent, a
person in no approver group; and an impostor whose display name is "Isaac" (the real engineer)
but whose participant id is not Isaac's.
""",
        h,
        """
**PASS.** The agent was refused with the agent template ("@coder is an agent, not an
approver"), Vincent and the impostor with the not-approver template. Identity was matched on
participant id only: the display name "Isaac" did not help. No adapter write, no approval row,
no state change; `PR-41` is still pending.
""",
        [
            "both refused with the agent and not-approver templates; no adapter call",
            "display-name spoofing refused: approver match is by participant id",
        ],
    )
