"""The agent process (DDD §6, §16.1): one Switch connection per role, one model call per trigger,
replies routed by the role. `sindarin-agent run <role>`."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections import defaultdict
from pathlib import Path
from types import ModuleType

from sindarin.agents import coder, product, release, reviewer
from sindarin.agents.base import (
    RoleAgent,
    ScriptedModel,
    Turn,
    anthropic_model_call,
    is_fake_model,
)
from sindarin.agents.github_client import AgentGitHub, RealGitHub, SimGitHub
from sindarin.agents.roles import PENDING_RE, RoleContext, build_agent
from sindarin.executor.config import Env, load_config, load_env
from sindarin.executor.core import parse_proposal
from sindarin.names import AGENT_ROLES, SWITCH_EXECUTOR
from sindarin.schema import Kind
from sindarin.switch.client import ConnectionState, FatalEviction, SwitchClient, SwitchError
from sindarin.switch.events import ControlFrame, Frame, MessageEvent

log = logging.getLogger("sindarin.agents")

ROLES: dict[str, ModuleType] = {
    product.NAME: product,
    coder.NAME: coder,
    reviewer.NAME: reviewer,
    release.NAME: release,
}
BRIEF_RE = re.compile(r"\*\*(SPEC-\d+)\*\* instructions", re.IGNORECASE)


class AgentService:
    def __init__(
        self, role: ModuleType, ctx: RoleContext, client: SwitchClient, env: Env, model_call
    ) -> None:
        self.role = role
        self.ctx = ctx
        self.client = client
        self.env = env
        self.model_call = model_call
        self.threads: dict[str, list[Turn]] = defaultdict(list)
        self.queue: asyncio.Queue[MessageEvent] = asyncio.Queue()
        self.state = ConnectionState(connection_id=env.connection_id)

    # -- learning from the stream (no model involved) ---------------------------------------

    def _learn(self, ev: MessageEvent) -> None:
        body = ev.body or ""
        mem = self.ctx.memory
        if ev.sender == self.ctx.policy().executor_id(SWITCH_EXECUTOR) and BRIEF_RE.search(body):
            mem.setdefault("briefs", {})[ev.room_id] = body
        if self.ctx.room_key(ev.room_id) == "product":
            if "```sindarin-proposal" in body:
                proposal, _ = parse_proposal(body)
                if proposal is not None and proposal.kind is Kind.SPEC and proposal.ask:
                    mem.setdefault("ask_by_thread", {})[ev.thread_id or ev.message_id] = (
                        proposal.ask
                    )
            m = PENDING_RE.search(body)
            if m and ev.thread_id and m.group(1).startswith("SPEC-"):
                ask = mem.get("ask_by_thread", {}).get(ev.thread_id)
                if ask:
                    mem.setdefault("asks", {})[m.group(1)] = ask
        if (
            self.ctx.room_key(ev.room_id) == "customer"
            and ev.thread_id is None
            and self.ctx.is_human(ev.sender)
        ):
            mem["asker"] = ev.sender
        if self.ctx.room_key(ev.room_id) == "releases" and "```sindarin-proposal" in body:
            proposal, _ = parse_proposal(body)
            if proposal is not None and proposal.kind is Kind.RELEASE:
                mem.setdefault("release_by_thread", {})[ev.thread_id or ev.message_id] = list(
                    proposal.body.specs
                )  # type: ignore[union-attr]
        if self.ctx.room_key(ev.room_id) == "releases":
            m = PENDING_RE.search(body)
            if m and m.group(1).startswith("REL-") and ev.sender in self.ctx.executor_ids():
                specs = mem.get("release_by_thread", {}).get(ev.thread_id or "") or mem.get(
                    "last_release_specs"
                )
                if specs:
                    mem.setdefault("releases", {})[m.group(1)] = list(specs)
                    mem.setdefault("release_by_thread", {})[ev.thread_id or ev.message_id] = list(
                        specs
                    )

    def _remember_own(self, text: str) -> None:
        """An agent does not receive its own posts (verified 2026-11-05), so what it proposed is
        noted here before posting: the executor's `pending` reply names the id a moment later."""
        if "```sindarin-proposal" not in text:
            return
        proposal, _ = parse_proposal(text)
        if proposal is None:
            return
        mem = self.ctx.memory
        if proposal.kind is Kind.RELEASE:
            mem["last_release_specs"] = list(proposal.body.specs)  # type: ignore[union-attr]
        elif proposal.kind is Kind.SPEC and proposal.ask:
            mem["last_spec_ask"] = proposal.ask

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

    async def worker(self) -> None:
        while True:
            ev = await self.queue.get()
            root = ev.thread_id or ev.message_id
            thread = list(self.threads.get(root, []))
            try:
                agent: RoleAgent = build_agent(
                    self.role, self.ctx, self.model_call, room_id=ev.room_id
                )
                header = self.role.header(ev, self.ctx, thread)
                text = await asyncio.to_thread(agent.respond, thread, header)
                log.info("%s replies in %s: %r", self.role.NAME, ev.room_id, text[:80])
                for out in self.role.route(text, ev, self.ctx, thread):
                    self._remember_own(out.text)
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


def github_for(role: str, repo_root: Path, values: dict[str, str]) -> AgentGitHub | None:
    """The agents' GitHub: the simulated repository when the executor simulates, else the API
    with whatever token this role holds (the coder's scoped token; read-only for the others)."""
    from sindarin.policy import load_policy

    cfg = load_config(repo_root / "executor" / "github.yaml")
    policy = load_policy(repo_root / "agents" / "policy" / "policy.yaml")
    repo = next((r.repo for r in policy.rules if r.repo), "")
    if cfg.github_mode == "simulated":
        return SimGitHub(repo_root / "executor" / "simulated-github.json", repo)
    if not repo:
        return None
    if role == coder.NAME:
        token = values.get("CODER_GITHUB_TOKEN", "")
        return RealGitHub(token, repo) if token else None
    # reviewer and release hold no GitHub credential: anonymous reads of the public repository
    return RealGitHub(values.get("GITHUB_TOKEN", ""), repo)


async def run_agent(repo_root: Path, role_name: str) -> int:
    if role_name not in ROLES:
        log.error("unknown role %s; one of %s", role_name, ", ".join(AGENT_ROLES))
        return 1
    role = ROLES[role_name]
    env_path = repo_root / "agents" / role_name / ".env"
    values = load_env(env_path, env_override=False)
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
            env_path,
        )
        return 1
    policy_path = repo_root / "agents" / "policy" / "policy.yaml"
    users_path = repo_root / "agents" / "policy" / "users.yaml"
    from sindarin.policy import load_policy

    self_id = load_policy(policy_path).proposer_id(role_name)
    if not self_id:
        log.error("policy.yaml has no participant id for %s (run `sindarin bootstrap`)", role_name)
        return 1
    workdir = values.get("CODER_WORKDIR") or os.environ.get("CODER_WORKDIR")
    ctx = RoleContext(
        role=role_name,
        repo_root=repo_root,
        policy_path=policy_path,
        users_path=users_path,
        self_id=self_id,
        github=github_for(role_name, repo_root, values),
        workdir=Path(workdir) if workdir else None,
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
