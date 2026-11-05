"""Switch Gateway client (DDD §12, §17): the operator side of Switch.

Session-cookie HTTP against `/gateway/*`: sign in, users, registration keys, agent addressing and
bridged rooms (`/gateway/rooms`, `/gateway/collaborations`). Used by `sindarin bootstrap`, by the
spike and by the Switch rooms adapter. Never logs or returns passwords or keys beyond what the
caller passed in.

Routes marked [verify] are from the Gateway OpenAPI and the Switch docs; §12.4 confirms them live.
"""

from __future__ import annotations

import json
from typing import Any

import httpx


class GatewayError(Exception):
    """A Gateway refusal: `status` and the server's `detail`, nothing else."""

    def __init__(self, status: int, detail: str, path: str) -> None:
        super().__init__(f"HTTP {status} {path}: {detail}")
        self.status = status
        self.detail = detail
        self.path = path


def _detail(r: httpx.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        return r.text[:200]
    if isinstance(body, dict):
        d = body.get("detail", body)
        if isinstance(d, dict):
            return f"{d.get('code', '')} {d.get('message', '')}".strip() or json.dumps(d)[:200]
        return str(d)[:200]
    return json.dumps(body)[:200]


class GatewayClient:
    COOKIE = "switch_auth"

    def __init__(self, api_url: str, *, timeout: float = 30.0) -> None:
        self.api_url = api_url.rstrip("/")
        self._http = httpx.Client(base_url=self.api_url, timeout=timeout)
        self.user: dict[str, Any] | None = None

    # -- auth ---------------------------------------------------------------------------------

    @classmethod
    def login(cls, api_url: str, email: str, password: str) -> GatewayClient:
        c = cls(api_url)
        r = c._http.post("/gateway/auth/login", json={"email": email, "password": password})
        if r.status_code >= 400:
            raise GatewayError(r.status_code, _detail(r), "/gateway/auth/login")
        token = r.cookies.get(cls.COOKIE)
        if not token:
            raise GatewayError(
                r.status_code, "login returned no session cookie", "/gateway/auth/login"
            )
        c._http.headers["Cookie"] = f"{cls.COOKIE}={token}"
        c.user = c.me()
        return c

    def close(self) -> None:
        self._http.close()

    def _req(self, method: str, path: str, *, ok: tuple[int, ...] = (200, 201), **kw: Any) -> Any:
        r = self._http.request(method, path, **kw)
        if r.status_code not in ok:
            raise GatewayError(r.status_code, _detail(r), path)
        if r.status_code == 204 or not r.content:
            return None
        return r.json()

    def me(self) -> dict[str, Any]:
        return self._req("GET", "/gateway/auth/me")

    # -- users (admin) ------------------------------------------------------------------------

    def list_users(self) -> list[dict[str, Any]]:
        return self._req("GET", "/gateway/users")

    def create_user(self, name: str, email: str, password: str, role: str = "user") -> dict:
        return self._req(
            "POST",
            "/gateway/users",
            json={"name": name, "email": email, "password": password, "role": role},
        )

    def ensure_user(self, name: str, email: str, password: str) -> tuple[dict, bool]:
        """(user, created)."""
        for u in self.list_users():
            if u.get("email", "").lower() == email.lower():
                return u, False
        return self.create_user(name, email, password), True

    # -- agents (admin / owner) ---------------------------------------------------------------

    def create_registration_key(self, label: str) -> str:
        data = self._req("POST", "/gateway/api-keys", json={"label": label})
        key = data.get("key") if isinstance(data, dict) else None
        if not key:
            raise GatewayError(500, "api-keys response had no key", "/gateway/api-keys")
        return key

    def list_agents(self) -> list[dict[str, Any]]:
        data = self._req("GET", "/gateway/agents")
        if isinstance(data, dict):
            data = data.get("agents", data.get("items", []))
        return list(data or [])

    def delete_agent_by_name(self, name: str) -> None:
        self._req("DELETE", f"/gateway/agents/by-name/{name}", ok=(200, 204))

    def set_addressing_open(self, agent_id: str) -> dict:
        return self._req(
            "PUT", f"/gateway/agents/{agent_id}/addressing-policy", json={"policy": None}
        )

    def set_addressing_rooms(self, agent_id: str, room_ids: list[str]) -> dict:
        """Layer 0 (DDD §4.1, §5): the agent can be addressed only in these rooms. Verified
        2026-11-05: a mention elsewhere still reaches the agent as an event with
        `addressed: false`, and Switch posts the refusal in the room on the agent's behalf."""
        rules = [{"rooms": sorted(set(room_ids))}] if room_ids else []
        return self._req(
            "PUT",
            f"/gateway/agents/{agent_id}/addressing-policy",
            json={"policy": {"rules": rules}},
        )

    def addressing_rooms(self, agent_id: str) -> list[str] | None:
        """The rooms an agent's policy allows, [] for a policy with other rule shapes, None for
        an open (null) policy."""
        agent = next((a for a in self.list_agents() if a.get("id") == agent_id), None)
        if agent is None:
            raise GatewayError(404, f"agent {agent_id} not found", "/gateway/agents")
        policy = agent.get("addressing_policy")
        if not policy:
            return None
        rooms: list[str] = []
        for rule in policy.get("rules") or []:
            r = rule.get("rooms", "*")
            if isinstance(r, list):
                rooms += [str(x) for x in r]
            elif r == "*":
                return None
        return rooms

    # -- bridged rooms (collab profile) — verified live 2026-11-05: POST /gateway/rooms creates a
    # bridged Mattermost channel; GET /gateway/rooms/{id} carries agent_ids, connected_user_names,
    # instructions, external_channel_id ------------------------------------------------------

    def collaborations(self) -> list[dict[str, Any]]:
        """Bridges (Mattermost, Slack) known to the server; the seeder registers Mattermost as
        the default."""
        data = self._req("GET", "/gateway/collaborations")
        if isinstance(data, dict):
            data = data.get("collaborations", data.get("items", data.get("bridges", [])))
        return list(data or [])

    def create_bridge(
        self, *, bridge_type: str, display_name: str, connection_config: dict[str, Any]
    ) -> dict[str, Any]:
        """`POST /gateway/collaborations` (Gateway OpenAPI, 2026-11-06): connect a messaging app.
        For Mattermost the config is url, admin_user, admin_password, team_name, public_url."""
        data = self._req(
            "POST",
            "/gateway/collaborations",
            json={
                "bridge_type": bridge_type,
                "display_name": display_name,
                "connection_config": connection_config,
                "set_as_default": True,
                "channel_creation_enabled": True,
            },
        )
        return data.get("bridge", data) if isinstance(data, dict) else data

    def set_default_bridge(self, bridge_id: str) -> None:
        self._req("POST", f"/gateway/collaborations/{bridge_id}/default", ok=(200, 201, 204))

    def default_bridge_id(self) -> str | None:
        bridges = self.collaborations()
        for b in bridges:
            if b.get("is_default") or b.get("default"):
                return b.get("id") or b.get("bridge_id")
        if bridges:
            return bridges[0].get("id") or bridges[0].get("bridge_id")
        return None

    def list_rooms(self) -> list[dict[str, Any]]:
        data = self._req("GET", "/gateway/rooms")
        if isinstance(data, dict):
            data = data.get("rooms", data.get("items", []))
        return list(data or [])

    def get_room(self, room_id: str) -> dict[str, Any]:
        data = self._req("GET", f"/gateway/rooms/{room_id}")
        return data.get("room", data) if isinstance(data, dict) else data

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
        external_channel_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "name": name,
            "description": description,
            "instructions": instructions,
            "bridge_id": bridge_id,
            "channel_type": channel_type,
            "agent_ids": agent_ids,
            "user_names": user_names,
        }
        if external_channel_id:
            body["external_channel_id"] = external_channel_id
        data = self._req("POST", "/gateway/rooms", json=body)
        return data.get("room", data) if isinstance(data, dict) else data

    def room_members(self, room_id: str) -> dict[str, list[str]]:
        """{"agents": [gateway agent ids], "users": [user names]} as the Gateway reports them."""
        room = self.get_room(room_id)
        agents = [
            a.get("id") or a.get("agent_id") if isinstance(a, dict) else a
            for a in (room.get("agents") or room.get("agent_ids") or [])
        ]
        users = [
            u.get("name") or u.get("user_name") or u.get("username") if isinstance(u, dict) else u
            for u in (
                room.get("connected_user_names")  # verified 2026-11-05 on switch-core 0.31
                or room.get("user_names")
                or room.get("users")
                or room.get("members")
                or []
            )
        ]
        return {"agents": [str(a) for a in agents if a], "users": [str(u) for u in users if u]}

    def add_room_agents(self, room_id: str, agent_ids: list[str]) -> None:
        self._req("POST", f"/gateway/rooms/{room_id}/agents", json={"agent_ids": agent_ids})

    def add_room_users(self, room_id: str, user_names: list[str]) -> None:
        self._req("POST", f"/gateway/rooms/{room_id}/users", json={"user_names": user_names})
