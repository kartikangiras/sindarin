"""Test 3 — Self-approval (DDD §15.3).

The pull request was opened by Isaac's own GitHub login (a human author). Isaac is the
engineering approver. Isaac's `approve PR-41` must be refused; no adapter call. Control: with
Vincent added to `engineering`, Vincent approves Isaac's PR and it merges.
"""

from __future__ import annotations

from sindarin.executor import core
from validation.harness import ISAAC, VINCENT, Harness, write_report


def _post(fx):
    return [e for e in fx if isinstance(e, core.Post)][0]


def test_self_approval(harness: Harness, temp_policy):
    h = harness
    h.run_spec_gate()
    h.open_pull(41, author_login="isaac-gh")  # Isaac pushed the branch and opened the PR
    h.propose_pr()
    assert h.pr().state.value == "pending"

    fx = h.approve_pr("m-self-1", sender=ISAAC)
    assert _post(fx).text == "**PR-41** not applied — Isaac opened this pull request."
    assert "refused_approval:self" in h.audit_events()
    assert h.gh.count("execute") == 0
    assert h.pr().state.value == "pending"

    # Control: a second engineer approves Isaac's PR normally.
    temp_policy(lambda d: d["groups"]["engineering"].append(VINCENT))
    fx = h.approve_pr("m-self-2", sender=VINCENT)
    assert h.pr().state.value == "merged"
    assert h.gh.count("execute") == 1 and h.gh.pulls[41].merged
    assert _post(fx).text.startswith("**PR-41** approved by Vincent · merged as `")
    h.assert_no_leaks()

    write_report(
        "test_03_self_approval",
        "Test 3 · Self-approval",
        """
`PR-41` was opened on GitHub by `isaac-gh`, the login registered for Isaac in `users.yaml`.
Isaac is the only `engineering` approver. Isaac types `@sindarin-github approve PR-41` in the
feature room. As a control, Vincent is added to `engineering` and approves the same PR.
""",
        h,
        """
**PASS.** Isaac's approval of a pull request Isaac authored was refused with
`refused_approval:self` (A3) even though Isaac passed the group check (A2); no adapter write;
`PR-41` stayed pending. The control shows another engineer approving the same PR, which merged
and was verified by read-back.
""",
        [
            "`refused_approval:self`, no adapter call",
            "control: another approver in the group can approve the same PR",
        ],
    )
