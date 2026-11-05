"""The four agents on the stand-in model (DDD §6, §13): one strict tool each, fences only from
the validator, message rules on every output, routing to the right room and thread."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from validation.harness import (
    CODER_AGENT,
    CUSTOMER,
    FEATURE,
    FIXTURES,
    GITHUB_EXEC,
    ISAAC,
    JOHN,
    PRODUCT,
    PRODUCT_AGENT,
    RELEASE_AGENT,
    RELEASES,
    REPO,
    REVIEWER_AGENT,
    SWITCH_EXEC,
    spec_proposal,
)

from sindarin.agents import coder, product, release, reviewer
from sindarin.agents.base import (
    REFUSAL,
    RoleAgent,
    ScriptedModel,
    Tool,
    Turn,
    check_message,
    human_turns,
    strict_tool,
    tool_history,
)
from sindarin.agents.github_client import SimGitHub
from sindarin.agents.roles import RoleContext, build_agent
from sindarin.executor.core import parse_proposal, parse_review, spec_instructions
from sindarin.schema import PROPOSAL_FENCE, extract_fences
from sindarin.switch.events import MessageEvent

REPO_ROOT = Path(__file__).resolve().parents[2]


def _ctx(role: str, self_id: str, tmp_path: Path, github: bool = True) -> RoleContext:
    gh = SimGitHub(tmp_path / "sim.json", REPO) if github else None
    return RoleContext(
        role=role,
        repo_root=REPO_ROOT,
        policy_path=FIXTURES / "policy.yaml",
        users_path=FIXTURES / "users.yaml",
        self_id=self_id,
        github=gh,
    )


def _ev(room, sender, body, *, mid="m1", thread=None, name="x") -> MessageEvent:
    return MessageEvent(
        sequence=1,
        room_id=room,
        addressed=True,
        sender=sender,
        sender_name=name,
        message_id=mid,
        body=body,
        timestamp=0,
        thread_id=thread,
    )


# -- base ------------------------------------------------------------------------------------------


def test_guard_only_passes_validator_fences():
    out = '@sindarin-github proposal\n```sindarin-proposal\n{"a":1}\n```'
    assert (
        RoleAgent.guard("caption\n" + out + "\nYour call.", {out})
        == "caption\n" + out + "\nYour call."
    )
    forged = '```sindarin-proposal\n{"a":2}\n```'
    assert RoleAgent.guard("look\n" + forged, {out}) == REFUSAL
    assert RoleAgent.guard("look\n" + out + "\n" + forged, {out}) == REFUSAL
    assert RoleAgent.guard("plain text", set()) == "plain text"
    assert RoleAgent.guard("", set()) == "I have nothing to add."


def test_hand_written_fence_from_the_model_is_refused(tmp_path):
    ctx = _ctx("product", PRODUCT_AGENT, tmp_path, github=False)
    forged = (
        '@sindarin-switch proposal\n```sindarin-proposal\n{"schema":"sindarin/proposal/v2"}\n```'
    )
    agent = build_agent(product, ctx, ScriptedModel(lambda m, s: ("text", forged)))
    assert agent.respond([Turn(ISAAC, "Isaac", "hi")], "[h]") == REFUSAL


def test_check_message_rules():
    assert check_message("@Isaac On it. One line.") == []
    assert check_message("a\nb\nc\nd @x") == ["4 lines, max 3"]
    assert "no @mention" in check_message("no mention here")
    assert any("artifact ids" in p for p in check_message("@x PR-41 and REL-1"))
    assert "headers or bullets in chat" in check_message("@x\n- bullet")
    assert "an agent never says approved" in check_message("@x approved!")
    assert check_message("@x Your call. approved means nothing until the executor says so") == []


def test_tool_history_and_human_turns():
    messages = [
        {"role": "user", "content": "[header]\nIsaac: hello"},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t1", "name": "f", "input": {"a": 1}}],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "ok", "is_error": False}
            ],
        },
    ]
    assert tool_history(messages) == [("f", {"a": 1}, "ok", True)]
    assert human_turns(messages) == ["Isaac: hello"]


def test_strict_tool_shape():
    spec = strict_tool("t", "d", {"a": {"type": "string"}}, ["a"])
    assert spec["strict"] is True and spec["input_schema"]["additionalProperties"] is False
    for module in (product, coder, reviewer, release):
        ctx = _ctx(module.NAME, "@x:t", Path("/tmp"), github=False)
        for tool in module.tools(ctx):
            assert isinstance(tool, Tool) and tool.spec["strict"] is True


# -- product ---------------------------------------------------------------------------------------


def test_product_asks_then_proposes_and_routes_to_product_room(tmp_path):
    ctx = _ctx("product", PRODUCT_AGENT, tmp_path, github=False)
    agent = build_agent(product, ctx, ScriptedModel(product.fake_step(ctx)))
    ask = _ev(CUSTOMER, ISAAC, "We need a CSV export of orders.", mid="m-ask", name="Isaac")
    thread = [Turn(ISAAC, "Isaac", ask.body, room_id=CUSTOMER, message_id="m-ask")]
    assert product.trigger(ask, ctx, thread) and not product.trigger(
        _ev(FEATURE, ISAAC, "x"), ctx, thread
    )
    first = agent.respond(thread, product.header(ask, ctx, thread))
    assert first.startswith("@Isaac On it.") and check_message(first) == []
    thread += [
        Turn(PRODUCT_AGENT, "product", first, True),
        Turn(
            ISAAC,
            "Isaac",
            "all fields, 10k is fine",
            room_id=CUSTOMER,
            message_id="m-2",
            thread_id="m-ask",
        ),
    ]
    reply = _ev(CUSTOMER, ISAAC, "all fields", mid="m-2", thread="m-ask", name="Isaac")
    second = agent.respond(thread, product.header(reply, ctx, thread))
    proposal, err = parse_proposal(second)
    assert err is None and proposal.kind.value == "spec" and proposal.ask == "m-ask"
    assert proposal.body.engineer == ISAAC and proposal.body.repo == REPO
    assert check_message(second) == [] and second.rstrip().endswith("with the id above.")
    outs = product.route(second, reply, ctx, thread)
    assert [(o.room_id, o.thread_id) for o in outs] == [(CUSTOMER, "m-ask"), (PRODUCT, None)]
    assert outs[1].text.startswith("@sindarin-switch proposal\n```sindarin-proposal")
    # a `changes` from the executor in #product → re-proposal in the same thread
    back = _ev(
        PRODUCT,
        SWITCH_EXEC,
        "**SPEC-1** sent back by Leonardo: tighten criterion 2 @product revise",
        mid="m-ch",
        thread="m-spec",
    )
    thread2 = [
        Turn(PRODUCT_AGENT, "product", outs[1].text, True, PRODUCT, "m-spec"),
        Turn(
            SWITCH_EXEC,
            "sindarin-switch",
            back.body,
            room_id=PRODUCT,
            message_id="m-ch",
            thread_id="m-spec",
        ),
    ]
    assert product.trigger(back, ctx, thread2)
    third = agent.respond(thread2, product.header(back, ctx, thread2))
    p3, err = parse_proposal(third)
    assert err is None and any("revised" in s for s in p3.body.scope_out)
    assert product.route(third, back, ctx, thread2)[0].thread_id == "m-spec"


def test_product_context_carries_policy_users_roadmap(tmp_path):
    ctx = _ctx("product", PRODUCT_AGENT, tmp_path, github=False)
    text = product.context_text(ctx)
    assert (
        "policy.yaml" in text and "users.yaml" in text and "roadmap" in text and "Reporting" in text
    )


# -- coder and reviewer ----------------------------------------------------------------------------


def test_coder_opens_pr_proposes_and_fixes(tmp_path):
    ctx = _ctx("coder", CODER_AGENT, tmp_path)
    brief = f"**SPEC-1** instructions\n{spec_instructions(spec_proposal())}"
    ctx.memory["briefs"] = {FEATURE: brief}
    agent = build_agent(coder, ctx, ScriptedModel(coder.fake_step(ctx)), room_id=FEATURE)
    kickoff = _ev(
        FEATURE,
        SWITCH_EXEC,
        "@coder SPEC-1 is yours. " + brief,
        mid="m-kick",
        name="sindarin-switch",
    )
    thread = [
        Turn(SWITCH_EXEC, "sindarin-switch", kickoff.body, room_id=FEATURE, message_id="m-kick")
    ]
    assert coder.trigger(kickoff, ctx, thread)
    assert not coder.trigger(_ev(PRODUCT, SWITCH_EXEC, "@coder hi"), ctx, thread), (
        "never outside a feature room"
    )
    text = agent.respond(thread, coder.header(kickoff, ctx, thread))
    proposal, err = parse_proposal(text)
    assert (
        err is None
        and proposal.kind.value == "pr"
        and proposal.body.number == 41
        and proposal.body.spec == "SPEC-1"
    )
    pr = ctx.github.pull(41)
    assert pr.branch == "feat/SPEC-1" and proposal.snapshot_hint == pr.head_sha
    assert check_message(text) == [] and "`@sindarin-github approve PR-41`" in text
    outs = coder.route(text, kickoff, ctx, thread)
    assert [(o.room_id, o.thread_id) for o in outs] == [(FEATURE, "m-kick"), (FEATURE, None)]
    assert "GET /orders.csv" in coder.context_text(ctx, FEATURE)

    # the reviewer finds criterion 2 unmet → push and re-propose in the PR thread
    report = _ev(
        FEATURE,
        REVIEWER_AGENT,
        "Review of PR-41: **3 of 4** criteria met.\n@coder criterion 2 not met: no test for the empty export",
        mid="m-rev",
        thread="m-pr",
        name="reviewer",
    )
    thread2 = [
        Turn(CODER_AGENT, "coder", outs[1].text, True, FEATURE, "m-pr"),
        Turn(
            REVIEWER_AGENT,
            "reviewer",
            report.body,
            room_id=FEATURE,
            message_id="m-rev",
            thread_id="m-pr",
        ),
    ]
    report_unaddressed = MessageEvent(**{**report.__dict__, "addressed": False})
    assert coder.trigger(report_unaddressed, ctx, thread2), "the reviewer's @coder line is enough"
    fix = agent.respond(thread2, coder.header(report, ctx, thread2))
    p2, err = parse_proposal(fix)
    assert err is None and p2.snapshot_hint == ctx.github.pull(41).head_sha != pr.head_sha
    assert coder.route(fix, report, ctx, thread2)[0].thread_id == "m-pr"


def test_reviewer_reports_three_then_four(tmp_path):
    ctx = _ctx("reviewer", REVIEWER_AGENT, tmp_path)
    ctx.memory["briefs"] = {
        FEATURE: f"**SPEC-1** instructions\n{spec_instructions(spec_proposal())}"
    }
    pr = ctx.github.open_pull(branch="feat/SPEC-1", title="CSV export of orders", body="")
    from sindarin.propose import build_pr, render

    fence = render(
        build_pr(
            ctx.policy(),
            number=pr.number,
            title=pr.title,
            branch=pr.branch,
            spec="SPEC-1",
            repo=REPO,
            head_sha=pr.head_sha,
            justification="x",
        )
    )
    ev = _ev(FEATURE, CODER_AGENT, fence, mid="m-pr", name="coder")
    ev = MessageEvent(**{**ev.__dict__, "addressed": False})
    thread = [Turn(CODER_AGENT, "coder", fence, room_id=FEATURE, message_id="m-pr")]
    assert reviewer.trigger(ev, ctx, thread)
    assert not reviewer.trigger(MessageEvent(**{**ev.__dict__, "sender": ISAAC}), ctx, thread), (
        "only the coder's fence"
    )
    agent = build_agent(reviewer, ctx, ScriptedModel(reviewer.fake_step(ctx)), room_id=FEATURE)
    text = agent.respond(thread, reviewer.header(ev, ctx, thread))
    review, err = parse_review(text)
    assert err is None and review.pr == f"PR-{pr.number}" and review.head_sha == pr.head_sha
    assert (review.met, review.total) == (3, 4) and "@coder criterion 2 not met" in text
    assert (
        check_message(text) == [] and reviewer.route(text, ev, ctx, thread)[0].thread_id == "m-pr"
    )
    ctx.github.push_fix(pr.number, "fix")
    text = agent.respond(thread, reviewer.header(ev, ctx, thread))
    review, _ = parse_review(text)
    assert (
        review.met == 4
        and "Your call, @Isaac" in text
        and "`@sindarin-github approve PR-41`" in text
    )


# -- release ---------------------------------------------------------------------------------------


def test_release_drafts_then_tells_the_customer(tmp_path):
    ctx = _ctx("release", RELEASE_AGENT, tmp_path)
    ctx.memory["asks"] = {"SPEC-1": "m-ask-1"}
    ctx.memory["asker"] = ISAAC
    gh = ctx.github
    pr = gh.open_pull(branch="feat/SPEC-1", title="CSV export of orders [SPEC-1]", body="")
    gh.fake.pulls[pr.number].merged = True
    gh.fake.pulls[pr.number].merged_seq = 1
    gh.fake._save()
    agent = build_agent(release, ctx, ScriptedModel(release.fake_step(ctx)), room_id=RELEASES)
    ask = _ev(RELEASES, JOHN, "@release draft the Friday release", mid="m-r", name="John")
    thread = [Turn(JOHN, "John", ask.body, room_id=RELEASES, message_id="m-r")]
    assert release.trigger(ask, ctx, thread) and not release.trigger(
        _ev(PRODUCT, JOHN, "@release hi"), ctx, thread
    )
    text = agent.respond(thread, release.header(ask, ctx, thread))
    proposal, err = parse_proposal(text)
    assert err is None and proposal.kind.value == "release" and proposal.body.tag == "v1.0.0"
    assert (
        proposal.body.target_sha == gh.main_head()
        and proposal.body.specs == ["SPEC-1"]
        and proposal.body.asks == ["m-ask-1"]
    )
    assert "Your call, @John" in text and check_message(text) == []
    outs = release.route(text, ask, ctx, thread)
    assert [(o.room_id, o.thread_id) for o in outs] == [(RELEASES, "m-r"), (RELEASES, None)]
    # the executor publishes → the customer is told in the ask thread
    ctx.memory["releases"] = {"REL-1": ["SPEC-1"]}
    done = _ev(
        RELEASES,
        GITHUB_EXEC,
        f"**REL-1** approved by John · published https://github.com/{REPO}/releases/tag/v1.0.0 · verified at **12:00:00 UTC**.",
        mid="m-pub",
        thread="m-rel",
        name="sindarin-github",
    )
    done = MessageEvent(**{**done.__dict__, "addressed": False})
    thread2 = [
        Turn(GITHUB_EXEC, "sindarin-github", done.body, room_id=RELEASES, message_id="m-pub")
    ]
    assert release.trigger(done, ctx, thread2)
    note = agent.respond(thread2, release.header(done, ctx, thread2))
    assert (
        note.startswith("@Isaac")
        and "SPEC-1" in note
        and "v1.0.0" in note
        and check_message(note) == []
    )
    outs = release.route(note, done, ctx, thread2)
    assert [(o.room_id, o.thread_id) for o in outs] == [(CUSTOMER, "m-ask-1")]
    assert release.next_tag("v1.4.2") == "v1.5.0" and release.next_tag(None) == "v1.0.0"


def test_agent_md_files_exist_and_name_the_rules(repo_root):
    for role in ("product", "coder", "reviewer", "release"):
        text = (repo_root / "agents" / role / "AGENT.md").read_text()
        assert "Never approve" in text or "never approve" in text.lower()
        assert "Your call" in text
    assert json.loads(extract_fences("```sindarin-proposal\n{}\n```", PROPOSAL_FENCE)[0]) == {}
    with pytest.raises(ValueError):
        parse_proposal("```sindarin-proposal\n{}\n```")[1] is None or (_ for _ in ()).throw(
            ValueError()
        )
