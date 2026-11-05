"""`product` (DDD §6.1): reads the ask, asks at most two questions, drafts the spec in the
working-backwards shape, proposes it in #product through one strict tool. Never approves."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

from sindarin.agents.base import Outgoing, Tool, Turn, human_turns, strict_tool, tool_history
from sindarin.agents.roles import RoleContext, split_fence_message, spoken_to
from sindarin.names import SWITCH_EXECUTOR
from sindarin.propose import build_spec, render
from sindarin.schema import Kind, utcnow
from sindarin.switch.events import MessageEvent

NAME = "product"

PROPOSE_SPEC = strict_tool(
    "propose_spec",
    "Validate one product spec against policy.yaml and return the exact block to post in "
    "#product, or a one-line reason it is not allowed. This is the only way a spec can be "
    "proposed. The engineer is a participant id from the header, never a name.",
    {
        "title": {"type": "string"},
        "press_release": {"type": "string", "description": "one sentence"},
        "faq": {"type": "array", "items": {"type": "string"}, "description": "exactly 3"},
        "criteria": {
            "type": "array",
            "items": {"type": "string"},
            "description": "3 to 5, each testable",
        },
        "scope_in": {"type": "array", "items": {"type": "string"}},
        "scope_out": {"type": "array", "items": {"type": "string"}},
        "target_date": {"type": "string", "description": "ISO date"},
        "engineer": {"type": "string", "description": "participant id of the engineer to assign"},
        "repo": {"type": "string", "description": "owner/repo from policy.yaml allowed_repos"},
        "slug": {"type": "string", "description": "feature room slug, e.g. feat-csv-export"},
        "ask": {"type": "string", "description": "the ask thread root message id from the header"},
        "justification": {"type": "string", "description": "one line, the customer's words"},
    },
    [
        "title",
        "press_release",
        "faq",
        "criteria",
        "scope_in",
        "scope_out",
        "target_date",
        "engineer",
        "repo",
        "slug",
        "ask",
        "justification",
    ],
)


def tools(ctx: RoleContext) -> list[Tool]:
    lists = ("faq", "criteria", "scope_in", "scope_out")

    def run(args: dict[str, Any]) -> tuple[str, bool]:
        kwargs = {
            k: args.get(k, [] if k in lists else "")
            for k in PROPOSE_SPEC["input_schema"]["required"]
        }
        try:
            p = build_spec(ctx.policy(), **kwargs)
        except (ValueError, TypeError) as exc:
            return f"Not allowed: {exc}", False
        return render(p), True

    return [Tool(PROPOSE_SPEC, run)]


def context_text(ctx: RoleContext, room_id: str = "") -> str:
    policy = ctx.policy_path.read_text(encoding="utf-8").strip()
    users = ctx.users_path.read_text(encoding="utf-8").strip()
    roadmap_path = ctx.repo_root / "agents" / "product" / "roadmap.yaml"
    roadmap = roadmap_path.read_text(encoding="utf-8").strip() if roadmap_path.exists() else ""
    return (
        f"# policy.yaml\n```yaml\n{policy}\n```\n\n# users.yaml\n```yaml\n{users}\n```\n\n"
        f"# roadmap.yaml\n```yaml\n{roadmap}\n```"
    )


def trigger(ev: MessageEvent, ctx: RoleContext, thread: list[Turn]) -> bool:
    if ev.sender == ctx.self_id:
        return False
    key = ctx.room_key(ev.room_id)
    if key not in ("customer", "product"):
        return False
    # people in either room (a mention, or an answer in my thread); the executor when it sends a
    # spec back (`changes`), which mentions @product
    if ev.addressed and ev.sender in ctx.executor_ids():
        return True
    return spoken_to(ev, ctx, thread)


def _engineers(ctx: RoleContext) -> list[tuple[str, str]]:
    p = ctx.policy()
    rule = next(iter(p.rules_for(Kind.SPEC)), None)
    group = rule.engineers if rule else "engineering"
    return [(pid, ctx.name_of(pid)) for pid in p.group_members(group)]


def header(ev: MessageEvent, ctx: RoleContext, thread: list[Turn]) -> str:
    root = thread[0] if thread else None
    key = ctx.room_key(ev.room_id) or "feature"
    engineers = ", ".join(f"{name}: {pid}" for pid, name in _engineers(ctx)) or "none configured"
    p = ctx.policy()
    repos = ", ".join(r for rule in p.rules_for(Kind.SPEC) for r in rule.allowed_repos)
    ask = root.message_id if root else (ev.thread_id or ev.message_id)
    who = (
        f"{ctx.name_of(root.sender)}, mention as @{ctx.name_of(root.sender)}; id {root.sender}"
        if root
        else ctx.name_of(ev.sender)
    )
    return (
        f"[Room {ctx.room_name(ev.room_id)} ({key}) · thread root {ask} · asked by {who} · "
        f"engineers {engineers} · repos {repos} · today {utcnow():%Y-%m-%d}]"
    )


def route(text: str, ev: MessageEvent, ctx: RoleContext, thread: list[Turn]) -> list[Outgoing]:
    before, fence, after = split_fence_message(text)
    root = ev.thread_id or ev.message_id
    if not fence:
        return [Outgoing(ev.room_id, text, root)]
    product_room = ctx.policy().room_id("product")
    if ctx.room_key(ev.room_id) == "product":
        # a revision after `changes`: same thread, same id
        return [Outgoing(ev.room_id, "\n".join(x for x in (before, fence, after) if x), root)]
    outs = []
    if before:
        outs.append(Outgoing(ev.room_id, before, root))
    outs.append(Outgoing(product_room, "\n".join(x for x in (fence, after) if x), None))
    return outs


# -- the stand-in (SINDARIN_MODEL=fake) ----------------------------------------------------------

DEMO_SPEC = {
    "title": "CSV export of orders",
    "press_release": (
        "Acme's finance team can download every order as a CSV file straight from the API."
    ),
    "faq": [
        "Who is it for? Acme's finance team, who reconcile orders weekly.",
        "What does it cost? Nothing; it ships in the API plan they already have.",
        "What about huge exports? Above 10,000 rows the API says so instead of timing out.",
    ],
    "criteria": [
        "GET /orders.csv returns text/csv",
        "a header row is present even when there are no orders",
        "exports above 10,000 rows return 413 with a message",
        "a test covers each of the above",
    ],
    "scope_in": ["CSV export endpoint", "header row", "size limit"],
    "scope_out": ["scheduled exports", "XLSX", "column selection"],
    "slug": "feat-csv-export",
}


def fake_step(ctx: RoleContext) -> Callable[[list[dict[str, Any]], str], tuple[Any, ...]]:
    def step(messages: list[dict[str, Any]], system: str) -> tuple[Any, ...]:
        history = tool_history(messages)
        if history:
            name, args, out, ok = history[-1]
            if ok:
                approver = next(iter(ctx.policy().group_members("pm")), "")
                who = ctx.mention(approver) if approver else "@pm"
                return (
                    "text",
                    f"Spec for the CSV export, drafted from the ask and validated.\n{out}\n"
                    f"Your call, {who}: `@{SWITCH_EXECUTOR} approve SPEC-n` with the id above.",
                )
            return ("text", f"I can't propose that: {out}")
        header_line = messages[0]["content"].splitlines()[0] if messages else ""
        turns = human_turns(messages)
        ask = _between(header_line, "thread root ", " ·")
        asker = _between(header_line, "asked by ", ",")
        engineer = _between(header_line, "engineers ", " ·")
        engineer_id = (
            engineer.split(": ", 1)[1].split(",", 1)[0].strip() if ": " in engineer else ""
        )
        repo = _between(header_line, "repos ", " ·").split(",", 1)[0].strip()
        in_product = "(product)" in header_line
        sent_back = any("sent back by" in t for t in turns)
        if in_product and not sent_back:
            return (
                "text",
                f"@{asker or 'there'} specs are proposed from the customer's ask thread; "
                "nothing to draft here.",
            )
        if (
            not in_product
            and len([t for t in turns if not t.startswith(("product:", "sindarin-"))]) < 2
        ):
            return (
                "text",
                f"@{asker or 'there'} On it. One question first: which fields must the CSV carry, "
                "and is a 10,000-row limit acceptable?",
            )
        spec = dict(DEMO_SPEC)
        if sent_back:
            note = next((t.split("sent back by", 1)[1] for t in turns if "sent back by" in t), "")
            spec["scope_out"] = [*spec["scope_out"], f"revised: {note.strip()[:80]}"]
        target = (utcnow() + timedelta(days=2)).date().isoformat()
        return (
            "tool",
            "propose_spec",
            {
                **spec,
                "target_date": target,
                "engineer": engineer_id,
                "repo": repo,
                "ask": ask,
                "justification": "Acme asked for a CSV export of their orders",
            },
        )

    return step


def _between(text: str, start: str, end: str) -> str:
    if start not in text:
        return ""
    rest = text.split(start, 1)[1]
    return rest.split(end, 1)[0] if end in rest else rest.rstrip("]")
