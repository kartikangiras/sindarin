"""In-memory Gateway rooms adapter for tests (design v4 §5). It records every call, can be told
to fail, and never touches a network. The ledger needs no fake: tests run the real one on a
temporary file."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sindarin.adapters.base import ActionResult, AdapterError, Facts, VerifyResult
from sindarin.schema import Artifact, Kind, Proposal


@dataclass
class FakeSwitchRooms:
    """Enough of the Gateway rooms route for the goals gate."""

    platform: str = "switch"
    kinds: frozenset[Kind] = frozenset({Kind.GOALS, Kind.CLOSE})
    rooms: dict[str, dict[str, Any]] = field(default_factory=dict)  # slug → room
    calls: list[tuple[str, str]] = field(default_factory=list)
    fail_execute_times: int = 0
    fail_verify_times: int = 0

    def read_facts(self, proposal: Proposal) -> Facts:
        self.calls.append(("read_facts", proposal.target))
        return Facts(ok=True, data={})

    def execute(self, a: Artifact) -> ActionResult:
        plan = a.facts()
        slug = plan.get("slug") or a.target
        self.calls.append(("execute", slug))
        if self.fail_execute_times > 0:
            self.fail_execute_times -= 1
            raise AdapterError("Gateway 503: unavailable")
        room = self.rooms.get(slug)
        if room is None:
            room = {
                "room_id": f"!room-{slug}:test",
                "name": slug,
                "humans": list(plan.get("humans", [])),
                "agents": list(plan.get("agents", [])),
                "instructions_sha": plan.get("instructions_sha", ""),
            }
            self.rooms[slug] = room
        return ActionResult(
            ok=True,
            data={
                "room_id": room["room_id"],
                "room_name": slug,
                "members": len(room["humans"]) + len(room["agents"]),
            },
        )

    def verify(self, a: Artifact) -> VerifyResult:
        plan = a.facts()
        slug = plan.get("slug") or a.target
        self.calls.append(("verify", slug))
        if self.fail_verify_times > 0:
            self.fail_verify_times -= 1
            return VerifyResult(present=None, error="Gateway 503")
        room = self.rooms.get(slug)
        if room is None:
            return VerifyResult(present=False)
        ok = set(room["humans"]) >= set(plan.get("humans", [])) and set(room["agents"]) >= set(
            plan.get("agents", [])
        )
        return VerifyResult(present=ok, data={"room_id": room["room_id"], "room_name": slug})

    def count(self, op: str) -> int:
        return sum(1 for c in self.calls if c[0] == op)
