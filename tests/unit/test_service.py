"""End-to-end through ExecutorService for sindarin-github: fake WebSocket bridge + mock HTTP
posts + FakeGitHub. PR proposal → review → approve → merged, then a taken_over eviction stops
the process (DDD §7, §16.1)."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import httpx
import pytest
from validation.harness import (
    CODER_AGENT,
    FEATURE,
    FIXTURES,
    GITHUB_EXEC,
    ISAAC,
    RELEASES,
    REVIEWER_AGENT,
    pr_proposal,
    proposal_text,
    review_fence,
)
from websockets.asyncio.server import serve

from sindarin.adapters.fake import FakeGitHub
from sindarin.executor.config import Env, ExecutorConfig, save_config
from sindarin.executor.service import ExecutorService, build_adapters
from sindarin.names import GITHUB_EXECUTOR
from sindarin.schema import State
from sindarin.store import Store
from sindarin.switch.client import SwitchClient
from sindarin.switch.events import message_envelope
from sindarin.switch.gateway import GatewayError

API_KEY = "switch-key-TEST"


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
                            {"room_id": FEATURE, "name": "feat-csv-export"},
                            {"room_id": RELEASES, "name": "releases"},
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


def _cfg() -> ExecutorConfig:
    return ExecutorConfig(
        executor=GITHUB_EXECUTOR,
        self_id=GITHUB_EXEC,
        policy_path="../policy.yaml",
        users_path="../users.yaml",
        sweep_interval_seconds=5,
        github_mode="simulated",
    )


async def test_service_end_to_end(exec_dir: Path):
    gh = FakeGitHub()
    pr = gh.add_pull(41, title="CSV export of orders")
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
            FEATURE,
            CODER_AGENT,
            "coder",
            "m-pr",
            proposal_text(pr_proposal(head_sha=pr.head_sha)),
            True,
        ),
        msg(
            FEATURE,
            REVIEWER_AGENT,
            "reviewer",
            "m-rev",
            review_fence(head_sha=pr.head_sha),
            True,
            "m-pr",
        ),
        "ping",
        msg(FEATURE, ISAAC, "Isaac", "m-ok", "@sindarin-github approve PR-41", True, "m-pr"),
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
    service = ExecutorService(exec_dir, env, _cfg(), client, {"github": gh})
    try:
        code = await asyncio.wait_for(service.run(), 20)
    finally:
        server.close()
        await server.wait_closed()
        client.close()

    assert code == 2  # taken_over → exit non-zero, never reconnect
    assert len(bridge.params) == 1 and bridge.pongs == 3
    store = Store(exec_dir / "github.db")
    a = store.get("PR-41")
    assert a is not None and a.state is State.MERGED and a.approver_name == "Isaac"
    assert gh.pulls[41].merged and gh.count("execute") == 1
    assert store.get_value("cursor") == "3" and store.get_value("generation") == "1"
    assert [e.event for e in store.audit_all()] == [
        "proposal_accepted",
        "review_recorded",
        "approved",
        "applied",
    ]
    assert store.latest_review("PR-41")[0].met == 4
    store.close()
    texts = [p["content"] for p in posts]
    assert texts[0].startswith("**PR-41** pending · #41 CSV export of orders")
    assert posts[0] == {"room_id": FEATURE, "content": texts[0], "thread_id": "m-pr"}
    assert any(t.startswith("**PR-41** last review: **4 of 4**") for t in texts)
    assert any(t.startswith("**PR-41** approved by Isaac · merged as `") for t in texts)
    assert all(API_KEY not in t for t in texts)
    # feature room names were learned from connection_state
    assert service.effector.config.room_names[FEATURE] == "#feat-csv-export"


def test_build_adapters_simulated_and_switch_requires_gateway(exec_dir: Path, tmp_path: Path):
    cfg = _cfg()
    adapters = build_adapters(Env("http://x", "a", "k", "c"), cfg, exec_dir)
    assert adapters["github"].repo == "acme/sindarin-demo-app"  # from the policy's rules
    save_config(exec_dir / "github.yaml", cfg)
    with pytest.raises((httpx.HTTPError, GatewayError)):
        build_adapters(
            Env("http://127.0.0.1:9", "a", "k", "c", gateway_email="e", gateway_password="p"),
            ExecutorConfig(executor="sindarin-switch"),
            exec_dir,
        )


async def test_service_thread_replies_off(exec_dir: Path):
    env = Env(bridge_url="http://x", agent_id="a", api_key="k", connection_id="c")
    cfg = _cfg().model_copy(update={"thread_replies": False})
    posts = []

    def http_handler(request: httpx.Request) -> httpx.Response:
        posts.append(json.loads(request.content))
        return httpx.Response(200, json={})

    client = SwitchClient(env.bridge_url, env.agent_id, env.api_key)
    client._http = httpx.Client(transport=httpx.MockTransport(http_handler))
    service = ExecutorService(exec_dir, env, cfg, client, {"github": FakeGitHub()})
    service._post(FEATURE, "hello", "m-root")
    assert posts == [{"room_id": FEATURE, "content": "hello"}]
    service.store.close()
    client.close()
