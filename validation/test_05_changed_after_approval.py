"""Test 5 — Changed after proposal (DDD §15.3).

A commit lands on the PR branch after the proposal (and its review) and before the engineer's
approval. A6 must refuse: the head re-read from GitHub differs from the snapshot. Approvals are
cleared, a notice is posted, no merge. A second approval on the new snapshot merges.
"""

from __future__ import annotations

from sindarin.executor import core
from validation.harness import FEATURE, Harness, write_report


def test_changed_after_approval(harness: Harness):
    h = harness
    h.run_spec_gate()
    h.open_pull(41)
    h.propose_pr()
    h.review()
    old_sha = h.pr().snapshot_sha
    old_head = h.gh.pulls[41].head_sha

    new_head = h.gh.push(41)  # the branch moved under the approver
    fx = h.approve_pr("m-ok-1")
    texts = [e.text for e in fx if isinstance(e, core.Post)]
    assert texts[0].startswith("**PR-41** changed since it was proposed (head_sha ")
    assert old_head[:7] in texts[0] and new_head[:7] in texts[0]
    assert texts[0].endswith("Approvals cleared; it is pending again.")
    assert texts[1].startswith("**PR-41** not applied — it changed since it was proposed")
    assert h.gh.count("execute") == 0, "no merge"
    a = h.pr()
    assert a.state.value == "pending" and a.snapshot_sha != old_sha
    assert a.facts()["head_sha"] == new_head and a.approver_id is None
    assert h.audit_events()[-2:] == ["snapshot_changed", "refused_approval:snapshot_changed"]
    assert h.github.store.approvals_for("PR-41") == []

    # the reviewer's report is now at an older sha; status says so
    fx = h.command("m-st", "status PR-41", sender="@switch-user-isaac:test", room=FEATURE)
    status = [e for e in fx if isinstance(e, core.Post)][0].text
    assert f"at `{old_head[:7]}`" in status

    # Approve again on the new snapshot → merged.
    fx = h.approve_pr("m-ok-2")
    assert h.pr().state.value == "merged" and h.gh.pulls[41].merged
    assert h.gh.pulls[41].merge_commit_sha and h.gh.count("execute") == 1
    h.assert_no_leaks()

    write_report(
        "test_05_changed_after_approval",
        "Test 5 · Changed after proposal",
        """
`PR-41` is proposed and reviewed at head `A`. A new commit lands on the branch (head `B`).
Isaac then types `approve PR-41`. The executor re-reads the pull request from GitHub before
writing anything (A6). Isaac approves a second time on the new snapshot.
""",
        h,
        """
**PASS.** The first approval was refused with `refused_approval:snapshot_changed`: the notice
named the old and new head, the artifact was re-snapshotted at `B`, approvals were cleared,
and GitHub was not asked to merge. The second approval merged `B`; GitHub's own `sha` guard
would have refused anything else.
""",
        [
            "A6 refuses; approvals cleared; pending notice posted; no merge",
            "a second approval on the new snapshot merges",
        ],
    )
