"""`reviewer` (DDD §6.3): checks every acceptance criterion against the PR and posts a report
the executor stores against the PR and its head sha. Its pass is a report, never an approval."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from sindarin.agents.base import Outgoing, Tool, Turn, strict_tool, tool_history
from sindarin.agents.roles import RoleContext, spoken_to
from sindarin.executor.core import parse_proposal
from sindarin.names import GITHUB_EXECUTOR
from sindarin.schema import REVIEW_SCHEMA, Kind, Review
from sindarin.switch.events import MessageEvent

NAME = "reviewer"

REPORT_REVIEW = strict_tool(
    "report_review",
    "Record a review of one pull request head: one result per acceptance criterion with one line "
    "of evidence (file and line, or test name). Returns the exact block to post. The head sha is "
    "the one from the header.",
    {
        "pr": {"type": "string", "description": "PR-n"},
        "head_sha": {"type": "string"},
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "criterion": {"type": "integer"},
                    "met": {"type": "boolean"},
                    "evidence": {"type": "string"},
                },
                "required": ["criterion", "met", "evidence"],
                "additionalProperties": False,
            },
        },
    },
    ["pr", "head_sha", "results"],
)


def tools(ctx: RoleContext) -> list[Tool]:
    def run(args: dict[str, Any]) -> tuple[str, bool]:
        results = list(args.get("results") or [])
        met = sum(1 for r in results if r.get("met"))
        try:
            review = Review(
                schema=REVIEW_SCHEMA,
                pr=str(args.get("pr", "")),
                head_sha=str(args.get("head_sha", "")),
                results=results,
                summary=f"{met} of {len(results)} met",
            )
        except Exception as exc:
            return f"Not a valid review: {str(exc).splitlines()[0][:160]}", False
        return f"@{GITHUB_EXECUTOR} review\n{review.render()}", True

    return [Tool(REPORT_REVIEW, run)]


def context_text(ctx: RoleContext, room_id: str = "") -> str:
    return f"# The spec (room instructions)\n{ctx.brief(room_id) or '(not seen yet)'}"


def trigger(ev: MessageEvent, ctx: RoleContext, thread: list[Turn]) -> bool:
    if ev.sender == ctx.self_id or not ctx.is_feature_room(ev.room_id):
        return False
    if spoken_to(ev, ctx, thread):
        return True
    if "```sindarin-proposal" not in (ev.body or ""):
        return False
    proposal, _ = parse_proposal(ev.body)
    return (
        proposal is not None
        and proposal.kind is Kind.PR
        and ev.sender == ctx.policy().proposer_id("coder")
    )


def header(ev: MessageEvent, ctx: RoleContext, thread: list[Turn]) -> str:
    proposal, _ = parse_proposal(ev.body or "")
    pr_line = ""
    if proposal is not None and proposal.kind is Kind.PR and ctx.github is not None:
        pr = ctx.github.pull(proposal.body.number)  # type: ignore[union-attr]
        if pr:
            changed = ctx.github.pull_files(pr.number)
            files = ", ".join(f.get("filename", "") for f in changed)
            pr_line = f" · PR-{pr.number} head {pr.head_sha} files {files}"
            diff = "\n".join(f"--- {f.get('filename', '')}\n{f.get('patch', '')}" for f in changed)[
                :6000
            ]
            if diff:
                pr_line += f" · diff follows the header]\n{diff}\n["
    criteria = re.findall(r"^\d+\. (.+)$", ctx.brief(ev.room_id), re.MULTILINE)
    crit = " | ".join(f"{i + 1}. {c}" for i, c in enumerate(criteria))
    return (
        f"[Room {ctx.room_name(ev.room_id)} (feature) · thread root "
        f"{ev.thread_id or ev.message_id}{pr_line} · criteria {crit}]"
    )


def route(text: str, ev: MessageEvent, ctx: RoleContext, thread: list[Turn]) -> list[Outgoing]:
    root = ev.thread_id or ev.message_id
    return [Outgoing(ev.room_id, text, root)]


# -- the stand-in --------------------------------------------------------------------------------


def fake_step(ctx: RoleContext) -> Callable[[list[dict[str, Any]], str], tuple[Any, ...]]:
    def step(messages: list[dict[str, Any]], system: str) -> tuple[Any, ...]:
        header_line = messages[0]["content"].splitlines()[0] if messages else ""
        history = tool_history(messages)
        m = re.search(r"PR-(\d+) head ([0-9a-f]{40}) files ([^·\]]*)", header_line)
        if history:
            name, args, out, ok = history[-1]
            if not ok:
                return ("text", f"I could not record the review: {out}")
            results = args["results"]
            met = sum(1 for r in results if r["met"])
            total = len(results)
            failed = [r for r in results if not r["met"]]
            engineer = _engineer_mention(ctx)
            if failed:
                tail = f"@coder criterion {failed[0]['criterion']} not met: {failed[0]['evidence']}"
            else:
                number = m.group(1) if m else "n"
                from sindarin.agents.coder import demo_agent_refusal

                nudge = " @coder nothing more to do." if demo_agent_refusal() else ""
                tail = (
                    f"All criteria met.{nudge} Your call, {engineer}: "
                    f"`@{GITHUB_EXECUTOR} approve PR-{number}`."
                )
            return (
                "text",
                f"Review of PR-{m.group(1) if m else 'n'}: **{met} of {total}** criteria met."
                f"\n{out}\n{tail}",
            )
        if not m:
            return ("text", "@coder I can only review a pull request proposal in this room.")
        number, head, files = m.group(1), m.group(2), m.group(3)
        full = "test_header_row_when_empty" in files or len(_pulls_heads(ctx, int(number))) > 1
        results = [
            {"criterion": 1, "met": True, "evidence": "tests/test_export.py::test_content_type"},
            {
                "criterion": 2,
                "met": full,
                "evidence": "tests/test_export.py::test_header_row_when_empty"
                if full
                else "tests/test_export.py::test_header_row_when_empty fails: no header row on "
                "an empty export",
            },
            {"criterion": 3, "met": True, "evidence": "tests/test_export.py::test_size_limit"},
            {"criterion": 4, "met": True, "evidence": "four tests in tests/test_export.py"},
        ]
        return (
            "tool",
            "report_review",
            {"pr": f"PR-{number}", "head_sha": head, "results": results},
        )

    return step


def _pulls_heads(ctx: RoleContext, number: int) -> list[str]:
    gh = ctx.github
    fake = getattr(gh, "fake", None)
    if fake is None:
        return []
    fake._load()
    pr = fake.pulls.get(number)
    return list(pr.heads) if pr else []


def _engineer_mention(ctx: RoleContext) -> str:
    p = ctx.policy()
    rule = next(iter(p.rules_for(Kind.PR)), None)
    members = p.group_members(rule.approvers) if rule else []
    return ctx.mention(members[0]) if members else "@engineer"
