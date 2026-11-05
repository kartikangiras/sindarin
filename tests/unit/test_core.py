"""Core pipeline coverage beyond the seven adversarial tests (DDD §7): P1–P5 rejections,
re-proposals, deny and changes, reviews, commands, retries, crash recovery, spec stages, board."""

from __future__ import annotations

import json

import pytest
from validation.harness import (
    CODER_AGENT,
    CUSTOMER,
    FEATURE,
    GITHUB_EXEC,
    ISAAC,
    JOHN,
    LEONARDO,
    PRODUCT,
    PRODUCT_AGENT,
    RELEASE_AGENT,
    RELEASES,
    REVIEWER_AGENT,
    SWITCH_EXEC,
    VINCENT,
    Harness,
    pr_proposal,
    proposal_text,
    release_proposal,
    review_fence,
    spec_proposal,
)

from sindarin.adapters.base import ActionResult, VerifyResult
from sindarin.executor import core
from sindarin.names import GITHUB_EXECUTOR, SWITCH_EXECUTOR
from sindarin.schema import State, is_status_fence


def _posts(fx, *, fences: bool = False):
    return [e for e in fx if isinstance(e, core.Post) and (fences or not is_status_fence(e.text))]


def _text(fx) -> str:
    return _posts(fx)[0].text


# -- P pipeline ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,fragment",
    [
        ("@sindarin-switch proposal\n```sindarin-proposal\nnot json\n```", "not JSON"),
        ("@sindarin-switch proposal\n```sindarin-proposal\n{} unterminated", "found 0"),
        ("x\n```sindarin-proposal\n{}\n```\n```sindarin-proposal\n{}\n```", "found 2"),
        (
            '@sindarin-switch proposal\n```sindarin-proposal\n{"schema":"sindarin/proposal/v2","kind":"spec"}\n```',
            "platform",
        ),
    ],
)
def test_p1_malformed(harness: Harness, raw, fragment):
    fx = harness.say(PRODUCT, PRODUCT_AGENT, raw, mid="m1")
    assert _text(fx).startswith("Proposal not recorded — malformed:") and fragment in _text(fx)
    assert harness.audit_events(SWITCH_EXECUTOR) == ["rejected_proposal:malformed"]
    assert harness.switch.store.all() == []


def test_p1_extra_key_and_wrong_executor(harness: Harness):
    body = json.loads(spec_proposal().canonical())
    body["approved"] = True
    raw = f"@sindarin-switch proposal\n```sindarin-proposal\n{json.dumps(body)}\n```"
    fx = harness.say(PRODUCT, PRODUCT_AGENT, raw, mid="m1")
    assert "approved" in _text(fx)
    # a spec fence sent to sindarin-github (in a room it listens to) is refused, not recorded
    fx = harness.say(RELEASES, PRODUCT_AGENT, proposal_text(spec_proposal()), mid="m2")
    assert _text(fx) == "Proposal not recorded — spec proposals go to @sindarin-switch, not me."
    assert harness.github.store.all() == []


def test_p2_wrong_proposer(harness: Harness):
    fx = harness.say(PRODUCT, CODER_AGENT, proposal_text(spec_proposal()), mid="m1")
    assert _text(fx) == "Proposal not recorded — only product may propose under rule spec."
    fx = harness.say(PRODUCT, LEONARDO, proposal_text(spec_proposal()), mid="m2")
    assert _text(fx) == "Proposal not recorded — only product may propose under rule spec."
    fx = harness.say(
        PRODUCT, PRODUCT_AGENT, proposal_text(spec_proposal()), mid="m3", name="Sindarin Switch"
    )
    assert harness.spec("SPEC-1") is not None, "display names never matter"
    assert harness.audit_events(SWITCH_EXECUTOR)[:2] == ["rejected_proposal:bad_sender"] * 2


def test_p2_wrong_room(harness: Harness):
    harness.run_spec_gate()
    harness.open_pull(41)
    # a merge proposal in #releases (sindarin-github listens there) is the wrong room
    fx = harness.propose_pr("m-pr-x", room=RELEASES)
    assert _text(fx) == "Proposal not recorded — I take pr proposals in the feature room only."
    # a release proposal in a feature room
    fx = harness.propose_release("m-rel-x", room=FEATURE)
    assert _text(fx) == "Proposal not recorded — I take release proposals in #releases only."
    assert harness.github.store.all() == []


def test_p3_policy(harness: Harness):
    fx = harness.propose_spec("m1", repo="acme/other")
    assert _text(fx).startswith(
        "Proposal not recorded — policy: repository 'acme/other' is not allowed"
    )
    fx = harness.propose_spec("m2", engineer=VINCENT)
    assert _text(fx) == "Proposal not recorded — policy: engineer is not in group engineering."
    harness.run_spec_gate()
    harness.open_pull(41)
    fx = harness.propose_pr("m3", repo="acme/other")
    assert "not allowed by rule merge" in _text(fx)
    fx = harness.propose_release("m4", tag="v9")
    assert "does not match rule release pattern" in _text(fx)
    assert harness.gh.count("execute") == 0 and harness.github.store.all() == []


def test_p4_facts_from_the_platform(harness: Harness):
    harness.run_spec_gate()
    fx = harness.propose_pr("m1", number=99)  # never opened
    assert (
        _text(fx)
        == "Proposal not recorded — could not read acme/sindarin-demo-app#99 from github: pull request #99 not found."
    )
    pr = harness.open_pull(41, draft=True)
    fx = harness.propose_pr("m2")
    assert _text(fx) == "Proposal not recorded — pull request is a draft."
    pr.draft, pr.base = False, "develop"
    fx = harness.propose_pr("m3")
    assert "merges into main" in _text(fx)
    pr.base = "main"
    # a stale hint: the fence names an old head
    fx = harness.propose_pr("m4", head_sha="0" * 40)
    assert _text(fx).startswith("Proposal not recorded — head is `")
    assert "re-propose at the current head" in _text(fx)
    fx = harness.propose_pr("m5")
    assert harness.pr().state is State.PENDING
    assert harness.pr().facts()["head_sha"] == pr.head_sha
    # releases: tag exists / commit missing
    harness.gh.tags["v1.4.0"] = "a" * 40
    fx = harness.propose_release("m6")
    assert _text(fx) == "Proposal not recorded — tag v1.4.0 already exists."
    fx = harness.propose_release("m7", tag="v1.5.0", target_sha="f" * 40)
    assert _text(fx) == "Proposal not recorded — target sha is not a commit in the repository."


def test_p5_duplicate_and_reproposal_keep_id(harness: Harness):
    harness.propose_spec("m-spec-1")
    fx = harness.say(
        PRODUCT, PRODUCT_AGENT, proposal_text(spec_proposal()), mid="m-spec-dup", thread="m-spec-1"
    )
    assert _text(fx) == "Proposal not recorded — SPEC-1 is already pending with this snapshot."
    # changes → changes_requested; the re-proposal in the thread keeps SPEC-1, new snapshot
    fx = harness.command(
        "m-ch",
        "changes SPEC-1 tighten criterion 2",
        sender=LEONARDO,
        room=PRODUCT,
        executor=SWITCH_EXECUTOR,
        thread="m-spec-1",
    )
    assert _text(fx).startswith(
        "**SPEC-1** sent back by Leonardo: tighten criterion 2 @product revise"
    )
    a = harness.spec()
    assert (
        a.state is State.CHANGES_REQUESTED
        and a.note == "tighten criterion 2"
        and a.approver_name == "Leonardo"
    )
    old = a.snapshot_sha
    fx = harness.say(
        PRODUCT,
        PRODUCT_AGENT,
        proposal_text(spec_proposal(criteria=["a", "b", "c", "tighter d"])),
        mid="m-spec-2",
        thread="m-spec-1",
    )
    a = harness.spec()
    assert (
        a.state is State.PENDING and a.snapshot_sha != old and a.seq == 4 and a.approver_id is None
    )
    assert a.proposal_message_id == "m-spec-2" and a.proposal().body.criteria[-1] == "tighter d"
    assert _text(fx).startswith("**SPEC-1** pending (revised, approvals cleared) ·")
    assert harness.switch.store.peek_next_id(core.Kind.SPEC) == "SPEC-2"
    assert harness.audit_events(SWITCH_EXECUTOR)[-1] == "reproposed"
    # a *different* spec in another thread gets the next id
    harness.propose_spec("m-spec-3", slug="feat-xlsx")
    assert harness.spec("SPEC-2").target == "feat-xlsx"


def test_pr_reproposal_updates_snapshot_in_place(harness: Harness):
    harness.run_spec_gate()
    harness.open_pull(41)
    harness.propose_pr()
    harness.gh.push(41)
    fx = harness.propose_pr("m-pr-2")
    assert _text(fx).startswith("**PR-41** pending (revised, approvals cleared) ·")
    assert harness.pr().facts()["head_sha"] == harness.gh.pulls[41].head_sha
    harness.approve_pr()
    fx = harness.propose_pr("m-pr-3")
    assert _text(fx) == "Proposal not recorded — pull request is closed, not open."
    # and a merged artifact proposed again while GitHub still says open is refused by state
    harness.gh.pulls[41].state, harness.gh.pulls[41].merged = "open", False
    fx = harness.propose_pr("m-pr-4")
    assert _text(fx) == "Proposal not recorded — PR-41 is merged, not open for proposals."


def test_unaddressed_own_and_idless_messages_only_recorded(harness: Harness):
    fx = harness.say(
        PRODUCT, LEONARDO, "@sindarin-switch approve SPEC-1", mid="m1", addressed=False
    )
    assert [type(e).__name__ for e in fx] == ["RecordMessage"]
    fx = harness.say(PRODUCT, SWITCH_EXEC, "@sindarin-switch approve SPEC-1", mid="m2")
    assert [type(e).__name__ for e in fx] == ["RecordMessage"]
    fx = harness.say(PRODUCT, "", "@sindarin-switch approve SPEC-1", mid="m3", name="")
    assert [type(e).__name__ for e in fx] == ["RecordMessage"]
    assert harness.posts == []


# -- reviews ------------------------------------------------------------------------------------


def test_review_paths(harness: Harness):
    harness.run_spec_gate()
    harness.open_pull(41)
    harness.propose_pr()
    head = harness.gh.pulls[41].head_sha
    fx = harness.say(FEATURE, CODER_AGENT, review_fence(head_sha=head), mid="r1", thread="m-pr-1")
    assert _text(fx) == "Review not recorded — only reviewer posts reviews."
    fx = harness.say(
        FEATURE, REVIEWER_AGENT, review_fence(pr="PR-99", head_sha=head), mid="r2", thread="m-pr-1"
    )
    assert _text(fx) == "Review not recorded — PR-99 is unknown here."
    fx = harness.say(
        FEATURE, REVIEWER_AGENT, "@sindarin-github review\n```sindarin-review\n{}\n```", mid="r3"
    )
    assert _text(fx).startswith("Review not recorded — malformed:")
    fx = harness.review("r4", met=[True, False, True, False])
    assert (
        _text(fx) == f"**PR-41** last review: **2 of 4** criteria met at `{head[:7]}` by reviewer."
    )
    assert _posts(fx)[0].thread_id == "m-pr-1"
    # a review of an older head is kept and flagged
    harness.gh.push(41)
    harness.propose_pr("m-pr-2")
    fx = harness.say(
        FEATURE, REVIEWER_AGENT, review_fence(head_sha=head), mid="r5", thread="m-pr-1"
    )
    assert "The pending head is `" in _text(fx)
    # the pending template shows the last review
    harness.review("r6")
    fx = harness.command("st", "status PR-41", sender=ISAAC, room=FEATURE)
    assert "last review: **4 of 4**" in _text(fx)
    # a review posted to the wrong executor
    fx = harness.say(
        PRODUCT, REVIEWER_AGENT, review_fence(head_sha=head), mid="r7", to=(SWITCH_EXECUTOR,)
    )
    assert _text(fx) == "Review not recorded — reviews go to @sindarin-github."


# -- commands -----------------------------------------------------------------------------------


def test_help_whoami_bare_mention_and_silence(harness: Harness):
    fx = harness.command("c1", "help", sender=VINCENT, room=RELEASES)
    assert _text(fx).startswith("Commands: `approve <id>`")
    fx = harness.command("c2", "whoami", sender=ISAAC, room=FEATURE)
    assert _text(fx) == f"Isaac: participant id `{ISAAC}` · groups: engineering"
    fx = harness.command("c3", "whoami", sender=CODER_AGENT, room=FEATURE)
    assert _text(fx) == f"coder: participant id `{CODER_AGENT}` · agent role coder"
    fx = harness.say(RELEASES, JOHN, "@sindarin-github", mid="c4")
    assert _text(fx).startswith("Commands:")
    fx = harness.say(RELEASES, JOHN, "@sindarin-github please approve REL-1 now", mid="c5")
    assert _posts(fx) == [] and harness.audit_events(GITHUB_EXECUTOR)[-1] == "ignored"


def test_status_and_unknown_ids(harness: Harness):
    fx = harness.command("s0", "status", sender=ISAAC, room=FEATURE)
    assert _text(fx) == "No open artifacts in this room."
    harness.run_spec_gate()
    harness.open_pull(41)
    harness.propose_pr()
    fx = harness.command("s1", "status", sender=ISAAC, room=FEATURE)
    assert _text(fx).startswith(
        "**PR-41** · pending · #41 CSV export of orders"
    ) and "awaiting approval until" in _text(fx)
    fx = harness.command("s2", "status PR-41", sender=VINCENT, room=FEATURE)
    assert "snapshot sha256" in _text(fx) and "revision 1" in _text(fx)
    fx = harness.command("s3", "status SPEC-1", sender=ISAAC, room=FEATURE)
    assert _text(fx) == "**SPEC-1** is unknown here."
    fx = harness.command("s4", "approve SPEC-1", sender=LEONARDO, room=FEATURE)
    assert _text(fx) == "**SPEC-1** is unknown here. Try @sindarin-switch."
    fx = harness.command("s5", "approve PR-7", sender=ISAAC, room=FEATURE)
    assert _text(fx) == "**PR-7** is unknown here."


def test_deny_paths(harness: Harness):
    harness.propose_spec()
    fx = harness.command(
        "d1", "deny SPEC-1", sender=VINCENT, room=PRODUCT, executor=SWITCH_EXECUTOR
    )
    assert _text(fx) == "**SPEC-1** not applied — Vincent is not in the pm group."
    fx = harness.command(
        "d2", "deny SPEC-1", sender=PRODUCT_AGENT, room=PRODUCT, executor=SWITCH_EXECUTOR
    )
    assert _text(fx) == "**SPEC-1** not applied — @product is an agent, not an approver."
    fx = harness.command(
        "d3",
        "deny SPEC-1 not this quarter",
        sender=LEONARDO,
        room=PRODUCT,
        executor=SWITCH_EXECUTOR,
    )
    assert _text(fx) == "**SPEC-1** denied by Leonardo: not this quarter. @product nothing happens."
    a = harness.spec()
    assert a.state is State.DENIED and a.note == "not this quarter"
    fx = harness.command(
        "d4", "approve SPEC-1", sender=LEONARDO, room=PRODUCT, executor=SWITCH_EXECUTOR
    )
    assert _text(fx) == "**SPEC-1** is denied, not pending."
    fx = harness.command(
        "d5", "changes SPEC-1 again", sender=LEONARDO, room=PRODUCT, executor=SWITCH_EXECUTOR
    )
    assert _text(fx) == "**SPEC-1** is denied, not pending."
    assert harness.rooms.count("execute") == 0


def test_peer_executor_is_refused_as_approver(harness: Harness):
    harness.propose_spec()
    fx = harness.command(
        "x1", "approve SPEC-1", sender=GITHUB_EXEC, room=PRODUCT, executor=SWITCH_EXECUTOR
    )
    assert _text(fx) == "**SPEC-1** not applied — @sindarin-github is an agent, not an approver."


def test_changes_requested_cannot_be_approved_until_reproposed(harness: Harness):
    harness.propose_spec()
    harness.command(
        "c1",
        "changes SPEC-1 add a limit",
        sender=LEONARDO,
        room=PRODUCT,
        executor=SWITCH_EXECUTOR,
        thread="m-spec-1",
    )
    fx = harness.approve_spec("c2")
    assert _text(fx) == "**SPEC-1** is changes_requested, not pending."
    fx = harness.command(
        "c3", "changes SPEC-1 and more", sender=LEONARDO, room=PRODUCT, executor=SWITCH_EXECUTOR
    )
    assert _text(fx) == "**SPEC-1** is changes_requested, not pending."
    fx = harness.command(
        "c4", "deny SPEC-1 never mind", sender=LEONARDO, room=PRODUCT, executor=SWITCH_EXECUTOR
    )
    assert harness.spec().state is State.DENIED


# -- execution, retries, recovery --------------------------------------------------------------


def test_spec_gate_opens_room_with_plan_and_posts_brief(harness: Harness):
    harness.propose_spec()
    plan = harness.spec().facts()
    assert plan["humans"] == [ISAAC, LEONARDO]
    assert plan["agents"] == [CODER_AGENT, REVIEWER_AGENT, GITHUB_EXEC, SWITCH_EXEC]
    fx = harness.approve_spec()
    kinds = [type(e).__name__ for e in fx]
    assert kinds.index("Transition") < kinds.index("Execute")
    assert [e.state.value for e in fx if isinstance(e, core.Transition)] == [
        "approved",
        "opening",
        "open",
    ]
    texts = [p.text for p in _posts(fx)]
    assert texts[0].startswith(
        "**SPEC-1** approved by Leonardo · room **#feat-csv-export** opened · verified at **"
    )
    assert texts[1].startswith(
        "**SPEC-1** feature room **#feat-csv-export** is open · people: Isaac, Leonardo · agents: coder, reviewer, sindarin-github, sindarin-switch"
    )
    room = harness.rooms.rooms["feat-csv-export"]
    assert room["humans"] == [ISAAC, LEONARDO] and set(room["agents"]) == set(plan["agents"])
    assert harness.spec().result()["room_id"] == FEATURE


def test_spec_plan_change_is_caught_at_approval(harness: Harness, temp_policy):
    harness.propose_spec()
    temp_policy(lambda d: d["groups"]["pm"].append(VINCENT))
    fx = harness.approve_spec()
    texts = [p.text for p in _posts(fx)]
    assert texts[0].startswith("**SPEC-1** changed since it was proposed (humans ")
    assert harness.spec().state is State.PENDING and harness.rooms.count("execute") == 0
    harness.approve_spec("m-ok-2")
    assert harness.spec().state is State.OPEN


def test_merge_failure_retries_once_then_fails(harness: Harness):
    harness.run_spec_gate()
    harness.open_pull(41)
    harness.propose_pr()
    harness.gh.fail_execute_times = 1
    fx = harness.approve_pr()
    assert [e.state.value for e in fx if isinstance(e, core.Transition)] == [
        "approved",
        "executing",
        "merge_failed",
    ]
    a = harness.pr()
    assert (
        a.state is State.MERGE_FAILED and a.attempts == 1 and a.error == "GitHub 502: bad gateway"
    )
    assert (
        _posts(fx)[-1].text
        == "**PR-41** approved by Isaac but merge pr failed: GitHub 502: bad gateway. Retrying once in 60 min."
    )
    harness.clock.advance(minutes=30)
    assert [e for e in harness.sweep() if isinstance(e, core.Execute)] == []
    harness.clock.advance(minutes=31)
    fx = harness.sweep()
    assert harness.pr().state is State.MERGED and harness.pr().attempts == 2
    assert "retry" in harness.audit_events()
    # a second failure is final
    harness.open_pull(42)
    harness.propose_pr("m-pr-2", number=42)
    harness.gh.fail_execute_times = 2
    harness.approve_pr("ok-2", pr="PR-42")
    harness.clock.advance(minutes=61)
    fx = harness.sweep()
    a = harness.pr("PR-42")
    assert a.state is State.FAILED and a.attempts == 2
    assert _posts(fx)[-1].text.startswith("**PR-42** approved by Isaac but merge pr failed again:")
    assert "a new proposal is required" in _posts(fx)[-1].text
    assert harness.sweep() == [] or not any(isinstance(e, core.Execute) for e in harness.sweep())


def test_room_open_failure_retries_hourly_for_ever(harness: Harness):
    harness.propose_spec()
    harness.rooms.fail_execute_times = 3
    harness.approve_spec()
    assert harness.spec().state is State.OPEN_FAILED
    for _ in range(2):
        harness.clock.advance(minutes=61)
        harness.sweep()
        assert harness.spec().state is State.OPEN_FAILED
    harness.clock.advance(minutes=61)
    harness.sweep()
    assert harness.spec().state is State.OPEN and harness.spec().attempts == 4


def test_read_back_failure_is_not_success(harness: Harness):
    harness.run_spec_gate()
    harness.open_pull(41)
    harness.propose_pr()
    harness.gh.fail_verify_times = 1
    harness.approve_pr()
    a = harness.pr()
    assert a.state is State.MERGE_FAILED and "read-back failed" in a.error
    assert harness.gh.pulls[41].merged, "the merge landed; the retry will find it"
    harness.clock.advance(minutes=61)
    harness.sweep()
    assert harness.pr().state is State.MERGED


def test_crash_recovery(harness: Harness):
    harness.run_spec_gate()
    harness.open_pull(41)
    harness.propose_pr()
    # crash after `approved` was written and before `executing`
    harness.github.store.transition(
        "PR-41",
        State.APPROVED,
        approver_id=ISAAC,
        approver_name="Isaac",
        approved_at=harness.clock.now,
    )
    harness.sweep()
    assert (
        "recovery_resume" in harness.audit_events() and "recovery_execute" in harness.audit_events()
    )
    assert harness.pr().state is State.MERGED
    # crash after `executing` with the merge already landed: verify alone finishes it
    harness.open_pull(42)
    harness.propose_pr("m-pr-2", number=42)
    harness.github.store.transition(
        "PR-42",
        State.APPROVED,
        approver_id=ISAAC,
        approver_name="Isaac",
        approved_at=harness.clock.now,
    )
    harness.github.store.transition("PR-42", State.EXECUTING, attempts=1)
    harness.gh.pulls[42].merged = True
    harness.gh.pulls[42].merge_commit_sha = "c" * 40
    harness.sweep()
    assert harness.pr("PR-42").state is State.MERGED and harness.gh.count("execute") == 1
    # a verify error leaves it for the next sweep
    harness.open_pull(43)
    harness.propose_pr("m-pr-3", number=43)
    harness.github.store.transition(
        "PR-43",
        State.APPROVED,
        approver_id=ISAAC,
        approver_name="Isaac",
        approved_at=harness.clock.now,
    )
    harness.github.store.transition("PR-43", State.EXECUTING, attempts=1)
    harness.gh.fail_verify_times = 1
    harness.sweep()
    assert (
        harness.pr("PR-43").state is State.EXECUTING
        and harness.audit_events()[-1] == "recovery_verify_error"
    )
    harness.sweep()
    assert harness.pr("PR-43").state is State.MERGED


def test_after_execute_ignores_stale_state(harness: Harness):
    harness.propose_spec()
    ctx = harness.switch.context()
    fx = core.after_execute(ctx, harness.spec(), ActionResult(ok=True), True)
    assert [e.event for e in fx if isinstance(e, core.Audit)] == ["execute_result_ignored"]
    fx = core.after_verify(ctx, harness.spec(), VerifyResult(present=True))
    assert [e.event for e in fx if isinstance(e, core.Audit)] == ["verify_result_ignored"]


def test_post_failure_does_not_stop_state(harness: Harness):
    harness.propose_spec()

    def boom(room, text, thread):
        raise RuntimeError("switch down")

    harness.switch.poster = boom
    harness.approve_spec()
    assert harness.spec().state is State.OPEN


# -- spec stages and the board (sindarin-switch reads sindarin-github's state) -----------------


def test_spec_stages_follow_the_peer_state(harness: Harness):
    harness.run_spec_gate()
    harness.run_merge_gate()
    fx = harness.sweep()
    assert harness.spec().state is State.DONE
    assert any(p.text == "**SPEC-1** done · PR-41 merged." for p in _posts(fx))
    harness.propose_release()
    harness.approve_release()
    fx = harness.sweep()
    assert harness.spec().state is State.RELEASED
    assert any(p.text == "**SPEC-1** released in REL-1." for p in _posts(fx))
    assert harness.audit_events(SWITCH_EXECUTOR)[-2:] == [
        "spec_released",
        "board_posted",
    ] or "spec_released" in harness.audit_events(SWITCH_EXECUTOR)


def test_board(harness: Harness):
    harness.run_spec_gate()
    harness.open_pull(41)
    harness.propose_pr()
    fx = harness.command("b1", "board", sender=LEONARDO, room=PRODUCT, executor=SWITCH_EXECUTOR)
    board = _posts(fx)[0]
    assert board.room_id == PRODUCT and board.thread_id is None
    lines = board.text.splitlines()
    assert lines[0].startswith("**Board** · 2026-11-05") and lines[1] == "**In flight**"
    assert any(
        ln.startswith("SPEC-1 · open · Leonardo ·") and "waiting on coder and Isaac" in ln
        for ln in lines
    )
    assert any(
        ln.startswith("PR-41 · pending · coder ·") and "waiting on engineering: Isaac" in ln
        for ln in lines
    )
    assert "**Waiting on**" in lines
    assert any(ln == "PR-41 · #feat-csv-export · engineering: Isaac" for ln in lines)
    # the daily rebuild happens once per UTC day
    assert not any("**Board**" in p.text for p in _posts(harness.sweep()))
    harness.clock.advance(days=1)
    assert any("**Board**" in p.text for p in _posts(harness.sweep()))
    fx = harness.command("b2", "board", sender=JOHN, room=RELEASES)
    assert _text(fx) == "The board is kept by @sindarin-switch in #product."


def test_status_fences_optional(harness: Harness):
    harness.switch.config.post_status_fences = True
    harness.propose_spec()
    fences = [p for p in harness.posts if is_status_fence(p.text)]
    assert (
        len(fences) == 1
        and '"state":"pending"' in fences[0].text
        and "@switch" not in fences[0].text
    )
    harness.approve_spec()
    assert [p for p in harness.posts if is_status_fence(p.text)][-1].text.count(
        '"state":"open"'
    ) == 1


def test_customer_room_has_no_executor(harness: Harness):
    fx = harness.say(CUSTOMER, ISAAC, "@sindarin-switch approve SPEC-1", mid="c1")
    assert fx == [] and harness.posts == []
    assert (
        RELEASE_AGENT.startswith("@switch-agent-")
        and release_proposal(target_sha="a" * 40).kind.value == "release"
    )
    assert pr_proposal().kind.value == "pr"
