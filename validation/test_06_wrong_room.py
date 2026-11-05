"""Test 6 — Wrong room (DDD §15.3, new).

`approve REL-1` posted in a feature room, where sindarin-github also listens, must be refused
with the room template. The same command in #releases applies.
"""

from __future__ import annotations

from sindarin.executor import core
from validation.harness import FEATURE, JOHN, RELEASES, Harness, write_report


def test_wrong_room(harness: Harness):
    h = harness
    h.run_spec_gate()
    h.run_merge_gate()
    h.propose_release()
    assert h.rel().state.value == "pending"

    fx = h.command("m-wr-1", "approve REL-1", sender=JOHN, room=FEATURE)
    post = [e for e in fx if isinstance(e, core.Post)][0]
    assert post.room_id == FEATURE
    assert post.text == "**REL-1** not applied — I take release commands in #releases only."
    assert "refused_approval:wrong_room" in h.audit_events()
    assert h.gh.count("execute") == 1  # the merge only
    assert h.rel().state.value == "pending"

    # and a merge command in #releases is refused the same way
    h.open_pull(42)
    h.propose_pr("m-pr-2", number=42, spec="SPEC-1")
    fx = h.command("m-wr-2", "approve PR-42", sender="@switch-user-isaac:test", room=RELEASES)
    post = [e for e in fx if isinstance(e, core.Post)][0]
    assert post.text == "**PR-42** not applied — I take pr commands in #feat-csv-export only."

    fx = h.approve_release("m-ok-rel")
    assert h.rel().state.value == "published"
    assert "v1.4.0" in h.gh.tags and h.gh.count("execute") == 2
    post = [e for e in fx if isinstance(e, core.Post)][0]
    assert post.text.startswith("**REL-1** approved by John · published https://github.com/")
    h.assert_no_leaks()

    write_report(
        "test_06_wrong_room",
        "Test 6 · Wrong room",
        """
`REL-1` is pending in `#releases`. John, a release approver, types `@sindarin-github approve
REL-1` in the feature room instead. Isaac types `approve PR-42` in `#releases`. John then
approves `REL-1` in `#releases`.
""",
        h,
        """
**PASS.** Both misplaced commands were refused with the room template (A5) and audited as
`refused_approval:wrong_room`; nothing was executed. The same approval in the right room
published the release, verified by read-back.
""",
        [
            "refused with the room template",
            "the same command in the rule's room applies",
        ],
    )
