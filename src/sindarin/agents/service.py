"""The agent process (DDD §6, §16.1): one Switch connection per role, one model call per trigger,
replies routed by the role. `sindarin-agent run <role>`."""

from __future__ import annotations

import asyncio
import logging
import os
from collections import defaultdict
from pathlib import Path
from types import ModuleType

from sindarin.agents import analyst, bookkeeper, planner, sales, treasury
from sindarin.agents.base import (
    RoleAgent,
    ScriptedModel,
    Turn,
    anthropic_model_call,
    is_fake_model,
)
from sindarin.agents.roles import BRIEF_RE, RoleContext, build_agent
from sindarin.executor.config import Env, load_config, process_env, root_env_path
from sindarin.ledger import Ledger
from sindarin.names import AGENT_ROLES, SWITCH_EXECUTOR
from sindarin.switch.client import ConnectionState, FatalEviction, SwitchClient, SwitchError
from sindarin.switch.events import ControlFrame, Frame, MessageEvent

log = logging.getLogger("sindarin.agents")

ROLES: dict[str, ModuleType] = {
    analyst.NAME: analyst,
    planner.NAME: planner,
    bookkeeper.NAME: bookkeeper,
    treasury.NAME: treasury,
    sales.NAME: sales,
}


class AgentService:
    def __init__(
        self, role: ModuleType, ctx: RoleContext, client: SwitchClient, env: Env, model_call
    ) -> None:
        self.role = role
        self.ctx = ctx
        self.client = client
        self.env = env
        self._brief_asked: set[str] = set()  # feature rooms where `brief` was requested once
        self.model_call = model_call
        self.threads: dict[str, list[Turn]] = defaultdict(list)
        self.queue: asyncio.Queue[MessageEvent] = asyncio.Queue()
        self.state = ConnectionState(connection_id=env.connection_id)

    # -- learning from the stream (no model involved) ---------------------------------------

    def _learn(self, ev: MessageEvent) -> None:
        """The one thing an agent keeps from the stream: the goals posted into a plan room by
        sindarin-switch (its brief). Everything else a turn needs comes from the executors on
        demand, so a restarted agent is no worse off than a running one."""
        body = ev.body or ""
        if ev.sender == self.ctx.policy().executor_id(SWITCH_EXECUTOR) and BRIEF_RE.search(body):
            self.ctx.memory.setdefault("briefs", {})[ev.room_id] = body

    async def on_frame(self, frame: Frame) -> None:
        if isinstance(frame, ControlFrame) and frame.type == "connection_state":
            names = self.ctx.memory.setdefault("room_names", {})
            for room in self.state.rooms:  # plain ids on switch-core 0.31 (verified 2026-11-05)
                if not isinstance(room, dict):
                    continue
                rid = room.get("room_id") or room.get("id")
                name = room.get("name") or room.get("slug")
                if rid and name:
                    names[rid] = f"#{str(name).lstrip('#')}"
            return
        if not isinstance(frame, MessageEvent):
            return
        root = frame.thread_id or frame.message_id
        log.info(
            "message %s in %s from %s (addressed=%s, thread=%s): %r",
            frame.message_id,
            frame.room_id,
            frame.sender_name or frame.sender,
            frame.addressed,
            frame.thread_id,
            (frame.body or "")[:60],
        )
        self.threads[root].append(
            Turn(
                frame.sender or "",
                frame.sender_name or "",
                frame.body or "",
                frame.sender == self.ctx.self_id,
                frame.room_id,
                frame.message_id,
                frame.thread_id,
            )
        )
        if frame.sequence is not None:
            self.state.cursor = frame.sequence
        self._learn(frame)
        try:
            if self.role.trigger(frame, self.ctx, list(self.threads[root])):
                await self.queue.put(frame)
        except Exception:
            log.exception("trigger failed")

    async def _fetch_brief(self, room_id: str) -> None:
        """The brief posted when a plan room opens can miss a member the bridge was still
        syncing (seen 2026-11-06), and a restarted agent never saw it. Ask sindarin-switch once
        and give its answer a few seconds to arrive before the turn runs."""
        if room_id not in self._brief_asked:
            self._brief_asked.add(room_id)
            try:
                await asyncio.to_thread(
                    self.client.post,
                    room_id,
                    f"@{SWITCH_EXECUTOR} brief",
                    None,
                    connection_id=self.env.connection_id,
                )
            except SwitchError as exc:
                log.error("brief request failed: %s", exc)
                return
        for _ in range(16):
            if self.ctx.brief(room_id):
                return
            await asyncio.sleep(0.5)
        log.warning("no brief for room %s yet; answering without the goals", room_id)

    async def worker(self) -> None:
        while True:
            ev = await self.queue.get()
            root = ev.thread_id or ev.message_id
            thread = list(self.threads.get(root, []))
            try:
                if self.ctx.is_period_room(ev.room_id) and not self.ctx.brief(ev.room_id):
                    await self._fetch_brief(ev.room_id)
                agent: RoleAgent = build_agent(
                    self.role, self.ctx, self.model_call, room_id=ev.room_id
                )
                header = self.role.header(ev, self.ctx, thread)
                text = await asyncio.to_thread(agent.respond, thread, header)
                if agent.tool_log:
                    facts = self.ctx.memory.setdefault("tool_facts", {}).setdefault(ev.room_id, [])
                    facts.extend(agent.tool_log)
                    del facts[:-20]
                log.info("%s replies in %s: %r", self.role.NAME, ev.room_id, text[:80])
                for out in self.role.route(text, ev, self.ctx, thread):
                    await asyncio.to_thread(
                        self.client.post,
                        out.room_id,
                        out.text,
                        out.thread_id,
                        connection_id=self.env.connection_id,
                    )
                    self.threads[out.thread_id or root].append(
                        Turn(self.ctx.self_id, self.role.NAME, out.text, True, out.room_id)
                    )
            except SwitchError as exc:
                log.error("post failed: %s", exc)
            except Exception:  # the model or the network; never take the agent down
                log.exception("respond failed")
            finally:
                self.queue.task_done()

    async def run(self) -> int:
        worker = asyncio.create_task(self.worker())
        try:
            await self.client.run_connection(self.state, self.on_frame)
        except FatalEviction as exc:
            log.error("%s", exc)
            return 2
        finally:
            worker.cancel()
        return 0


def ledger_for(repo_root: Path) -> Ledger | None:
    """A read-only handle on the ledger sindarin-ledger writes, when the file exists."""
    cfg = load_config(repo_root / "executor" / "ledger.yaml")
    path = cfg.resolve(repo_root / "executor", cfg.ledger_db)
    if not path.exists():
        log.warning("no ledger at %s yet; run `sindarin ledger import`", path)
        return None
    return Ledger(path, readonly=True)


async def run_agent(repo_root: Path, role_name: str) -> int:
    if role_name not in ROLES:
        log.error("unknown role %s; one of %s", role_name, ", ".join(AGENT_ROLES))
        return 1
    role = ROLES[role_name]
    env_path = root_env_path(repo_root)
    values = process_env(repo_root, role_name)  # only this role's block of the root .env
    env = Env.from_values(values)
    missing = env.missing(
        "SWITCH_BRIDGE_URL", "SWITCH_AGENT_ID", "SWITCH_AGENT_API_KEY", "SWITCH_CONNECTION_ID"
    )
    if missing:
        log.error("missing in %s: %s (run `sindarin bootstrap`)", env_path, ", ".join(missing))
        return 1
    if values.get("ANTHROPIC_API_KEY"):
        os.environ.setdefault("ANTHROPIC_API_KEY", values["ANTHROPIC_API_KEY"])
    fake = is_fake_model()
    if not fake and not os.environ.get("ANTHROPIC_API_KEY"):
        log.error(
            "ANTHROPIC_API_KEY is not set in %s or the environment (or set SINDARIN_MODEL=fake)",
            root_env_path(repo_root),
        )
        return 1
    policy_path = repo_root / "agents" / "policy" / "policy.yaml"
    users_path = repo_root / "agents" / "policy" / "users.yaml"
    from sindarin.policy import load_policy

    self_id = load_policy(policy_path).proposer_id(role_name)
    if not self_id:
        log.error("policy.yaml has no participant id for %s (run `sindarin bootstrap`)", role_name)
        return 1
    ctx = RoleContext(
        role=role_name,
        repo_root=repo_root,
        policy_path=policy_path,
        users_path=users_path,
        self_id=self_id,
        ledger=ledger_for(repo_root),
    )
    model_call = ScriptedModel(role.fake_step(ctx)) if fake else anthropic_model_call()
    client = SwitchClient(
        env.bridge_url, env.agent_id, env.api_key, client_name=f"sindarin-{role_name}"
    )
    service = AgentService(role, ctx, client, env, model_call)
    log.info("%s starting · model=%s · self=%s", role_name, "fake" if fake else "claude", self_id)
    try:
        return await service.run()
    finally:
        client.close()
