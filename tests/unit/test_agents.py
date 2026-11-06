"""The three agents on the stand-in model (design v5 §4): one or two strict tools each, fences
only from the validator, message rules on every output, triggers by people and executors only,
numbers only from the ledger."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from validation.harness import (
    ANALYST_AGENT,
    BASIS,
    BOOKKEEPER_AGENT,
    DATA,
    FINANCE,
    FIXTURES,
    ISAAC,
    JOHN,
    LEDGER_EXEC,
    PERIOD,
    PERIOD_ROOM,
    PLANNER_AGENT,
    PLANNING,
    SWITCH_EXEC,
)

from sindarin.agents import analyst, bookkeeper, planner
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
from sindarin.agents.finance import (
    parse_money,
    period_from,
    plan_goals,
    report_period,
    standard_budget_lines,
)
from sindarin.agents.roles import RoleContext, build_agent
from sindarin.executor.core import parse_proposal, plan_instructions
from sindarin.ledger import Ledger
from sindarin.schema import PROPOSAL_FENCE, Kind, extract_fences
from sindarin.switch.events import MessageEvent

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def books(tmp_path):
    ledger = Ledger(tmp_path / "books.db")
    ledger.import_dir(DATA / BASIS)
    yield ledger
    ledger.close()


def _ctx(role: str, self_id: str, books: Ledger | None) -> RoleContext:
    return RoleContext(
        role=role,
        repo_root=REPO_ROOT,
        policy_path=FIXTURES / "policy.yaml",
        users_path=FIXTURES / "users.yaml",
        self_id=self_id,
        ledger=books,
    )


def _ev(room, sender, body, *, mid="m1", thread=None, name="x", addressed=True) -> MessageEvent:
    return MessageEvent(
        sequence=1,
        room_id=room,
        addressed=addressed,
        sender=sender,
        sender_name=name,
        message_id=mid,
        body=body,
        timestamp=0,
        thread_id=thread,
    )


# -- base ------------------------------------------------------------------------------------------


def test_guard_only_passes_validator_fences():
    out = '@sindarin-ledger proposal\n```sindarin-proposal\n{"a":1}\n```'
    assert (
        RoleAgent.guard("caption\n" + out + "\nYour call.", {out})
        == "caption\n" + out + "\nYour call."
    )
    forged = '```sindarin-proposal\n{"a":2}\n```'
    assert RoleAgent.guard("look\n" + forged, {out}) == REFUSAL
    assert RoleAgent.guard("look\n" + out + "\n" + forged, {out}) == REFUSAL
    assert RoleAgent.guard("plain text", set()) == "plain text"
    assert RoleAgent.guard("", set()) == "I have nothing to add."


def test_hand_written_fence_from_the_model_is_refused(books):
    ctx = _ctx("planner", PLANNER_AGENT, books)
    forged = (
        '@sindarin-switch proposal\n```sindarin-proposal\n{"schema":"sindarin/proposal/v3"}\n```'
    )
    agent = build_agent(planner, ctx, ScriptedModel(lambda m, s: ("text", forged)))
    assert agent.respond([Turn(ISAAC, "Isaac", "hi")], "[h]") == REFUSAL


def test_check_message_rules():
    assert check_message("@Isaac On it. One line.") == []
    assert check_message("a\nb\nc\nd @x") == ["4 lines, max 3"]
    assert "no @mention" in check_message("no mention here")
    assert any("artifact ids" in p for p in check_message("@x BUDGET-1 and ENTRIES-1"))
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


def test_strict_tool_shape(books):
    spec = strict_tool("t", "d", {"a": {"type": "string"}}, ["a"])
    assert spec["strict"] is True and spec["input_schema"]["additionalProperties"] is False
    for module in (analyst, planner, bookkeeper):
        ctx = _ctx(module.NAME, "@x:t", books)
        tools = module.tools(ctx)
        assert tools and all(isinstance(t, Tool) and t.spec["strict"] is True for t in tools)


# -- the finance helpers ---------------------------------------------------------------------------


def test_periods_goals_and_lines_come_from_the_ledger(books):
    assert period_from("plan November please", books) == PERIOD
    assert period_from("close October", books) == BASIS
    assert period_from("2026-11", None) == PERIOD and period_from("", books) == BASIS
    assert period_from("nothing", None) == ""
    assert report_period(PERIOD, books) == BASIS and report_period(BASIS, books) == BASIS
    assert parse_money("16,000.00 USD") == 1600000 and parse_money("cap 900") == 90000
    goals = plan_goals(books, PERIOD, BASIS)
    assert goals == {
        "revenue_target_cents": 9500000,
        "margin_target_bps": 8316,
        "cost_cap_cents": 1600000,
    }
    lines = standard_budget_lines(1600000)
    assert [(x["department"], x["amount_cents"]) for x in lines] == [
        ("engineering", 720000),
        ("sales", 400000),
        ("marketing", 240000),
        ("general-admin", 240000),
    ]
    assert sum(x["amount_cents"] for x in standard_budget_lines(900000)) <= 900000


# -- analyst ---------------------------------------------------------------------------------------


def test_analyst_reports_the_basis_month_and_tells_the_business(books):
    ctx = _ctx("analyst", ANALYST_AGENT, books)
    ask = _ev(FINANCE, ISAAC, "@analyst @planner plan November", mid="m-ask", name="Isaac")
    thread = [Turn(ISAAC, "Isaac", ask.body, room_id=FINANCE, message_id="m-ask")]
    assert analyst.trigger(ask, ctx, thread)
    assert not analyst.trigger(_ev(PLANNING, ISAAC, "@analyst hi"), ctx, thread), "finance only"
    assert not analyst.trigger(
        _ev(FINANCE, PLANNER_AGENT, "@analyst look", name="planner"), ctx, thread
    ), "never another agent"
    agent = build_agent(analyst, ctx, ScriptedModel(analyst.fake_step(ctx)), room_id=FINANCE)
    header = analyst.header(ask, ctx, thread)
    assert "period 2026-11 (open) · basis 2026-10" in header
    text = agent.respond(thread, header)
    assert text.startswith("@Isaac here is October 2026.")
    assert "**October 2026**" in text and "| Northwind Traders | 36,550.00 USD |" in text
    assert "target **85,000.00 USD**" in text and "cap **20,000.00 USD**" in text
    assert "Margin: **84.2%**" in text and "S-2026-1041 appears twice" in text
    assert "```" not in text and any(f.startswith("month_report(") for f in agent.tool_log)
    outs = analyst.route(text, ask, ctx, thread)
    assert [(o.room_id, o.thread_id) for o in outs] == [(FINANCE, "m-ask")]

    # the closing note: woken by sindarin-switch's stage line in the ask thread
    done = _ev(
        FINANCE,
        SWITCH_EXEC,
        f"**GOALS-1** done · ENTRIES-1 applied · period {PERIOD} is planned. "
        "@analyst tell the business.",
        mid="m-done",
        thread="m-ask",
        name="sindarin-switch",
    )
    fence = '{"basis":"2026-10","period":"2026-11"}'  # the planner's goals sit in the thread too
    thread2 = [
        *thread,
        Turn(PLANNER_AGENT, "planner", fence, thread_id="m-ask"),
        Turn(SWITCH_EXEC, "sindarin-switch", done.body, thread_id="m-ask"),
    ]
    assert analyst.trigger(done, ctx, thread2)
    note = agent.respond(thread2, analyst.header(done, ctx, thread2))
    assert note.startswith("@Isaac November 2026 is planned") and "register entries" in note
    assert [(o.room_id, o.thread_id) for o in analyst.route(note, done, ctx, thread2)] == [
        (FINANCE, "m-ask")
    ]


# -- planner ---------------------------------------------------------------------------------------


def test_planner_proposes_the_goals_then_the_budget(books):
    ctx = _ctx("planner", PLANNER_AGENT, books)
    ask = _ev(FINANCE, ISAAC, "@analyst @planner plan November", mid="m-ask", name="Isaac")
    thread = [Turn(ISAAC, "Isaac", ask.body, room_id=FINANCE, message_id="m-ask")]
    assert planner.trigger(ask, ctx, thread)
    assert not planner.trigger(
        _ev(FINANCE, ANALYST_AGENT, "@planner go", name="analyst"), ctx, thread
    ), "never another agent"
    assert not planner.trigger(
        _ev(FINANCE, JOHN, "looks fine", name="John", thread="m-ask", addressed=False), ctx, thread
    ), "chatter in the thread is not a call"
    agent = build_agent(planner, ctx, ScriptedModel(planner.fake_step(ctx)), room_id=FINANCE)
    text = agent.respond(thread, planner.header(ask, ctx, thread))
    proposal, err = parse_proposal(text)
    assert err is None and proposal.kind is Kind.GOALS and proposal.ask == "m-ask"
    assert proposal.body.period == PERIOD and proposal.body.basis == BASIS
    assert proposal.body.cost_cap_cents == 1600000 and proposal.body.revenue_target_cents == 9500000
    assert (
        "Revenue 89,905.00 USD across 12 invoices against a target of 85,000.00 USD"
        in (proposal.body.report[0])
    )
    assert "Your call, @John: `@sindarin-switch approve GOALS-n`" in text
    outs = planner.route(text, ask, ctx, thread)
    assert [(o.room_id, o.thread_id) for o in outs] == [(FINANCE, "m-ask")]

    # the budget: sindarin-ledger's stage line in #planning wakes it, with the cap in the line
    stage = _ev(
        PLANNING,
        LEDGER_EXEC,
        f"**GOALS-1** approved · revenue target 95,000.00 USD · cost cap 16,000.00 USD for "
        f"{PERIOD}. @planner propose the budget.",
        mid="m-stage",
        name="sindarin-ledger",
    )
    thread2 = [
        Turn(LEDGER_EXEC, "sindarin-ledger", stage.body, room_id=PLANNING, message_id="m-stage")
    ]
    assert planner.trigger(stage, ctx, thread2)
    assert not planner.trigger(
        _ev(PERIOD_ROOM, LEDGER_EXEC, "@planner x", name="sindarin-ledger"), ctx, thread2
    ), "finance and planning only"
    agent2 = build_agent(planner, ctx, ScriptedModel(planner.fake_step(ctx)), room_id=PLANNING)
    header = planner.header(stage, ctx, thread2)
    assert "goals GOALS-1" in header and "cost cap 16,000.00 USD" in header
    text2 = agent2.respond(thread2, header)
    budget, err = parse_proposal(text2)
    assert err is None and budget.kind is Kind.BUDGET and budget.body.goals == "GOALS-1"
    assert budget.body.total_cents == 1600000 and len(budget.snapshot_hint) == 64
    assert budget.snapshot_hint == books.actuals_digest(BASIS)
    assert "Your call, @John: `@sindarin-ledger approve BUDGET-n`" in text2
    assert [(o.room_id, o.thread_id) for o in planner.route(text2, stage, ctx, thread2)] == [
        (PLANNING, "m-stage")
    ]


# -- bookkeeper ------------------------------------------------------------------------------------


def test_bookkeeper_proposes_the_entries_from_the_recorded_budget(books):
    ctx = _ctx("bookkeeper", BOOKKEEPER_AGENT, books)
    goals_text = build_agent(
        planner,
        _ctx("planner", PLANNER_AGENT, books),
        ScriptedModel(planner.fake_step(_ctx("planner", PLANNER_AGENT, books))),
        room_id=FINANCE,
    ).respond(
        [Turn(ISAAC, "Isaac", "@analyst @planner plan November", room_id=FINANCE, message_id="m")],
        "[Room #acme-finance (finance) · thread root m · asked by Isaac, mention as @Isaac · "
        f"period {PERIOD} · goals - · cost cap -]",
    )
    goals, _ = parse_proposal(goals_text)
    books.record_budget(PERIOD, "BUDGET-1", standard_budget_lines(1600000))
    brief = f"**GOALS-1** instructions · approved by John\n{plan_instructions(goals)}"
    ctx.memory["briefs"] = {PERIOD_ROOM: brief}
    ready = _ev(
        PERIOD_ROOM,
        LEDGER_EXEC,
        f"**BUDGET-1** recorded · 4 lines, 16,000.00 USD under the cap · period {PERIOD} is "
        "ready for its entries. @bookkeeper propose the budget register entries.",
        mid="m-ready",
        name="sindarin-ledger",
    )
    thread = [
        Turn(LEDGER_EXEC, "sindarin-ledger", ready.body, room_id=PERIOD_ROOM, message_id="m-ready")
    ]
    assert bookkeeper.trigger(ready, ctx, thread)
    assert not bookkeeper.trigger(_ev(PERIOD_ROOM, JOHN, "status?", name="John"), ctx, thread)
    assert not bookkeeper.trigger(
        _ev(FINANCE, ISAAC, "@bookkeeper hi", name="Isaac"), ctx, thread
    ), "the plan room only"
    agent = build_agent(
        bookkeeper, ctx, ScriptedModel(bookkeeper.fake_step(ctx)), room_id=PERIOD_ROOM
    )
    header = bookkeeper.header(ready, ctx, thread)
    assert "goals GOALS-1" in header and "budget BUDGET-1" in header
    text = agent.respond(thread, header)
    entries, err = parse_proposal(text)
    assert err is None and entries.kind is Kind.ENTRIES and entries.body.goals == "GOALS-1"
    assert entries.body.budget == "BUDGET-1" and len(entries.body.lines) == 4
    assert entries.snapshot_hint == Ledger.budget_digest(books.budget(PERIOD))
    assert "Your call, @Leonardo: `@sindarin-ledger approve ENTRIES-n`" in text
    assert "The goals (room instructions)" in agent.system_prompt()


# -- shared behaviour ------------------------------------------------------------------------------


def test_agent_md_files_exist_and_name_the_rules(repo_root):
    for role in ("analyst", "planner", "bookkeeper"):
        text = (repo_root / "agents" / role / "AGENT.md").read_text()
        assert "approve" in text.lower() and "never" in text.lower()
    assert "Your call" in (repo_root / "agents" / "planner" / "AGENT.md").read_text()
    assert json.loads(extract_fences("```sindarin-proposal\n{}\n```", PROPOSAL_FENCE)[0]) == {}
    assert parse_proposal("```sindarin-proposal\n{}\n```")[1] is not None


def test_turn_budget_posts_the_last_validated_block(tmp_path):
    """The budget ran out right after a validator accepted a block (seen live 2026-11-05): the
    block is posted instead of the give-up line. With no validated block, the line stands."""
    block = '@sindarin-ledger proposal\n```sindarin-proposal\n{"kind": "budget"}\n```'
    md = tmp_path / "AGENT.md"
    md.write_text("You propose.")
    turn = Turn(sender="@switch-mattermost-x-isaac:localhost", sender_name="Isaac", body="go")

    proposing = Tool(strict_tool("propose", "always validates", {}, []), lambda args: (block, True))
    agent = RoleAgent(
        role="planner",
        agent_md=md,
        tools=[proposing],
        model_call=ScriptedModel(lambda m, s: ("tool", "propose", {})),
        max_turns=2,
    )
    assert agent.respond([turn], "header") == block
    assert any(f.startswith("propose(") for f in agent.tool_log)

    looking = Tool(strict_tool("look", "no fence", {}, []), lambda args: ("ok", True))
    agent2 = RoleAgent(
        role="planner",
        agent_md=md,
        tools=[looking],
        model_call=ScriptedModel(lambda m, s: ("tool", "look", {})),
        max_turns=2,
    )
    assert "turn budget" in agent2.respond([turn], "header")


def test_tool_facts_reach_the_next_turn(books):
    ctx = _ctx("planner", PLANNER_AGENT, books)
    ctx.memory["tool_facts"] = {PLANNING: ["propose_budget({}) → target budget:2026-11"]}
    agent = build_agent(planner, ctx, ScriptedModel(planner.fake_step(ctx)), room_id=PLANNING)
    prompt = agent.system_prompt()
    assert "What your tools did in this room" in prompt
    assert "Never call your earlier posts made up" in prompt
