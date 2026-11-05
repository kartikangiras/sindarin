"""Switch agent protocol client: register, connect, ops, post (SPEC §5.1–§5.4).

HTTP via httpx (sync; the service wraps calls in threads). Events via websockets. Every frame type
is logged at DEBUG. The API key is never logged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import urllib.parse
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import websockets

from sindarin import __version__
from sindarin.switch.events import ControlFrame, Frame, parse_frame

log = logging.getLogger("sindarin.switch")

RECOVERABLE_EVICTIONS = frozenset({"heartbeat_lapsed", "no_stream", "unfenced", "connection_error"})


class SwitchError(Exception):
    pass


class FatalEviction(SwitchError):
    """`taken_over` or `closed`: stop, never reconnect (§5.3)."""


@dataclass
class ConnectionState:
    connection_id: str
    cursor: int | None = None
    generation: int | None = None
    rooms: list[dict[str, Any]] = field(default_factory=list)
    heartbeat_interval_seconds: float = 2.0
    connected: bool = False
    pings_answered: int = 0
    raw_connection_state: dict[str, Any] = field(default_factory=dict)


def _clean(exc: Exception) -> str:
    """Error text safe to show: class name plus status, never headers."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code} {exc.request.method} {exc.request.url.path}"
    return exc.__class__.__name__


class SwitchClient:
    def __init__(
        self,
        bridge_url: str,
        agent_id: str = "",
        api_key: str = "",
        *,
        timeout: float = 20.0,
        client_name: str = "sindarin",
    ) -> None:
        self.bridge_url = bridge_url.rstrip("/")
        self.agent_id = agent_id
        self._api_key = api_key
        self.client_name = client_name
        self.thread_posts_ok = True  # flipped off after the first rejected in-thread post
        self._http = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._http.close()

    # -- http helpers -----------------------------------------------------------------------

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        h = {"Accept": "application/json"}
        if self._api_key:
            h["Authorization"] = f"Bearer {self._api_key}"
        if extra:
            h.update(extra)
        return h

    def _request(
        self, method: str, path: str, *, json: Any = None, headers: dict | None = None
    ) -> Any:
        url = f"{self.bridge_url}{path}"
        try:
            r = self._http.request(method, url, json=json, headers=self._headers(headers))
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:300]
            raise SwitchError(f"{_clean(exc)}: {body}") from None
        except httpx.HTTPError as exc:
            raise SwitchError(f"{_clean(exc)} for {method} {path}") from None
        if not r.content:
            return None
        try:
            return r.json()
        except ValueError:
            return r.text

    # -- §5.1 registration ------------------------------------------------------------------

    def health(self) -> Any:
        return self._request("GET", "/health")

    def register(
        self,
        registration_key: str,
        *,
        name: str = "sindarin-exec",
        display_name: str = "Sindarin Executor",
        description: str = (
            "Deterministic executor for Sindarin. Verifies approvals and applies grants."
        ),
    ) -> dict[str, str]:
        body = {
            "name": name,
            "display_name": display_name,
            "description": description,
            "connector_type": "sindarin",
            "integration_profile": {
                "connection_model": "always_on",
                "message_exchange": True,
                "pre_invocation_mediation": [],
                "post_invocation_mediation": [],
                "event_reporting": [],
            },
        }
        url = f"{self.bridge_url}/agents"
        try:
            r = self._http.post(
                url,
                json=body,
                headers={
                    "Authorization": f"Bearer {registration_key}",
                    "Accept": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            raise SwitchError(f"{_clean(exc)} for POST /agents") from None
        if r.status_code == 409:
            raise SwitchError(
                "409: an agent with this name already exists. "
                "Reuse its id/key; never re-register with overwrite."
            )
        if r.status_code >= 400:
            raise SwitchError(f"HTTP {r.status_code} on POST /agents: {r.text[:300]}")
        data = r.json()
        if not data.get("id") or not data.get("api_key"):
            raise SwitchError(f"registration response lacks id/api_key: keys={sorted(data)}")
        self.agent_id = data["id"]
        self._api_key = data["api_key"]
        return {"id": data["id"], "api_key": data["api_key"]}

    # -- §5.4 operations ----------------------------------------------------------------------

    def ops_catalog(self) -> dict[str, Any]:
        data = self._request("GET", f"/agents/{self.agent_id}/ops")
        return data.get("operations", data) if isinstance(data, dict) else {}

    def op(
        self, name: str, args: dict[str, Any] | None = None, *, connection_id: str | None = None
    ) -> Any:
        headers = {"X-Switch-Connection-Id": connection_id} if connection_id else None
        data = self._request(
            "POST", f"/agents/{self.agent_id}/ops/{name}", json=args or {}, headers=headers
        )
        return data.get("result", data) if isinstance(data, dict) else data

    def set_addressing(self, mode: str) -> Any:
        return self.op("update_agent_detail", {"agent_id": self.agent_id, "addressing": mode})

    def list_rooms(self, connection_id: str | None = None) -> Any:
        return self.op("list_rooms", {}, connection_id=connection_id)

    # -- posting (no connection needed) -------------------------------------------------------

    def post(
        self,
        room_id: str,
        text: str,
        thread_id: str | None = None,
        *,
        connection_id: str | None = None,
    ) -> Any:
        """Post to a room. Top-level: `POST /agents/{id}/message` (no connection needed; the
        route ignores thread ids). In a thread: the `connect_to_room` + `post_message` operations
        on the live connection (SPEC v2 §5.3, verified 2026-11-01). If the ops path fails, the
        text still goes out top-level so a reply is never lost."""
        if thread_id and connection_id and self.thread_posts_ok:
            try:
                self.op("connect_to_room", {"room_id": room_id}, connection_id=connection_id)
                return self.op(
                    "post_message",
                    {"body": text, "thread_id": thread_id},
                    connection_id=connection_id,
                )
            except SwitchError as exc:
                log.warning("in-thread post via ops failed (%s); posting top-level", str(exc)[:80])
        body: dict[str, Any] = {"room_id": room_id, "content": text}
        return self._request("POST", f"/agents/{self.agent_id}/message", json=body)

    # -- §5.3 websocket -------------------------------------------------------------------------

    def ws_url(self, state: ConnectionState) -> str:
        q: dict[str, str] = {
            "connection_id": state.connection_id,
            "scope": "all",
            "filter": "all",
            "start_from": str(state.cursor) if state.cursor is not None else "head",
            "client": self.client_name,
            "client_version": __version__,
        }
        if state.generation is not None:
            q["expected_generation"] = str(state.generation)
        base = self.bridge_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        return f"{base}/agents/{self.agent_id}/connection/ws?{urllib.parse.urlencode(q)}"

    async def run_connection(
        self,
        state: ConnectionState,
        on_frame: Callable[[Frame], Awaitable[None]],
        *,
        stop: asyncio.Event | None = None,
    ) -> None:
        """Keep one connection open. Pings are answered before anything else. Recoverable
        evictions, refusals and socket drops reconnect with start_from=cursor and
        expected_generation; `taken_over`/`closed` raise FatalEviction. Handler errors are
        logged, never fatal."""
        backoff = 1.0
        while not (stop and stop.is_set()):
            url = self.ws_url(state)
            log.info(
                "connecting (start_from=%s, generation=%s)",
                state.cursor or "head",
                state.generation,
            )
            state.connected = False
            try:
                async with websockets.connect(
                    url,
                    additional_headers={"Authorization": f"Bearer {self._api_key}"},
                    ping_interval=None,  # Switch runs its own application-level ping
                    max_size=4 * 1024 * 1024,
                ) as ws:
                    await self._read_loop(ws, state, on_frame, stop)
            except FatalEviction:
                raise
            except websockets.InvalidStatus as exc:
                code = exc.response.status_code
                if code in (401, 403):
                    raise SwitchError(
                        f"websocket refused with HTTP {code}: check SWITCH_AGENT_API_KEY"
                    ) from None
                log.warning("websocket handshake failed with HTTP %s", code)
            except (TimeoutError, websockets.ConnectionClosed, OSError) as exc:
                log.warning("connection dropped: %s", exc.__class__.__name__)
            except Exception as exc:  # anything else is logged and retried, never fatal
                log.exception("connection error: %s", exc.__class__.__name__)
            if state.connected:
                backoff = 1.0  # reset only after a connection that reached connection_state
            state.connected = False
            if stop and stop.is_set():
                return
            delay = min(backoff, 30.0) * (0.8 + 0.4 * random.random())
            log.info("reconnecting in %.1fs", delay)
            await asyncio.sleep(delay)
            backoff = min(backoff * 2, 30.0)

    async def _read_loop(
        self,
        ws: Any,
        state: ConnectionState,
        on_frame: Callable[[Frame], Awaitable[None]],
        stop: asyncio.Event | None,
    ) -> None:
        async for raw in ws:
            if stop and stop.is_set():
                return
            try:
                frame = parse_frame(raw)
            except ValueError as exc:
                log.debug("unparseable frame: %s", exc)
                continue
            if not isinstance(frame, ControlFrame):
                await self._deliver(on_frame, frame)
                continue
            if frame.type == "ping":
                await ws.send(json.dumps({"type": "pong", "cursor": state.cursor or 0}))
                state.pings_answered += 1
                log.debug("ping → pong cursor=%s", state.cursor)
                continue
            log.debug("control frame %s", frame.type)
            if frame.type == "connection_state":
                state.connected = True
                state.raw_connection_state = frame.data
                state.rooms = list(frame.data.get("rooms") or [])
                if frame.data.get("generation") is not None:
                    state.generation = frame.data["generation"]
                if state.cursor is None and frame.data.get("cursor") is not None:
                    state.cursor = int(frame.data["cursor"])
                if frame.data.get("heartbeat_interval_seconds"):
                    state.heartbeat_interval_seconds = float(
                        frame.data["heartbeat_interval_seconds"]
                    )
                log.info(
                    "connection_state: %d room(s), cursor=%s, generation=%s",
                    len(state.rooms),
                    state.cursor,
                    state.generation,
                )
            elif frame.type == "gap":
                resumed = frame.data.get("resumed_at")
                log.warning("gap frame: events were missed; resuming at %s", resumed)
                if resumed is not None:
                    state.cursor = int(resumed)
            elif frame.type == "evicted":
                code = str(frame.data.get("code") or frame.data.get("reason") or "")
                reason = str(frame.data.get("reason") or "")
                if code == "taken_over" or "taken_over" in reason:
                    raise FatalEviction(
                        "evicted: taken_over — another process holds this connection_id; "
                        "not reconnecting"
                    )
                if code == "closed" or (code not in RECOVERABLE_EVICTIONS and "closed" in reason):
                    raise FatalEviction(f"evicted: closed — {reason or 'no reason given'}")
                log.warning("evicted (%s): reconnecting", code or reason)
                await self._deliver(on_frame, frame)
                return
            elif frame.type == "refused":
                # Usually a stale expected_generation or cursor after a bridge restart: drop the
                # generation first, then the cursor, and reconnect.
                reason = str(frame.data.get("reason") or frame.data)[:120]
                if state.generation is not None:
                    log.warning("refused (%s): dropping expected_generation", reason)
                    state.generation = None
                else:
                    log.warning("refused (%s): dropping cursor, resuming from head", reason)
                    state.cursor = None
                await self._deliver(on_frame, frame)
                return
            await self._deliver(on_frame, frame)

    @staticmethod
    async def _deliver(on_frame: Callable[[Frame], Awaitable[None]], frame: Frame) -> None:
        """A handler error must not take the connection down."""
        try:
            await on_frame(frame)
        except Exception:
            log.exception("frame handler failed for %s", type(frame).__name__)
