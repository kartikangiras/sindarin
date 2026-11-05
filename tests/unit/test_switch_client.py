"""Switch client: HTTP shapes via mock transport; WebSocket behaviour via a local fake server
(SPEC §5.1–§5.4): ping answered with cursor, connection_state read, recoverable eviction
reconnects with start_from and expected_generation, taken_over is fatal."""

from __future__ import annotations

import asyncio
import json
import urllib.parse

import httpx
import pytest
import websockets
from websockets.asyncio.server import serve

from sindarin.switch.client import ConnectionState, FatalEviction, SwitchClient, SwitchError
from sindarin.switch.events import MessageEvent, message_envelope

API_KEY = "switch-key-TEST-SECRET"


def _client(handler) -> SwitchClient:
    c = SwitchClient("http://bridge.test", "agent-1", API_KEY)
    c._http = httpx.Client(transport=httpx.MockTransport(handler), timeout=5)
    return c


def test_register_and_409():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["Authorization"]
        seen["body"] = json.loads(request.content)
        if seen["body"]["name"] == "taken":
            return httpx.Response(409, json={"error": "exists"})
        return httpx.Response(200, json={"id": "agent-9", "api_key": "k-9"})

    c = _client(handler)
    out = c.register("reg-key", name="sindarin-exec")
    assert out == {"id": "agent-9", "api_key": "k-9"}
    assert seen["auth"] == "Bearer reg-key"
    assert seen["body"]["integration_profile"]["connection_model"] == "always_on"
    assert "overwrite" not in seen["body"]
    assert c.agent_id == "agent-9"
    with pytest.raises(SwitchError, match="409"):
        c.register("reg-key", name="taken")


def test_post_in_thread_uses_connect_to_room_then_post_message():
    calls: list[tuple[str, str | None, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append((request.url.path, request.headers.get("X-Switch-Connection-Id"), body))
        assert request.headers["Authorization"] == f"Bearer {API_KEY}"
        if request.url.path.endswith("/ops/connect_to_room"):
            return httpx.Response(200, json={"result": {"room_id": body["room_id"]}})
        if request.url.path.endswith("/ops/post_message"):
            return httpx.Response(200, json={"result": {"event_id": "sw_new"}})
        return httpx.Response(500, json={"error": "unexpected"})

    c = _client(handler)
    assert c.post("!r:s", "hello", "m-root", connection_id="conn-1") == {"event_id": "sw_new"}
    assert calls == [
        ("/agents/agent-1/ops/connect_to_room", "conn-1", {"room_id": "!r:s"}),
        ("/agents/agent-1/ops/post_message", "conn-1", {"body": "hello", "thread_id": "m-root"}),
    ]


def test_post_without_connection_or_thread_is_top_level_rest():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"ok": True, "event_id": "sw_1"})

    c = _client(handler)
    # a thread id without a live connection cannot thread: the route ignores thread ids
    assert c.post("!r:s", "hello", "m-root")["ok"] is True
    assert c.post("!r:s", "plain", None, connection_id="conn-1")["ok"] is True
    assert calls == [
        ("/agents/agent-1/message", {"room_id": "!r:s", "content": "hello"}),
        ("/agents/agent-1/message", {"room_id": "!r:s", "content": "plain"}),
    ]


def test_post_in_thread_falls_back_to_top_level_when_ops_fail():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if "/ops/" in request.url.path:
            return httpx.Response(409, json={"detail": "unknown connection"})
        return httpx.Response(200, json={"ok": True})

    c = _client(handler)
    assert c.post("!r:s", "hello", "m-root", connection_id="dead") == {"ok": True}
    assert calls == ["/agents/agent-1/ops/connect_to_room", "/agents/agent-1/message"]


def test_post_other_4xx_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "no such room"})

    c = _client(handler)
    with pytest.raises(SwitchError, match="404"):
        c.post("!r:s", "hello")


def test_ops_and_errors_hide_key():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/ops"):
            return httpx.Response(200, json={"operations": {"post_message": {"input_schema": {}}}})
        if request.url.path.endswith("/ops/update_agent_detail"):
            assert json.loads(request.content) == {"agent_id": "agent-1", "addressing": "anyone"}
            return httpx.Response(200, json={"result": {"addressing": "anyone"}})
        if request.url.path.endswith("/ops/list_rooms"):
            assert request.headers["X-Switch-Connection-Id"] == "conn-1"
            return httpx.Response(403, json={"error": "denied"})
        return httpx.Response(500)

    c = _client(handler)
    assert "post_message" in c.ops_catalog()
    assert c.set_addressing("anyone") == {"addressing": "anyone"}
    with pytest.raises(SwitchError) as exc:
        c.list_rooms(connection_id="conn-1")
    assert "403" in str(exc.value) and API_KEY not in str(exc.value)


def test_ws_url():
    c = SwitchClient("https://bridge.test/", "agent-1", API_KEY)
    st = ConnectionState(connection_id="conn-1")
    url = c.ws_url(st)
    assert url.startswith("wss://bridge.test/agents/agent-1/connection/ws?")
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert q["scope"] == ["all"] and q["filter"] == ["all"] and q["start_from"] == ["head"]
    assert "expected_generation" not in q
    st.cursor, st.generation = 4813, 2
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(c.ws_url(st)).query)
    assert q["start_from"] == ["4813"] and q["expected_generation"] == ["2"]


# -- fake websocket server -----------------------------------------------------------------------


class FakeBridge:
    """Scripted server: records query params and pongs; sends a script per connection attempt."""

    def __init__(self, live_shape: bool = False):
        self.connections: list[dict] = []
        self.pongs: list[list[dict]] = []
        self.scripts: list[list[dict | str]] = []
        self.live_shape = live_shape  # switch-core 0.31: {"event": ..., "data": {...}}

    def control(self, name: str, **data):
        if self.live_shape:
            return {"event": name, "data": data}
        return {"type": name, **data}

    async def handler(self, ws):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(ws.request.path).query)
        assert ws.request.headers["Authorization"] == f"Bearer {API_KEY}"
        attempt = len(self.connections)
        self.connections.append({k: v[0] for k, v in q.items()})
        self.pongs.append([])
        script = list(self.scripts[attempt] if attempt < len(self.scripts) else [])
        if script and script[0] == "no_state":
            script.pop(0)
        else:
            await ws.send(
                json.dumps(
                    self.control(
                        "connection_state",
                        rooms=[{"room_id": "!r:s"}],
                        cursor=10,
                        generation=attempt + 1,
                        heartbeat_interval_seconds=2,
                    )
                )
            )
        for step in script:
            if step == "ping":
                await ws.send(json.dumps(self.control("ping")))
                reply = json.loads(await asyncio.wait_for(ws.recv(), 2))
                self.pongs[attempt].append(reply)
            elif step == "close":
                await ws.close()
                return
            else:
                await ws.send(json.dumps(step))
        await asyncio.sleep(5)


async def _run_bridge(bridge: FakeBridge):
    server = await serve(bridge.handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


@pytest.mark.parametrize("live_shape", [False, True], ids=["type-envelope", "event-envelope"])
async def test_ws_ping_pong_resume_and_fatal_eviction(live_shape):
    bridge = FakeBridge(live_shape=live_shape)
    msg = message_envelope(
        room_id="!r:s",
        sequence=11,
        sender="@u:s",
        sender_name="Alice",
        message_id="m1",
        body="hi",
        addressed=False,
    )
    bridge.scripts = [
        ["ping", msg, "ping", bridge.control("evicted", code="heartbeat_lapsed")],
        ["ping", "close"],
        [
            bridge.control("gap", resumed_at=40),
            "ping",
            bridge.control("evicted", code="taken_over"),
        ],
    ]
    server, port = await _run_bridge(bridge)
    try:
        client = SwitchClient(f"http://127.0.0.1:{port}", "agent-1", API_KEY)
        state = ConnectionState(connection_id="conn-1")
        seen: list = []

        async def on_frame(frame):
            seen.append(frame)
            if isinstance(frame, MessageEvent):
                state.cursor = frame.sequence  # what the handler does after processing

        with pytest.raises(FatalEviction, match="taken_over"):
            await asyncio.wait_for(client.run_connection(state, on_frame), 20)
        client.close()
    finally:
        server.close()
        await server.wait_closed()

    assert [c["start_from"] for c in bridge.connections] == ["head", "11", "11"]
    assert [c.get("expected_generation") for c in bridge.connections] == [None, "1", "2"]
    assert all(c["scope"] == "all" and c["filter"] == "all" for c in bridge.connections)
    # pong carries the highest processed cursor: 10 from connection_state, then 11 after the message
    assert bridge.pongs[0] == [{"type": "pong", "cursor": 10}, {"type": "pong", "cursor": 11}]
    assert bridge.pongs[1] == [{"type": "pong", "cursor": 11}]
    assert bridge.pongs[2] == [{"type": "pong", "cursor": 40}]  # gap moved the cursor
    assert state.generation == 3 and state.rooms == [{"room_id": "!r:s"}]
    assert any(isinstance(f, MessageEvent) and f.message_id == "m1" for f in seen)
    assert state.pings_answered == 4


async def test_ws_bad_key_is_error():
    async def handler(ws):
        await ws.close()

    async def process_request(connection, request):
        return connection.respond(401, "nope")

    server = await serve(handler, "127.0.0.1", 0, process_request=process_request)
    port = server.sockets[0].getsockname()[1]
    try:
        client = SwitchClient(f"http://127.0.0.1:{port}", "agent-1", "bad")
        with pytest.raises(SwitchError, match="401"):
            await asyncio.wait_for(
                client.run_connection(
                    ConnectionState(connection_id="c"), lambda f: asyncio.sleep(0)
                ),
                10,
            )
        client.close()
    finally:
        server.close()
        await server.wait_closed()


async def test_ws_stop_event_ends_loop():
    bridge = FakeBridge()
    bridge.scripts = [["ping"]]
    server, port = await _run_bridge(bridge)
    try:
        client = SwitchClient(f"http://127.0.0.1:{port}", "agent-1", API_KEY)
        stop = asyncio.Event()
        state = ConnectionState(connection_id="conn-1")
        task = asyncio.create_task(
            client.run_connection(state, lambda f: asyncio.sleep(0), stop=stop)
        )
        await asyncio.sleep(0.5)
        stop.set()
        task.cancel()
        with pytest.raises((asyncio.CancelledError, websockets.ConnectionClosed)):
            await task
        client.close()
    finally:
        server.close()
        await server.wait_closed()


async def test_ws_handler_error_and_refused_do_not_kill_the_loop():
    bridge = FakeBridge()
    msg = message_envelope(
        room_id="!r:s",
        sequence=11,
        sender="@u:s",
        sender_name="Alice",
        message_id="m1",
        body="hi",
        addressed=False,
    )
    bridge.scripts = [
        [msg, "ping", {"type": "refused", "reason": "stale generation"}],  # handler raises on msg
        ["no_state", {"type": "refused", "reason": "unknown cursor"}],
        ["ping", {"type": "evicted", "code": "taken_over"}],
    ]
    server, port = await _run_bridge(bridge)
    try:
        client = SwitchClient(f"http://127.0.0.1:{port}", "agent-1", API_KEY)
        state = ConnectionState(connection_id="conn-1", cursor=5, generation=9)
        seen = []

        async def on_frame(frame):
            seen.append(frame)
            if isinstance(frame, MessageEvent):
                raise RuntimeError("handler bug")

        with pytest.raises(FatalEviction):
            await asyncio.wait_for(client.run_connection(state, on_frame), 30)
        client.close()
    finally:
        server.close()
        await server.wait_closed()
    # the message handler raised, yet the ping right after it was still answered
    assert bridge.pongs[0] == [{"type": "pong", "cursor": 5}]
    assert [c["start_from"] for c in bridge.connections] == ["5", "5", "head"]
    assert [c.get("expected_generation") for c in bridge.connections] == ["9", None, None]
    assert any(isinstance(f, MessageEvent) for f in seen)
