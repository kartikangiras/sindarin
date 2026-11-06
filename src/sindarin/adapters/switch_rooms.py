"""Switch rooms adapter for sindarin-switch (design v5 §5): open a bridged plan room with the
goals as instructions, add the people and agents the snapshot names, read the members back.

Talks to the Gateway (`POST /gateway/rooms`, verify item 4 in §12.4). The participant ids in the
snapshot are translated to Gateway agent ids and Mattermost user names through a directory that
bootstrap writes into executor/switch.yaml; the translation is display plumbing, not a check.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import Any, Protocol

from sindarin.adapters.base import ActionResult, AdapterError, Facts, VerifyResult
from sindarin.executor.core import plan_instructions
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
    # optional Switch components (the live Gateway has them; a fake may not)
    # create_document(...) -> str · attach_document(room, doc) · link_rooms(room, target, label)
    # ensure_room_group(name) -> str · group_rooms(id) -> list · assign_rooms_to_group(id, rooms)
    def room_members(self, room_id: str) -> dict[str, list[str]]: ...
    def add_room_agents(self, room_id: str, agent_ids: list[str]) -> None: ...
    def add_room_users(self, room_id: str, user_names: list[str]) -> None: ...


# After a bridged room is created the Gateway lists its members at once, but the bridge is still
# syncing them to Mattermost for a moment; a post in that window reaches only the members already
# synced (2026-11-06: the reviewer never saw the brief). Adapters run off the event loop, so a
# short wait here is safe. Tests set it to 0.
SETTLE_SECONDS = 3.0


class SwitchRoomsAdapter:
    platform = "switch"
    kinds = frozenset({Kind.GOALS, Kind.CLOSE})

    def __init__(
        self,
        gateway: RoomsGateway,
        *,
        bridge_id: str,
        directory: Mapping[str, str],
        channel_type: str = "channel_private",
        scoped_addressing: bool = True,
        settle_seconds: float | None = None,
    ) -> None:
        self.gateway = gateway
        self.bridge_id = bridge_id
        self.directory = dict(directory)  # participant id → gateway agent id | mattermost username
        self.channel_type = channel_type
        self.scoped_addressing = scoped_addressing
        self.settle_seconds = SETTLE_SECONDS if settle_seconds is None else settle_seconds
        self.home_room_id = ""  # #acme-finance: the room the new one is linked from
        self.group_name = "Acme finance"  # the Switch room group the new room joins

    def read_facts(self, proposal: Proposal) -> Facts:
        return Facts(ok=True, data={})

    def _translate(self, ids: list[str]) -> list[str]:
        missing = [i for i in ids if i not in self.directory]
        if missing:
            raise AdapterError(f"{len(missing)} participant id(s) missing from the directory")
        return list(dict.fromkeys(self.directory[i] for i in ids))  # one person, one seat

    def _find(self, name: str) -> dict[str, Any] | None:
        """The room of that name on this adapter's bridge. With two Mattermost servers connected
        the same name exists once per bridge; a room elsewhere is never reused (2026-11-06)."""
        for room in self.gateway.list_rooms():
            if (room.get("name") or room.get("slug")) != name:
                continue
            on = str(room.get("bridge_id") or "")
            if self.bridge_id and on and on != self.bridge_id:
                continue
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
                    description=(
                        f"Plan room for {a.id}: {body.title}. The budget register entries are "
                        "approved here."
                        if a.kind is Kind.GOALS
                        else f"Period room for {a.id}: {body.title}. The lock is approved here."
                    ),
                    instructions=plan_instructions(proposal),
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
        extras = self._dress(str(room.get("id") or ""), a, proposal) if existing is None else {}
        if existing is None:
            self._settle(str(room.get("id") or ""), agents, users)
        return ActionResult(
            ok=True,
            data={
                "room_id": room.get("id"),
                "room_name": slug,
                "members": len(agents) + len(users),
                "addressing_widened": widened,
                **extras,
            },
        )

    def _dress(self, room_id: str, a: Artifact, proposal: Proposal) -> dict[str, Any]:
        """The Switch components around the new room, each fail-soft: the brief as a Switch
        document attached to it, a link from the home room, membership of the finance group."""
        out: dict[str, Any] = {}
        body: Any = proposal.body
        gw: Any = self.gateway
        if hasattr(gw, "create_document"):
            try:
                doc = gw.create_document(
                    name=f"{a.id} · {body.title}",
                    description=(
                        f"The brief of {a.id}, approved by {a.approver_name or 'the approver'}."
                    ),
                    instructions="Read before acting in this room. Agents propose; people decide.",
                    content=plan_instructions(proposal),
                )
                gw.attach_document(room_id, doc)
                out["document_id"] = doc
            except Exception as exc:
                log.warning("document not attached: %s", str(exc)[:120])
        if self.home_room_id and hasattr(gw, "link_rooms"):
            try:
                gw.link_rooms(self.home_room_id, room_id, f"opened by {a.id}")
                out["linked_from"] = self.home_room_id
            except Exception as exc:
                log.warning("rooms not linked: %s", str(exc)[:120])
        if self.group_name and hasattr(gw, "put_room_group"):
            try:
                gid = gw.ensure_room_group(self.group_name)
                gw.put_room_group(room_id, gid)
                out["group"] = self.group_name
            except Exception as exc:
                log.warning("room not grouped: %s", str(exc)[:120])
        return out

    def _settle(self, room_id: str, agents: list[str], users: list[str]) -> None:
        """Wait until the Gateway lists every member, then a grace period for the bridge, so the
        brief posted next reaches everyone (see SETTLE_SECONDS)."""
        if not room_id or self.settle_seconds <= 0:
            return
        deadline = time.monotonic() + max(10.0, self.settle_seconds * 3)
        while time.monotonic() < deadline:
            try:
                members = self.gateway.room_members(room_id)
            except Exception:
                break
            if set(agents) <= set(members.get("agents", [])) and set(users) <= set(
                members.get("users", [])
            ):
                break
            time.sleep(0.5)
        time.sleep(self.settle_seconds)

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
