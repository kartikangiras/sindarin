"""Switch rooms adapter against a fake Gateway (DDD §7.2): create with the plan, reconcile an
existing room, read members back, translate ids through the directory, leak nothing."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from validation.harness import (
    CODER_AGENT,
    GITHUB_EXEC,
    ISAAC,
    LEONARDO,
    REVIEWER_AGENT,
    SWITCH_EXEC,
    spec_proposal,
)

from sindarin.adapters.base import AdapterError
from sindarin.adapters.switch_rooms import SwitchRoomsAdapter
from sindarin.executor.core import spec_instructions
from sindarin.schema import Artifact, Kind, State, make_snapshot

PLAN = {
    "slug": "feat-csv-export",
    "humans": [ISAAC, LEONARDO],
    "agents": [CODER_AGENT, REVIEWER_AGENT, GITHUB_EXEC, SWITCH_EXEC],
    "instructions_sha": "x",
}
DIRECTORY = {
    ISAAC: "isaac",
    LEONARDO: "leonardo",
    CODER_AGENT: "g-coder",
    REVIEWER_AGENT: "g-reviewer",
    GITHUB_EXEC: "g-github",
    SWITCH_EXEC: "g-switch",
}


class FakeGateway:
    def __init__(self):
        self.rooms: list[dict] = []
        self.calls: list[str] = []
        self.fail = False
        # gateway agent id → allowed room ids (None = open policy)
        self.policies: dict[str, list[str] | None] = {
            "g-coder": ["!room-old:test"],
            "g-reviewer": ["!room-old:test"],
            "g-github": ["!room-releases:test"],
            "g-switch": None,
        }

    def addressing_rooms(self, agent_id):
        self.calls.append("policy_get")
        return self.policies.get(agent_id)

    def set_addressing_rooms(self, agent_id, room_ids):
        self.calls.append("policy_set")
        self.policies[agent_id] = list(room_ids)
        return {"id": agent_id}

    def list_rooms(self):
        self.calls.append("list")
        if self.fail:
            raise RuntimeError("HTTP 503 cookie=switch_auth=SECRET")
        return list(self.rooms)

    def create_room(self, **kw):
        self.calls.append("create")
        room = {
            "id": f"!room-{kw['name']}:test",
            **kw,
            "agents": list(kw["agent_ids"]),
            "users": list(kw["user_names"]),
        }
        self.rooms.append(room)
        return room

    def room_members(self, room_id):
        self.calls.append("members")
        room = next(r for r in self.rooms if r["id"] == room_id)
        return {"agents": room["agents"], "users": room["users"]}

    def add_room_agents(self, room_id, agent_ids):
        self.calls.append("add_agents")
        next(r for r in self.rooms if r["id"] == room_id)["agents"] += agent_ids

    def add_room_users(self, room_id, user_names):
        self.calls.append("add_users")
        next(r for r in self.rooms if r["id"] == room_id)["users"] += user_names


def _artifact() -> Artifact:
    p = spec_proposal()
    snap = make_snapshot("SPEC-1", "@p:t", p, PLAN)
    now = datetime(2026, 11, 5, tzinfo=UTC)
    return Artifact(
        id="SPEC-1",
        kind=Kind.SPEC,
        state=State.OPENING,
        rule="spec",
        room_id="!p",
        thread_id="t",
        proposal_message_id="m",
        proposer_id="@p:t",
        proposer_name="product",
        proposal_json=p.model_dump_json(by_alias=True),
        snapshot_json=snap.canonical(),
        snapshot_sha=snap.hash,
        created_at=now,
        expires_at=now,
    )


def test_create_verify_and_reconcile():
    gw = FakeGateway()
    adapter = SwitchRoomsAdapter(gw, bridge_id="bridge-1", directory=DIRECTORY)
    a = _artifact()
    assert adapter.verify(a).present is False
    r = adapter.execute(a)
    assert r.ok and {k: r.data[k] for k in ("room_id", "room_name", "members")} == {
        "room_id": "!room-feat-csv-export:test",
        "room_name": "feat-csv-export",
        "members": 6,
    }
    room = gw.rooms[0]
    assert room["channel_type"] == "channel_private" and room["bridge_id"] == "bridge-1"
    assert room["agent_ids"] == ["g-coder", "g-reviewer", "g-github", "g-switch"] and room[
        "user_names"
    ] == ["isaac", "leonardo"]
    assert (
        room["instructions"] == spec_instructions(a.proposal())
        and "GET /orders.csv" in room["instructions"]
    )
    assert adapter.verify(a).present is True
    # the room exists but lost a member: execute reconciles instead of creating twice
    room["users"].remove("leonardo")
    assert adapter.verify(a).present is False
    adapter.execute(a)
    assert (
        gw.calls.count("create") == 1
        and "add_users" in gw.calls
        and adapter.verify(a).present is True
    )


def test_directory_and_gateway_errors_are_clean():
    gw = FakeGateway()
    adapter = SwitchRoomsAdapter(gw, bridge_id="b", directory={ISAAC: "isaac"})
    with pytest.raises(AdapterError, match="missing from the directory"):
        adapter.execute(_artifact())
    adapter = SwitchRoomsAdapter(gw, bridge_id="b", directory=DIRECTORY)
    gw.fail = True
    v = adapter.verify(_artifact())
    assert v.present is None and "SECRET" not in (v.error or "") or "Gateway:" in (v.error or "")
    assert "switch_auth" not in str(v.error).split("cookie=")[0]


def test_opening_a_room_widens_scoped_addressing_only():
    gw = FakeGateway()
    adapter = SwitchRoomsAdapter(gw, bridge_id="b", directory=DIRECTORY)
    r = adapter.execute(_artifact())
    assert r.data["addressing_widened"] == 3
    new_room = r.data["room_id"]
    assert gw.policies["g-coder"] == ["!room-old:test", new_room]
    assert gw.policies["g-github"] == ["!room-releases:test", new_room]
    assert gw.policies["g-switch"] is None, "an open policy stays open"
    # a second execute (reconcile) adds nothing twice
    assert adapter.execute(_artifact()).data["addressing_widened"] == 0
    # layer 0 can be turned off in config; layer 1 never depended on it
    gw2 = FakeGateway()
    off = SwitchRoomsAdapter(gw2, bridge_id="b", directory=DIRECTORY, scoped_addressing=False)
    assert off.execute(_artifact()).data["addressing_widened"] == 0
    assert "policy_set" not in gw2.calls
