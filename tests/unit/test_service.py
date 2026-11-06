"""The executor service end to end over a fake Switch bridge (design v5 §5): connection_state,
pings, a budget proposed, approved by participant id, recorded in the ledger, read back; plus
the adapters' wiring and the agents' read-only ledger handle."""

from __future__ import annotations

import asyncio
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from validation.harness import (
    ASK,
    BASIS,
    DATA,
    FINANCE,
    FIXTURES,
    ISAAC,
    LEDGER_EXEC,
    PERIOD,
    PERIOD_ROOM,
    PLANNER_AGENT,
    PLANNING,
    budget_proposal,
    goals_proposal,
    proposal_text,
)
from websockets.asyncio.server import serve

from sindarin.adapters.ledger import LedgerAdapter
from sindarin.executor.config import Env, ExecutorConfig, save_config
from sindarin.executor.core import Context, _goals_plan
from sindarin.executor.service import ExecutorService, build_adapters
from sindarin.ledger import Ledger
from sindarin.names import LEDGER_EXECUTOR
from sindarin.policy import load_policy
from sindarin.schema import Artifact, Kind, State, make_snapshot
from sindarin.store import Store
from sindarin.switch.client import SwitchClient
from sindarin.switch.events import message_envelope
from sindarin.switch.gateway import GatewayError
from sindarin.users import load_users

API_KEY = "switch-key-TEST"
T0 = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


class Bridge:
    def __init__(self, script):
        self.script = script
        self.pongs = 0
        self.params = []

    async def handler(self, ws):
        from urllib.parse import parse_qs, urlsplit

        self.params.append({k: v[0] for k, v in parse_qs(urlsplit(ws.request.path).query).items()})
        await ws.send(
            json.dumps(
                {
                    "event": "connection_state",
                    "data": {
                        "rooms": [
                            {"room_id": PLANNING, "name": "planning"},
                            {"room_id": PERIOD_ROOM, "name": f"plan-{PERIOD}"},
                            "!bare-id:localhost",  # Switch lists some rooms as id strings
                        ],
                        "cursor": 0,
                        "generation": 1,
                    },
                }
            )
        )
        for step in self.script:
            if step == "ping":
                await ws.send(json.dumps({"event": "ping", "data": {}}))
                await asyncio.wait_for(ws.recv(), 2)
                self.pongs += 1
            elif isinstance(step, float):
                await asyncio.sleep(step)
            else:
                await ws.send(json.dumps(step))
        await asyncio.sleep(5)


@pytest.fixture
def exec_dir(tmp_path: Path) -> Path:
    d = tmp_path / "executor"
    d.mkdir()
    shutil.copy(FIXTURES / "policy.yaml", tmp_path / "policy.yaml")
    shutil.copy(FIXTURES / "users.yaml", tmp_path / "users.yaml")
    return d


@pytest.fixture
def books(exec_dir: Path) -> Ledger:
    ledger = Ledger(exec_dir / "books.db")
    ledger.import_dir(DATA / BASIS)
    yield ledger
    ledger.close()


def _cfg() -> ExecutorConfig:
    return ExecutorConfig(
        executor=LEDGER_EXECUTOR,
        self_id=LEDGER_EXEC,
        policy_path="../policy.yaml",
        users_path="../users.yaml",
        sweep_interval_seconds=5,
        peer_state_db="switch.db",
    )


def _seed_open_plan(exec_dir: Path, books: Ledger) -> None:
    """What sindarin-switch's state looks like once GOALS-1 is approved and its room is open."""
    policy = load_policy(FIXTURES / "policy.yaml")
    p = goals_proposal(books)
    ctx = Context(
        executor="sindarin-switch",
        self_id="@switch-agent-sindarin-switch:test",
        policy=policy,
        users=load_users(FIXTURES / "users.yaml"),
        view=None,  # type: ignore[arg-type]
        now=T0,
    )
    plan = _goals_plan(ctx, p, policy.rule("goals"))
    snap = make_snapshot("GOALS-1", "@switch-agent-planner:test", p, plan)
    store = Store(exec_dir / "switch.db")
    store.insert_artifact(
        Artifact(
            id="GOALS-1",
            kind=Kind.GOALS,
            rule="goals",
            room_id=FINANCE,
            thread_id=ASK,
            proposal_message_id="m-goals-1",
            proposer_id="@switch-agent-planner:test",
            proposer_name="planner",
            proposal_json=p.model_dump_json(by_alias=True),
            snapshot_json=snap.canonical(),
            snapshot_sha=snap.hash,
            created_at=T0,
            expires_at=T0 + timedelta(hours=24),
        )
    )
    store.transition("GOALS-1", State.APPROVED, approver_id="@switch-user-john:test")
    store.transition("GOALS-1", State.OPENING, attempts=1)
    store.transition(
        "GOALS-1",
        State.OPEN,
        result_json=json.dumps({"room_id": PERIOD_ROOM, "room_name": f"plan-{PERIOD}"}),
    )
    store.close()


async def test_service_end_to_end(exec_dir: Path, books: Ledger):
    _seed_open_plan(exec_dir, books)
    seq = iter(range(1, 100))

    def msg(room, sender, name, mid, body, addressed, thread=None):
        return message_envelope(
            room_id=room,
            sequence=next(seq),
            sender=sender,
            sender_name=name,
            message_id=mid,
            body=body,
            addressed=addressed,
            thread_id=thread,
        )

    script = [
        "ping",
        msg(
            PLANNING,
            PLANNER_AGENT,
            "planner",
            "m-budget",
            proposal_text(budget_proposal(books)),
            True,
        ),
        "ping",
        msg(
            PLANNING, ISAAC, "Isaac", "m-ok", "@sindarin-ledger approve BUDGET-1", True, "m-budget"
        ),
        "ping",
        1.5,
        {"event": "evicted", "data": {"code": "taken_over"}},
    ]
    bridge = Bridge(script)
    server = await serve(bridge.handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    posts: list[dict] = []
    connected_room: dict[str, str] = {}

    def http_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {API_KEY}"
        body = json.loads(request.content) if request.content else {}
        if request.url.path.endswith("/message"):
            posts.append(body)
            return httpx.Response(200, json={"ok": True})
        if request.url.path.endswith("/ops/connect_to_room"):
            assert request.headers["X-Switch-Connection-Id"] == "conn-1"
            connected_room["id"] = body["room_id"]
            return httpx.Response(200, json={"result": {"room_id": body["room_id"]}})
        if request.url.path.endswith("/ops/post_message"):
            posts.append(
                {
                    "room_id": connected_room["id"],
                    "content": body["body"],
                    "thread_id": body["thread_id"],
                }
            )
            return httpx.Response(200, json={"result": {"event_id": "sw_x"}})
        return httpx.Response(404)

    env = Env(
        bridge_url=f"http://127.0.0.1:{port}",
        agent_id="agent-1",
        api_key=API_KEY,
        connection_id="conn-1",
    )
    client = SwitchClient(env.bridge_url, env.agent_id, env.api_key)
    client._http = httpx.Client(transport=httpx.MockTransport(http_handler))
    service = ExecutorService(exec_dir, env, _cfg(), client, {"ledger": LedgerAdapter(books)})
    try:
        code = await asyncio.wait_for(service.run(), 20)
    finally:
        server.close()
        await server.wait_closed()
        client.close()

    assert code == 2  # taken_over → exit non-zero, never reconnect
    assert len(bridge.params) == 1 and bridge.pongs == 3
    store = Store(exec_dir / "ledger.db")
    a = store.get("BUDGET-1")
    assert a is not None and a.state is State.RECORDED and a.approver_name == "Isaac"
    assert store.get_value("cursor") == "2" and store.get_value("generation") == "1"
    assert [e.event for e in store.audit_all() if e.event != "stage_line"] == [
        "proposal_accepted",
        "approved",
        "applied",
    ]
    store.close()
    assert [r["department"] for r in books.budget(PERIOD)] == [
        "engineering",
        "general-admin",
        "marketing",
        "sales",
    ]
    assert books.targets(PERIOD)["cost_cap_cents"] == 1600000
    texts = [p["content"] for p in posts]
    # the first sweep found the open goals and called the planner, once
    stage = [t for t in texts if t.startswith("**GOALS-1** approved · revenue target")]
    assert len(stage) == 1 and stage[0].endswith("@planner propose the budget.")
    pending = next(t for t in texts if t.startswith("**BUDGET-1** pending"))
    assert pending.startswith("**BUDGET-1** pending · 4 lines · 16,000.00 USD under the cap")
    assert {"room_id": PLANNING, "content": pending, "thread_id": "m-budget"} in posts
    assert any(
        t.startswith("**BUDGET-1** approved by Isaac · recorded 16,000.00 USD") for t in texts
    )
    assert all(API_KEY not in t for t in texts)
    # room names were learned from connection_state
    assert service.effector.config.room_names[PERIOD_ROOM] == f"#plan-{PERIOD}"


def test_build_adapters_ledger_and_switch_requires_gateway(exec_dir: Path, books: Ledger):
    cfg = _cfg()
    adapters = build_adapters(Env("http://x", "a", "k", "c"), cfg, exec_dir)
    assert isinstance(adapters["ledger"], LedgerAdapter)
    assert adapters["ledger"].ledger.periods() == [BASIS]  # the executor's own writable handle
    with pytest.raises((httpx.HTTPError, GatewayError)):
        build_adapters(
            Env("http://127.0.0.1:9", "a", "k", "c", gateway_email="e", gateway_password="p"),
            ExecutorConfig(executor="sindarin-switch"),
            exec_dir,
        )


async def test_service_thread_replies_off(exec_dir: Path, books: Ledger):
    env = Env(bridge_url="http://x", agent_id="a", api_key="k", connection_id="c")
    cfg = _cfg().model_copy(update={"thread_replies": False})
    posts = []

    def http_handler(request: httpx.Request) -> httpx.Response:
        posts.append(json.loads(request.content))
        return httpx.Response(200, json={})

    client = SwitchClient(env.bridge_url, env.agent_id, env.api_key)
    client._http = httpx.Client(transport=httpx.MockTransport(http_handler))
    service = ExecutorService(exec_dir, env, cfg, client, {"ledger": LedgerAdapter(books)})
    service._post(PLANNING, "hello", "m-root")
    assert posts == [{"room_id": PLANNING, "content": "hello"}]
    service.store.close()
    client.close()


def test_agents_open_the_ledger_read_only(tmp_path, exec_dir: Path, books: Ledger):
    """The agents read the file sindarin-ledger writes and can never write it (design v5 §4)."""
    from sindarin.agents.service import ledger_for
    from sindarin.ledger import LedgerError

    save_config(exec_dir / "ledger.yaml", _cfg())
    handle = ledger_for(tmp_path)
    assert handle is not None and handle.periods() == [BASIS]
    with pytest.raises(LedgerError, match="read-only"):
        handle.add_sale("S-1", "2026-10-30", "X", 1)
    handle.close()
    (exec_dir / "books.db").unlink()
    assert ledger_for(tmp_path) is None
