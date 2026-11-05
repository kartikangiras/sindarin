"""Mattermost REST client, used by `sindarin bootstrap` and the §12.4 verification only (DDD §17).

Creates the demo people, puts them in the team and the bridged channels, posts as them to learn
participant ids, and pins the board. Nothing in the executors or the agents imports this module:
their only surface is Switch. Never logs a password or a token.
"""

from __future__ import annotations

from typing import Any

import httpx


class MattermostError(Exception):
    def __init__(self, status: int, detail: str, path: str) -> None:
        super().__init__(f"Mattermost HTTP {status} {path}: {detail}")
        self.status = status
        self.detail = detail
        self.path = path


def _detail(r: httpx.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        return r.text[:200]
    if isinstance(body, dict):
        return str(body.get("message") or body.get("id") or body)[:200]
    return str(body)[:200]


class MattermostClient:
    def __init__(self, url: str, *, timeout: float = 30.0) -> None:
        self.url = url.rstrip("/")
        self._http = httpx.Client(base_url=f"{self.url}/api/v4", timeout=timeout)
        self.user: dict[str, Any] | None = None

    @classmethod
    def login(cls, url: str, login_id: str, password: str) -> MattermostClient:
        c = cls(url)
        r = c._http.post("/users/login", json={"login_id": login_id, "password": password})
        if r.status_code >= 400:
            raise MattermostError(r.status_code, _detail(r), "/users/login")
        token = r.headers.get("Token")
        if not token:
            raise MattermostError(r.status_code, "login returned no token", "/users/login")
        c._http.headers["Authorization"] = f"Bearer {token}"
        c.user = r.json()
        return c

    def close(self) -> None:
        self._http.close()

    def _req(self, method: str, path: str, *, ok: tuple[int, ...] = (200, 201), **kw: Any) -> Any:
        r = self._http.request(method, path, **kw)
        if r.status_code not in ok:
            raise MattermostError(r.status_code, _detail(r), path)
        if not r.content:
            return None
        return r.json()

    def ping(self) -> Any:
        return self._req("GET", "/system/ping")

    # -- users ----------------------------------------------------------------------------------

    def user_by_username(self, username: str) -> dict[str, Any] | None:
        r = self._http.get(f"/users/username/{username}")
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise MattermostError(r.status_code, _detail(r), f"/users/username/{username}")
        return r.json()

    def ensure_user(
        self, username: str, email: str, password: str, display: str
    ) -> tuple[dict[str, Any], bool]:
        """(user, created)."""
        existing = self.user_by_username(username)
        if existing is not None:
            return existing, False
        user = self._req(
            "POST",
            "/users",
            json={
                "username": username,
                "email": email,
                "password": password,
                "first_name": display,
                "nickname": display,
            },
        )
        return user, True

    # -- teams and channels ---------------------------------------------------------------------

    def team_by_name(self, name: str) -> dict[str, Any]:
        return self._req("GET", f"/teams/name/{name}")

    def add_to_team(self, team_id: str, user_id: str) -> None:
        self._req(
            "POST", f"/teams/{team_id}/members", json={"team_id": team_id, "user_id": user_id}
        )

    def channel_by_name(self, team_id: str, name: str) -> dict[str, Any] | None:
        r = self._http.get(
            f"/teams/{team_id}/channels/name/{name}", params={"include_deleted": "false"}
        )
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise MattermostError(
                r.status_code, _detail(r), f"/teams/{team_id}/channels/name/{name}"
            )
        return r.json()

    def add_to_channel(self, channel_id: str, user_id: str) -> None:
        self._req("POST", f"/channels/{channel_id}/members", json={"user_id": user_id})

    # -- posts ----------------------------------------------------------------------------------

    def create_post(
        self, channel_id: str, message: str, root_id: str | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"channel_id": channel_id, "message": message}
        if root_id:
            body["root_id"] = root_id
        return self._req("POST", "/posts", json=body)

    def posts(self, channel_id: str, per_page: int = 30) -> list[dict[str, Any]]:
        data = self._req("GET", f"/channels/{channel_id}/posts", params={"per_page": per_page})
        order = data.get("order", [])
        posts = data.get("posts", {})
        return [posts[i] for i in order if i in posts]

    def pin(self, post_id: str) -> None:
        self._req("POST", f"/posts/{post_id}/pin")

    def pinned(self, channel_id: str) -> list[dict[str, Any]]:
        data = self._req("GET", f"/channels/{channel_id}/pinned")
        order = data.get("order", [])
        posts = data.get("posts", {})
        return [posts[i] for i in order if i in posts]
