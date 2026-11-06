#!/usr/bin/env python3
"""Remove the rooms a previous run opened (`plan-<period>`, `close-<period>`) from the Gateway and
Mattermost, so the next run opens a fresh one. Reads the stack env and the root .env like the
bootstrap does; the demo rooms (#acme-finance, #planning) are kept.

    uv run python scripts/reset_plan_rooms.py [--stack-env ~/.sindarin/switch-stack/.env]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from sindarin.bootstrap import MattermostTarget, ensure_bridge  # noqa: E402
from sindarin.executor.config import parse_env_text  # noqa: E402
from sindarin.switch.gateway import GatewayClient, GatewayError  # noqa: E402
from sindarin.switch.mattermost import MattermostClient  # noqa: E402


def _quiet(admin: GatewayClient, method: str, path: str, **kw):
    try:
        return admin._req(method, path, **kw)
    except GatewayError:
        return None


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--stack-env", type=Path, default=Path.home() / ".sindarin" / "switch-stack" / ".env"
    )
    p.add_argument("--api", default=None, help="Gateway base URL; default from the stack env")
    a = p.parse_args()
    stack = parse_env_text(a.stack_env.read_text())
    api = (a.api or f"http://localhost:{stack.get('API_HOST_PORT') or '8000'}").rstrip("/")
    target = MattermostTarget.resolve(REPO, stack)
    admin = GatewayClient.login(api, stack["GATEWAY_ADMIN_EMAIL"], stack["GATEWAY_ADMIN_PASSWORD"])
    bridge_id, _ = ensure_bridge(admin, target)
    removed = 0
    listing = admin.list_rooms()
    homes = [r["id"] for r in listing if str(r.get("name") or "") == "acme-finance"]
    for room in listing:
        name = str(room.get("name") or room.get("slug") or "")
        on = str(room.get("bridge_id") or "")
        if name.startswith(("plan-", "close-")) and (not bridge_id or on == bridge_id):
            rid = room["id"]
            # undress first: the Switch components around the room keep it from being deleted
            _quiet(admin, "PUT", f"/gateway/rooms/{rid}/group", json={"group_id": None})
            docs = _quiet(admin, "GET", f"/gateway/rooms/{rid}/documents") or []
            for doc in docs if isinstance(docs, list) else []:
                _quiet(admin, "DELETE", f"/gateway/rooms/{rid}/documents/{doc['id']}")
                _quiet(admin, "DELETE", f"/gateway/documents/{doc['id']}")
            for home in homes:
                _quiet(admin, "DELETE", f"/gateway/rooms/{home}/linked-rooms/{rid}")
            try:
                admin._req("DELETE", f"/gateway/rooms/{rid}")
                print(f"gateway room {name}: removed")
            except GatewayError as exc:
                print(f"gateway room {name}: not removed ({str(exc)[:80]})")
            removed += 1
    mm = MattermostClient.login(target.url, target.admin_user, target.admin_password)
    team = mm.team_by_name(target.team_name)["id"]
    channels = (
        mm._req("GET", f"/teams/{team}/channels?per_page=200")
        + mm._req("GET", f"/teams/{team}/channels/private?per_page=200")
        + mm._req("GET", f"/teams/{team}/channels/deleted?per_page=200")
    )
    for ch in channels:
        if str(ch.get("name", "")).startswith(("plan-", "close-")):
            r = mm._http.delete(f"/channels/{ch['id']}?permanent=true")
            if r.status_code >= 400:
                mm._http.delete(f"/channels/{ch['id']}")
                r = mm._http.delete(f"/channels/{ch['id']}?permanent=true")
            print(f"channel {ch['name']}: {'removed' if r.status_code < 400 else 'archived'}")
            removed += 1
    admin.close()
    mm.close()
    print("no plan rooms to remove" if not removed else f"{removed} removed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
