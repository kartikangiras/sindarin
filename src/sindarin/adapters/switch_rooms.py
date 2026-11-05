"""Switch rooms adapter for sindarin-switch (DDD §7.2): open a bridged feature room with the
spec as instructions, add the people and agents the snapshot names, read the members back.

Talks to the Gateway (`POST /gateway/rooms`, verify item 4 in §12.4). The participant ids in the
snapshot are translated to Gateway agent ids and Mattermost user names through a directory that
bootstrap writes into executor/switch.yaml; the translation is display plumbing, not a check.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Protocol

from sindarin.adapters.base import ActionResult, AdapterError, Facts, VerifyResult
from sindarin.executor.core import spec_instructions
from sindarin.schema import Artifact, Kind, Proposal

log = logging.getLogger("sindarin.rooms")


class RoomsGateway(Protocol):
    """The slice of GatewayClient this adapter uses (so tests can fake it)."""

    def addressing_rooms(self, agent_id: str) -> list[str] | None: ...
    def set_addressing_rooms(self, agent_id: str, room_ids: list[str]) -> dict[str, Any]: ...
    def list_rooms(self) -> list[dict[str, Any]]: ...
    def create_room(
        self,
        *,
        name: str,
        description: str,
        instructions: str,
        bridge_id: str,
        channel_type: str,
        agent_ids: list[str],
        user_names: list[str],
    ) -> dict[str, Any]: ...
    def room_members(self, room_id: str) -> dict[str, list[str]]: ...
    def add_room_agents(self, room_id: str, agent_ids: list[str]) -> None: ...
    def add_room_users(self, room_id: str, user_names: list[str]) -> None: ...


class SwitchRoomsAdapter:
    platform = "switch"
    kinds = frozenset({Kind.SPEC})

    def __init__(
        self,
        gateway: RoomsGateway,
        *,
        bridge_id: str,
        directory: Mapping[str, str],
        channel_type: str = "channel_private",
        scoped_addressing: bool = True,
    ) -> None:
        self.gateway = gateway
        self.bridge_id = bridge_id
        self.directory = dict(directory)  # participant id → gateway agent id | mattermost username
        self.channel_type = channel_type
        self.scoped_addressing = scoped_addressing

    def read_facts(self, proposal: Proposal) -> Facts:
        return Facts(ok=True, data={})

    def _translate(self, ids: list[str]) -> list[str]:
        missing = [i for i in ids if i not in self.directory]
        if missing:
            raise AdapterError(f"{len(missing)} participant id(s) missing from the directory")
        return [self.directory[i] for i in ids]

    def _find(self, name: str) -> dict[str, Any] | None:
        for room in self.gateway.list_rooms():
            if (room.get("name") or room.get("slug")) == name:
                return room
        return None

    def execute(self, a: Artifact) -> ActionResult:
        plan = a.facts()
        proposal = a.proposal()
        slug = plan.get("slug") or a.target
        agents = self._translate(plan.get("agents", []))
        users = self._translate(plan.get("humans", []))
        body: Any = proposal.body
        existing = self._find(slug)
        try:
            if existing is None:
                room = self.gateway.create_room(
                    name=slug,
                    description=f"Feature room for {a.id}: {body.title}. Merge is approved here.",
                    instructions=spec_instructions(proposal),
                    bridge_id=self.bridge_id,
                    channel_type=self.channel_type,
                    agent_ids=agents,
                    user_names=users,
                )
            else:
                room = existing
                members = self.gateway.room_members(room["id"])
                want_agents = [x for x in agents if x not in set(members.get("agents", []))]
                want_users = [x for x in users if x not in set(members.get("users", []))]
                if want_agents:
                    self.gateway.add_room_agents(room["id"], want_agents)
                if want_users:
                    self.gateway.add_room_users(room["id"], want_users)
        except Exception as exc:  # the Gateway's words are safe to show; its cookie is not
            raise AdapterError(f"Gateway: {str(exc)[:160]}") from None
        widened = self._widen_addressing(str(room.get("id") or ""), agents)
        return ActionResult(
            ok=True,
            data={
                "room_id": room.get("id"),
                "room_name": slug,
                "members": len(agents) + len(users),
                "addressing_widened": widened,
            },
        )

    def _widen_addressing(self, room_id: str, gateway_agent_ids: list[str]) -> int:
        """Layer 0 for a room that did not exist at bootstrap (§5): every agent whose policy is
        room-scoped gets the new room added. Open policies stay open. Fail-soft: the executor's
        own checks (layer 1) do not depend on this."""
        if not room_id or not self.scoped_addressing:
            return 0
        widened = 0
        for gid in gateway_agent_ids:
            try:
                rooms = self.gateway.addressing_rooms(gid)
                if rooms is None or room_id in rooms:
                    continue
                self.gateway.set_addressing_rooms(gid, [*rooms, room_id])
                widened += 1
            except Exception as exc:
                log.warning("could not widen addressing for %s: %s", gid, str(exc)[:120])
        return widened

    def verify(self, a: Artifact) -> VerifyResult:
        plan = a.facts()
        slug = plan.get("slug") or a.target
        try:
            room = self._find(slug)
            if room is None:
                return VerifyResult(present=False)
            members = self.gateway.room_members(room["id"])
            agents = set(self._translate(plan.get("agents", [])))
            users = set(self._translate(plan.get("humans", [])))
            ok = agents <= set(members.get("agents", [])) and users <= set(members.get("users", []))
            return VerifyResult(present=ok, data={"room_id": room.get("id"), "room_name": slug})
        except Exception as exc:
            return VerifyResult(present=None, error=f"Gateway: {str(exc)[:160]}")
