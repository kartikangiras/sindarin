"""Switch rooms adapter against a fake Gateway (design v5 §5): create with the goals, reconcile an
existing room, read members back, translate ids through the directory, leak nothing."""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from validation.harness import (
    BASIS,
    BOOKKEEPER_AGENT,
    DATA,
    ISAAC,
    JOHN,
    LEDGER_EXEC,
    LEONARDO,
    PLANNER_AGENT,
    SWITCH_EXEC,
    goals_proposal,
)

from sindarin.adapters.base import AdapterError
from sindarin.adapters.switch_rooms import SwitchRoomsAdapter
from sindarin.executor.core import plan_instructions
from sindarin.ledger import Ledger
from sindarin.schema import Artifact, Kind, State, make_snapshot

BOOKS = Ledger(Path(tempfile.mkdtemp()) / "books.db")
BOOKS.import_dir(DATA / BASIS)

PLAN = {
    "slug": "plan-2026-11",
    "humans": [JOHN, LEONARDO],
    "agents": [PLANNER_AGENT, BOOKKEEPER_AGENT, LEDGER_EXEC, SWITCH_EXEC],
    "instructions_sha": "x",
}
DIRECTORY = {
    ISAAC: "isaac",
    LEONARDO: "leonardo",
    JOHN: "john",
    PLANNER_AGENT: "g-planner",
    BOOKKEEPER_AGENT: "g-bookkeeper",
    LEDGER_EXEC: "g-ledger",
    SWITCH_EXEC: "g-switch",
}


@pytest.fixture(autouse=True)
def _no_settle(monkeypatch):
    monkeypatch.setattr("sindarin.adapters.switch_rooms.SETTLE_SECONDS", 0.0)


class FakeGateway:
    def __init__(self):
        self.rooms: list[dict] = []
        self.calls: list[str] = []
        self.fail = False
        # gateway agent id → allowed room ids (None = open policy)
        self.policies: dict[str, list[str] | None] = {
            "g-planner": ["!room-old:test"],
            "g-bookkeeper": ["!room-old:test"],
            "g-ledger": ["!room-planning:test"],
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
    p = goals_proposal(BOOKS)
    snap = make_snapshot("GOALS-1", "@p:t", p, PLAN)
    now = datetime(2026, 11, 5, tzinfo=UTC)
    return Artifact(
        id="GOALS-1",
        kind=Kind.GOALS,
        state=State.OPENING,
        rule="goals",
        room_id="!p",
        thread_id="t",
        proposal_message_id="m",
        proposer_id="@p:t",
        proposer_name="planner",
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
        "room_id": "!room-plan-2026-11:test",
        "room_name": "plan-2026-11",
        "members": 6,
    }
    room = gw.rooms[0]
    assert room["channel_type"] == "channel_private" and room["bridge_id"] == "bridge-1"
    assert room["agent_ids"] == ["g-planner", "g-bookkeeper", "g-ledger", "g-switch"] and room[
        "user_names"
    ] == ["john", "leonardo"]
    assert (
        room["instructions"] == plan_instructions(a.proposal())
        and "## Goals for 2026-11" in room["instructions"]
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
    assert gw.policies["g-bookkeeper"] == ["!room-old:test", new_room]
    assert gw.policies["g-ledger"] == ["!room-planning:test", new_room]
    assert gw.policies["g-switch"] is None, "an open policy stays open"
    # a second execute (reconcile) adds nothing twice
    assert adapter.execute(_artifact()).data["addressing_widened"] == 0
    # layer 0 can be turned off in config; layer 1 never depended on it
    gw2 = FakeGateway()
    off = SwitchRoomsAdapter(gw2, bridge_id="b", directory=DIRECTORY, scoped_addressing=False)
    assert off.execute(_artifact()).data["addressing_widened"] == 0
    assert "policy_set" not in gw2.calls


def test_a_room_of_the_same_name_on_another_bridge_is_not_reused():
    """Two Mattermost servers connected: yesterday's #plan-2026-11 on the bundled bridge must
    not be mistaken for the plan room of the bridge this executor runs on (seen 2026-11-06)."""
    gw = FakeGateway()
    gw.rooms.append(
        {
            "id": "!room-stale:test",
            "name": "plan-2026-11",
            "bridge_id": "old-bridge",
            "agents": ["g-bookkeeper"],
            "users": ["leonardo"],
        }
    )
    adapter = SwitchRoomsAdapter(gw, bridge_id="b", directory=DIRECTORY)
    a = _artifact()
    assert adapter.verify(a).present is False
    adapter.execute(a)
    assert gw.calls.count("create") == 1
    ours = [r for r in gw.rooms if r.get("bridge_id") == "b" and r["name"] == "plan-2026-11"]
    assert len(ours) == 1 and adapter.verify(a).present is True
    # a second execute reconciles our room, still never the stale one
    adapter.execute(a)
    assert gw.calls.count("create") == 1
    # without a bridge of its own the adapter keeps the old by-name behaviour
    assert SwitchRoomsAdapter(gw, bridge_id="", directory=DIRECTORY)._find("plan-2026-11")


def test_settle_waits_for_members_then_a_grace_period(monkeypatch):
    """The brief must not go out before the bridge has every member (2026-11-06)."""
    import sindarin.adapters.switch_rooms as mod

    slept: list[float] = []
    monkeypatch.setattr(mod.time, "sleep", lambda s: slept.append(s))
    gw = FakeGateway()
    adapter = SwitchRoomsAdapter(gw, bridge_id="b", directory=DIRECTORY, settle_seconds=2.0)
    adapter.execute(_artifact())
    assert slept == [2.0]  # members were complete at once: only the grace period
    assert "members" in gw.calls
    # reconciling an existing room does not wait
    adapter.execute(_artifact())
    assert slept == [2.0]


def test_a_mattermost_member_is_seated_from_the_id_itself():
    """A colleague added after the bootstrap is not in the executor's directory; the id a
    Mattermost member carries names them anyway. An agent still needs the directory."""
    adapter = SwitchRoomsAdapter(FakeGateway(), bridge_id="b", directory={"@a:x": "agent-1"})
    bridge = "b76af5d9-670f-4573-9f7b-6655cdb00753"
    maria = f"@switch-mattermost-{bridge}-maria:localhost"
    assert adapter._translate(["@a:x", maria, maria]) == ["agent-1", "maria"]
    with pytest.raises(AdapterError, match="1 participant id\\(s\\) missing"):
        adapter._translate(["@switch-agent-unknown:localhost"])


class DressedGateway(FakeGateway):
    """A Gateway with the Switch components around a room: documents, links, groups."""

    def __init__(self):
        super().__init__()
        self.documents: list[dict] = []
        self.attached: list[tuple[str, str]] = []
        self.links: list[tuple[str, str, str]] = []
        self.groups: dict[str, str] = {}
        self.grouped: list[tuple[str, str]] = []

    def create_document(self, *, name, description, instructions, content):
        self.documents.append({"name": name, "content": content})
        return f"doc-{len(self.documents)}"

    def attach_document(self, room_id, document_id):
        self.attached.append((room_id, document_id))

    def link_rooms(self, room_id, target_room_id, label):
        self.links.append((room_id, target_room_id, label))

    def ensure_room_group(self, name, description=""):
        return self.groups.setdefault(name, f"group-{len(self.groups) + 1}")

    def put_room_group(self, room_id, group_id):
        self.grouped.append((room_id, group_id))


def test_a_new_room_gets_its_document_link_and_group():
    gw = DressedGateway()
    adapter = SwitchRoomsAdapter(gw, bridge_id="b", directory=DIRECTORY)
    adapter.home_room_id = "!room-acme-finance:test"
    r = adapter.execute(_artifact())
    assert r.data["document_id"] == "doc-1" and r.data["group"] == "Acme finance"
    assert r.data["linked_from"] == "!room-acme-finance:test"
    assert gw.documents[0]["name"] == "GOALS-1 · Plan November 2026"
    assert "## Goals for 2026-11" in gw.documents[0]["content"]
    assert gw.attached == [("!room-plan-2026-11:test", "doc-1")]
    assert gw.links == [("!room-acme-finance:test", "!room-plan-2026-11:test", "opened by GOALS-1")]
    assert gw.grouped == [("!room-plan-2026-11:test", "group-1")]
    # reconciling an existing room dresses nothing twice; a plain gateway is fine without any
    assert "document_id" not in adapter.execute(_artifact()).data
    plain = SwitchRoomsAdapter(FakeGateway(), bridge_id="b", directory=DIRECTORY)
    assert "document_id" not in plain.execute(_artifact()).data
