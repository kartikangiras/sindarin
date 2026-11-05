"""`release` (DDD §6.4): drafts release notes naming each spec and the customer ask it answers,
proposes the tag; after the executor's `published` message, tells the customer in the original
ask thread. Never tags, never approves."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from sindarin.agents.base import Outgoing, Tool, Turn, human_turns, strict_tool, tool_history
from sindarin.agents.roles import RoleContext, split_fence_message, spoken_to
from sindarin.names import GITHUB_EXECUTOR
from sindarin.propose import build_release, render
from sindarin.schema import Kind
from sindarin.switch.events import MessageEvent

NAME = "release"
PUBLISHED_RE = re.compile(r"\*\*(REL-\d+)\*\* approved by (.+?) · published (\S+)")
SPEC_RE = re.compile(r"\bSPEC-\d+\b")

PROPOSE_RELEASE = strict_tool(
    "propose_release",
    "Validate a release proposal and return the exact block to post in #releases, or a one-line "
    "reason it is not allowed. The target sha is the head of main from the header; the notes "
    "name each spec and the customer ask it answers.",
    {
        "tag": {"type": "string", "description": "vX.Y.Z"},
        "target_sha": {"type": "string"},
        "notes": {"type": "string", "description": "markdown, under 2000 characters"},
        "specs": {"type": "array", "items": {"type": "string"}},
        "asks": {
            "type": "array",
            "items": {"type": "string"},
            "description": "ask thread root ids",
        },
        "justification": {"type": "string"},
    },
    ["tag", "target_sha", "notes", "specs", "asks", "justification"],
)


def tools(ctx: RoleContext) -> list[Tool]:
    def run(args: dict[str, Any]) -> tuple[str, bool]:
        if ctx.github is None:
            return "no GitHub client configured", False
        try:
            p = build_release(
                ctx.policy(),
                tag=str(args.get("tag", "")),
                target_sha=str(args.get("target_sha", "")),
                notes=str(args.get("notes", "")),
                specs=[str(s) for s in args.get("specs") or []],
                asks=[str(a) for a in args.get("asks") or []],
                repo=ctx.github.repo,
                justification=str(args.get("justification", "")),
            )
        except ValueError as exc:
            return f"Not allowed: {exc}", False
        return render(p), True

    return [Tool(PROPOSE_RELEASE, run)]


def context_text(ctx: RoleContext, room_id: str = "") -> str:
    policy = ctx.policy_path.read_text(encoding="utf-8").strip()
    asks = ctx.memory.get("asks", {})
    lines = "\n".join(f"- {spec}: ask {ask}" for spec, ask in sorted(asks.items())) or "- none seen"
    return f"# Specs and the asks they answer\n{lines}\n\n# policy.yaml\n```yaml\n{policy}\n```"


def trigger(ev: MessageEvent, ctx: RoleContext, thread: list[Turn]) -> bool:
    if ev.sender == ctx.self_id or ctx.room_key(ev.room_id) != "releases":
        return False
    if spoken_to(ev, ctx, thread):
        return True
    return ev.sender in ctx.executor_ids() and PUBLISHED_RE.search(ev.body or "") is not None


def header(ev: MessageEvent, ctx: RoleContext, thread: list[Turn]) -> str:
    gh = ctx.github
    published = PUBLISHED_RE.search(ev.body or "")
    asks = ctx.memory.get("asks", {})
    if published:
        rel, _who, url = published.groups()
        specs = _specs_of(ctx, rel, ev)
        [asks[s] for s in specs if s in asks]
        ctx.policy().room_id("customer")
        asker = ctx.memory.get("asker", "")
        contact = f"{ctx.name_of(asker)}, mention as @{ctx.name_of(asker)}" if asker else "unknown"
        return (
            f"[Room {ctx.room_name(ev.room_id)} · {rel} published at {url} · "
            f"specs {', '.join(specs) or '?'} · customer contact {contact} · "
            f"your reply goes to the customer's original ask thread; write one line to them]"
        )
    merged = gh.merged_pulls() if gh else []
    since = gh.latest_tag() if gh else None
    rows = "; ".join(f"#{p.number} {p.title} ({_spec_of(p)})" for p in merged)
    return (
        f"[Room {ctx.room_name(ev.room_id)} · thread root {ev.thread_id or ev.message_id} · "
        f"main head {gh.main_head() if gh else '?'} · last tag {since or 'none'} · "
        f"merged since: {rows or 'nothing'} · "
        f"asks {asks}]"
    )


def route(text: str, ev: MessageEvent, ctx: RoleContext, thread: list[Turn]) -> list[Outgoing]:
    published = PUBLISHED_RE.search(ev.body or "")
    if published:
        rel = published.group(1)
        specs = _specs_of(ctx, rel, ev)
        asks = ctx.memory.get("asks", {})
        ask = next((asks[s] for s in specs if s in asks), None)
        customer = ctx.policy().room_id("customer")
        if customer:
            return [Outgoing(customer, text, ask)]
        return [Outgoing(ev.room_id, text, ev.thread_id or ev.message_id)]
    before, fence, after = split_fence_message(text)
    if not fence:
        return [Outgoing(ev.room_id, text, ev.thread_id or ev.message_id)]
    outs = []
    if before:
        outs.append(Outgoing(ev.room_id, before, ev.thread_id or ev.message_id))
    outs.append(Outgoing(ev.room_id, "\n".join(x for x in (fence, after) if x), None))
    return outs


def _spec_of(p) -> str:
    return next(iter(SPEC_RE.findall(f"{p.title} {p.branch} {p.body}")), "?")


def _specs_of(ctx: RoleContext, rel: str, ev: MessageEvent) -> list[str]:
    """The specs a published release carries: from the proposal fence in the same thread when
    known, else from what was learned when the executor said `pending`."""
    by_thread = ctx.memory.get("release_by_thread", {})
    return list(by_thread.get(ev.thread_id or "") or ctx.memory.get("releases", {}).get(rel, []))


def next_tag(last: str | None) -> str:
    m = re.match(r"^v(\d+)\.(\d+)\.(\d+)$", last or "")
    if not m:
        return "v1.0.0"
    major, minor, _patch = (int(x) for x in m.groups())
    return f"v{major}.{minor + 1}.0"


# -- the stand-in --------------------------------------------------------------------------------


def fake_step(ctx: RoleContext) -> Callable[[list[dict[str, Any]], str], tuple[Any, ...]]:
    def step(messages: list[dict[str, Any]], system: str) -> tuple[Any, ...]:
        header_line = messages[0]["content"].splitlines()[0] if messages else ""
        history = tool_history(messages)
        if "published at" in header_line:
            m = re.search(r"(REL-\d+) published at (\S+) · specs ([^·]+) ·", header_line)
            url, specs = (m.group(2), m.group(3).strip()) if m else ("", "")
            tag = url.rsplit("/", 1)[-1] if url else "the new release"
            customer = _customer_mention(ctx)
            return (
                "text",
                f"{customer} your CSV export is live: {specs} shipped in **{tag}**, "
                f"release notes at {url}. Thank you for asking for it.",
            )
        if history:
            name, args, out, ok = history[-1]
            if ok:
                approver = _approver_mention(ctx)
                return (
                    "text",
                    "Release notes drafted from the merged pull requests and the asks they "
                    f"answer.\n{out}\n"
                    f"Your call, {approver}: `@{GITHUB_EXECUTOR} approve REL-n` with the id above.",
                )
            return ("text", f"I can't propose that release: {out}")
        head = _between(header_line, "main head ", " ·")
        last = _between(header_line, "last tag ", " ·")
        merged = _between(header_line, "merged since: ", " ·")
        if not merged or merged == "nothing":
            who = human_turns(messages)
            asker = who[0].split(":", 1)[0] if who else "there"
            since = "the start" if last in ("", "none") else last
            return ("text", f"@{asker} nothing merged since {since}; nothing to release.")
        specs = sorted(set(SPEC_RE.findall(merged))) or ["SPEC-1"]
        asks_map = ctx.memory.get("asks", {})
        asks = [asks_map[s] for s in specs if s in asks_map]
        tag = next_tag(None if last == "none" else last)
        notes = "\n".join(
            f"- {s}: {merged}" + (f" (asked in {asks_map[s]})" if s in asks_map else "")
            for s in specs
        )
        return (
            "tool",
            "propose_release",
            {
                "tag": tag,
                "target_sha": head,
                "notes": notes[:1900],
                "specs": specs,
                "asks": asks,
                "justification": "release manager asked for the release",
            },
        )

    return step


def _between(text: str, start: str, end: str) -> str:
    if start not in text:
        return ""
    rest = text.split(start, 1)[1]
    return rest.split(end, 1)[0].strip() if end in rest else rest.rstrip("]").strip()


def _approver_mention(ctx: RoleContext) -> str:
    p = ctx.policy()
    rule = next(iter(p.rules_for(Kind.RELEASE)), None)
    members = p.group_members(rule.approvers) if rule else []
    return ctx.mention(members[0]) if members else "@release-manager"


def _customer_mention(ctx: RoleContext) -> str:
    asker = ctx.memory.get("asker")
    return ctx.mention(asker) if asker else "@customer"
