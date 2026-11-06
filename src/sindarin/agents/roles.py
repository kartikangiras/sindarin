"""What every role shares (DDD §6): the context a role runs in and the shape of a role module."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from sindarin.agents.base import Outgoing, RoleAgent, Tool, Turn
from sindarin.ledger import Ledger
from sindarin.policy import Policy, load_policy
from sindarin.switch.events import MessageEvent
from sindarin.users import Users, load_users

PLAN_ID_RE = re.compile(r"\bGOALS-\d+\b")
BUDGET_ID_RE = re.compile(r"\bBUDGET-\d+\b")
CLOSE_ID_RE = re.compile(r"\bCLOSE-\d+\b")
# the goals or the plan posted into a room by sindarin-switch (its brief): context, never a call
BRIEF_RE = re.compile(r"\*\*((?:GOALS|CLOSE)-\d+)\*\* instructions", re.IGNORECASE)
PENDING_RE = re.compile(r"\*\*((?:GOALS|BUDGET|ENTRIES|CLOSE|PAY|LOCK|DEAL)-\d+)\*\* pending")


@dataclass
class RoleContext:
    role: str
    repo_root: Path
    policy_path: Path
    users_path: Path
    self_id: str
    ledger: Ledger | None = None  # read-only; every number an agent shows comes from it
    memory: dict[str, Any] = field(default_factory=dict)
    room_names: dict[str, str] = field(
        default_factory=lambda: {
            "finance": "#acme-finance",
            "planning": "#planning",
            "payments": "#payments",
            "sales": "#sales",
        }
    )

    def policy(self) -> Policy:
        return load_policy(self.policy_path)

    def users(self) -> Users:
        return load_users(self.users_path)

    def room_key(self, room_id: str) -> str | None:
        return self.policy().room_key(room_id)

    def room_name(self, room_id: str) -> str:
        key = self.room_key(room_id)
        if key:
            return self.room_names.get(key, f"#{key}")
        return self.memory.get("room_names", {}).get(room_id, "this room")

    def is_period_room(self, room_id: str) -> bool:
        return bool(room_id) and self.room_key(room_id) is None

    def executor_ids(self) -> set[str]:
        return {pid for pid in self.policy().executors.values() if pid}

    def agent_ids(self) -> set[str]:
        return set(self.policy().agent_ids)

    def is_human(self, participant_id: str) -> bool:
        return bool(participant_id) and participant_id not in self.agent_ids()

    def name_of(self, participant_id: str) -> str:
        p = self.policy()
        return (
            self.users().name_for(participant_id)
            or p.role_of(participant_id)
            or p.executor_of(participant_id)
            or participant_id
        )

    def mention(self, participant_id: str) -> str:
        """`@Name` for a person, `@role` for an agent. Display only; never an authorization."""
        return f"@{self.name_of(participant_id)}"

    def brief(self, room_id: str) -> str:
        return self.memory.get("briefs", {}).get(room_id, "")


class Role(Protocol):
    NAME: str

    def tools(self, ctx: RoleContext) -> list[Tool]: ...
    def context_text(self, ctx: RoleContext, room_id: str) -> str: ...
    def trigger(self, ev: MessageEvent, ctx: RoleContext, thread: list[Turn]) -> bool: ...
    def header(self, ev: MessageEvent, ctx: RoleContext, thread: list[Turn]) -> str: ...
    def route(
        self, text: str, ev: MessageEvent, ctx: RoleContext, thread: list[Turn]
    ) -> list[Outgoing]: ...
    def fake_step(
        self, ctx: RoleContext
    ) -> Callable[[list[dict[str, Any]], str], tuple[Any, ...]]: ...


def spoken_to(ev: MessageEvent, ctx: RoleContext, thread: list[Turn]) -> bool:
    """A person mentioned me, or answered in a thread I already took part in (people do not
    re-mention an agent inside its own thread; verified live 2026-11-05)."""
    if ev.sender == ctx.self_id or not ctx.is_human(ev.sender):
        return False
    if ev.addressed:
        return True
    return bool(ev.thread_id) and any(
        t.from_self and t.message_id != ev.message_id for t in thread[:-1]
    )


def tool_facts_text(ctx: RoleContext, room_id: str) -> str:
    """What this agent's tools did in the room so far (base.fact_line), for the next turn."""
    facts = ctx.memory.get("tool_facts", {}).get(room_id, [])
    if not facts:
        return ""
    lines = "\n".join(f"- {f}" for f in facts[-12:])
    return (
        "# What your tools did in this room (facts; your earlier posts here came from these)\n"
        + lines
    )


def build_agent(role: Role, ctx: RoleContext, model_call, *, room_id: str = "") -> RoleAgent:
    agent_md = ctx.repo_root / "agents" / role.NAME / "AGENT.md"
    return RoleAgent(
        role=role.NAME,
        agent_md=agent_md,
        tools=role.tools(ctx),
        model_call=model_call,
        context=lambda: "\n\n".join(
            p for p in (role.context_text(ctx, room_id), tool_facts_text(ctx, room_id)) if p
        ),
        max_turns=getattr(role, "MAX_TURNS", 8),
    )


def split_fence_message(text: str) -> tuple[str, str, str]:
    """(before, fence block with its mention line, after) for a rendered proposal/review."""
    m = re.search(
        r"(?:^|\n)(@sindarin-[a-z]+ (?:proposal|review)\n```sindarin-[a-z]+\n.*?\n```)",
        text,
        re.DOTALL,
    )
    if not m:
        return text, "", ""
    before = text[: m.start(1)].strip()
    after = text[m.end(1) :].strip()
    return before, m.group(1), after
