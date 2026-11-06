#!/usr/bin/env python3
"""The §12.4 verification list against a live stack (DDD §12.4, §20). Optionally runs the
bootstrap, then checks, through Mattermost and the Gateway:

  1. bridged rooms exist and both executors receive events from them
  2. participant ids of Mattermost users as the executors see them, identical for both executors
     (stable by construction: `@switch-mattermost-<bridge id>-<username>:localhost`)
  3. an executor's threaded reply renders as a Mattermost thread (root_id set)
  4. room creation through the Gateway route: the bootstrap's bridged probe room has both
     executors as members and a Mattermost channel; feature rooms opened so far are listed
  5. room-scoped addressing: sindarin-ledger scoped to #planning, a mention from a period room
     still arrives (addressed=False) and Switch refuses it in the room; the policy is restored

Items 1–3 need the two executors running (`scripts/dev_stack.sh start`): Isaac types
`@sindarin-switch whoami` in #acme-finance and `@sindarin-ledger whoami` in #planning, and the replies
are read back from Mattermost. Writes docs/SPIKE-RUN.md. Never prints a key or a password.

    uv run python scripts/spike.py [--stack-env ~/.sindarin/switch-stack/.env] [--no-bootstrap]
"""

from __future__ import annotations

import argparse
import contextlib
import re
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from sindarin.bootstrap import MattermostTarget, run_bootstrap  # noqa: E402
from sindarin.executor.config import parse_env_text, process_env  # noqa: E402
from sindarin.names import LEDGER_EXECUTOR, SWITCH_EXECUTOR  # noqa: E402
from sindarin.switch.gateway import GatewayClient, GatewayError  # noqa: E402
from sindarin.switch.mattermost import MattermostClient, MattermostError  # noqa: E402

ID_RE = re.compile(r"participant id `([^`]+)`")


def executors_running() -> bool:
    return all(
        subprocess.run(
            ["pgrep", "-f", f"sindarin-exec run --executor {s}"], capture_output=True
        ).returncode
        == 0
        for s in ("switch", "ledger")
    )


def whoami_round_trip(
    mm_admin: MattermostClient,
    isaac: MattermostClient,
    channel: dict,
    handle: str,
    wait: float = 20.0,
):
    """Isaac asks an executor who he is; returns (reply post or None, isaac's post)."""
    post = isaac.create_post(channel["id"], f"@{handle} whoami")
    deadline = time.time() + wait
    while time.time() < deadline:
        for q in mm_admin.posts(channel["id"], 30):
            if q.get("root_id") == post["id"] and "participant id" in q.get("message", ""):
                return q, post
        time.sleep(1)
    return None, post


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--stack-env", type=Path, default=Path.home() / ".sindarin" / "switch-stack" / ".env"
    )
    p.add_argument("--api", default=None)
    p.add_argument("--demo-pass", default="Sindarin-demo-1")
    p.add_argument("--no-bootstrap", action="store_true")
    p.add_argument("--report", type=Path, default=REPO / "docs" / "SPIKE-RUN.md")
    a = p.parse_args()
    lines: list[str] = []
    ok = True
    stack = parse_env_text(a.stack_env.read_text())
    api = (a.api or f"http://localhost:{stack.get('API_HOST_PORT') or '8000'}").rstrip("/")
    target = MattermostTarget.resolve(REPO, stack)
    mm_url = target.url
    lines.append(f"mattermost: {mm_url} team {target.team_name}")
    if not a.no_bootstrap:
        rep = run_bootstrap(
            REPO,
            a.stack_env,
            api_url=a.api,
            demo_pass=a.demo_pass,
            people="both",  # the spike posts as isaac and vincent
        )
        lines += [line if line.startswith("FAIL") else "PASS " + line for line in rep.lines]
        ok &= rep.ok
    sw = process_env(REPO, SWITCH_EXECUTOR)
    gh = process_env(REPO, LEDGER_EXECUTOR)
    import yaml

    policy = yaml.safe_load((REPO / "agents" / "policy" / "policy.yaml").read_text())
    rooms = policy.get("rooms", {})
    # 1–3 through the executors' own whoami replies
    if not executors_running():
        lines.append(
            "FAIL items 1-3: the executors are not running; start them with scripts/dev_stack.sh start"
        )
        ok = False
    else:
        try:
            mm_admin = MattermostClient.login(mm_url, target.admin_user, target.admin_password)
            team = mm_admin.team_by_name(target.team_name)["id"]
            isaac = MattermostClient.login(mm_url, "isaac@sindarin.local", a.demo_pass)
            ids: dict[str, str] = {}
            for handle, room_name in (
                (SWITCH_EXECUTOR, "acme-finance"),
                (LEDGER_EXECUTOR, "planning"),
            ):
                ch = mm_admin.channel_by_name(team, room_name)
                if ch is None:
                    lines.append(f"FAIL item 1: Mattermost channel {room_name} not found")
                    ok = False
                    continue
                reply, post = whoami_round_trip(mm_admin, isaac, ch, handle)
                if reply is None:
                    lines.append(
                        f"FAIL item 1: {handle} did not answer whoami in #{room_name} (room {rooms.get(room_name)})"
                    )
                    ok = False
                    continue
                m = ID_RE.search(reply["message"])
                ids[handle] = m.group(1) if m else "?"
                lines.append(
                    f"PASS item 1: {handle} answered Isaac in #{room_name} (policy room {rooms.get(room_name)})"
                )
                threaded = reply.get("root_id") == post["id"]
                lines.append(
                    f"{'PASS' if threaded else 'FAIL'} item 3: {handle}'s reply is threaded under Isaac's post (root_id match: {threaded})"
                )
                ok &= threaded
            same = len(set(ids.values())) == 1 and "?" not in ids.values()
            lines.append(
                f"{'PASS' if same else 'FAIL'} item 2: Isaac's participant id as seen by both executors: {sorted(set(ids.values()))}"
            )
            ok &= same
            isaac.close()
            mm_admin.close()
        except (MattermostError, KeyError) as exc:
            lines.append(f"FAIL items 1-3: Mattermost: {str(exc)[:160]}")
            ok = False
    # 4: room creation through the Gateway route; the bootstrap's probe room was created that way
    try:
        admin = GatewayClient.login(
            api, stack.get("GATEWAY_ADMIN_EMAIL", ""), stack.get("GATEWAY_ADMIN_PASSWORD", "")
        )
        bridge = admin.default_bridge_id() or ""
        probe = next((r for r in admin.list_rooms() if r.get("name") == "sindarin-ids"), None)
        if probe is None:
            lines.append(
                "FAIL item 4: the bootstrap's probe room sindarin-ids is missing; run `sindarin bootstrap`"
            )
            ok = False
        else:
            detail = admin.get_room(probe["id"])
            members = admin.room_members(probe["id"])
            both = {sw["SWITCH_AGENT_ID"], gh["SWITCH_AGENT_ID"]} <= set(members["agents"])
            bridged = bool(detail.get("external_channel_id")) and detail.get("bridge_id") == bridge
            lines.append(
                f"{'PASS' if both and bridged else 'FAIL'} item 4: POST /gateway/rooms created sindarin-ids on bridge "
                f"{bridge or 'none'}; Mattermost channel {detail.get('external_channel_id') or '?'}; "
                f"both executors read back: {both}; people: {members['users']}"
            )
            ok &= both and bridged
        feats = [
            r["name"]
            for r in admin.list_rooms()
            if str(r.get("name", "")).startswith("plan-") and not r.get("archived")
        ]
        lines.append(
            f"INFO item 4: feature rooms opened by {SWITCH_EXECUTOR} so far: {feats or 'none'}"
        )
        admin.close()
    except GatewayError as exc:
        lines.append(
            f"FAIL item 4: {exc} → fallback §20: bootstrap pre-creates rooms; sindarin-switch adds members"
        )
        ok = False
    # 5: room-scoped addressing. Restrict sindarin-ledger to #planning, have Vincent mention it in
    # a feature room it belongs to, read the executor's frame and Switch's reply, restore.
    if not executors_running():
        lines.append("SKIP item 5: needs the executors running")
    else:
        try:
            admin = GatewayClient.login(
                api, stack.get("GATEWAY_ADMIN_EMAIL", ""), stack.get("GATEWAY_ADMIN_PASSWORD", "")
            )
            mm_admin = MattermostClient.login(mm_url, target.admin_user, target.admin_password)
            team = mm_admin.team_by_name(target.team_name)["id"]
            feat = next(
                (
                    r
                    for r in admin.list_rooms()
                    if str(r.get("name", "")).startswith("plan-") and not r.get("archived")
                ),
                None,
            )
            channel = mm_admin.channel_by_name(team, feat["name"]) if feat else None
            if feat is None or channel is None:
                lines.append(
                    "SKIP item 5: no feature room yet; run the demo once, then re-run the spike"
                )
            else:
                users = yaml.safe_load((REPO / "agents" / "policy" / "users.yaml").read_text())[
                    "users"
                ]
                vincent_id = next(pid for pid in users if pid.endswith("-vincent:localhost"))
                vincent = MattermostClient.login(mm_url, "vincent@sindarin.local", a.demo_pass)
                with contextlib.suppress(MattermostError):
                    mm_admin.add_to_channel(channel["id"], vincent.user["id"])
                before = admin.addressing_rooms(gh["SWITCH_AGENT_ID"])
                log_path = REPO / "executor" / "sindarin-ledger.log"
                try:
                    admin.set_addressing_rooms(gh["SWITCH_AGENT_ID"], [rooms["planning"]])
                    mark = log_path.read_text() if log_path.exists() else ""
                    post = vincent.create_post(channel["id"], f"@{LEDGER_EXECUTOR} whoami")
                    time.sleep(8)
                    tail = (log_path.read_text() if log_path.exists() else "")[len(mark) :]
                    frame = next(
                        (ln for ln in tail.splitlines() if "message " in ln and vincent_id in ln),
                        "",
                    )
                    delivered = bool(frame)
                    unaddressed = "addressed=False" in frame
                    refusal = any(
                        q.get("root_id") == post["id"] and "not permitted" in q.get("message", "")
                        for q in mm_admin.posts(channel["id"], 20)
                    )
                    passed = delivered and unaddressed and refusal
                    lines.append(
                        f"{'PASS' if passed else 'FAIL'} item 5: with {LEDGER_EXECUTOR} scoped to #planning, Vincent's mention in "
                        f"#{feat['name']} reached the executor: {delivered}, addressed=False: {unaddressed}, Switch refused in the room: {refusal} "
                        "→ decision: room-scoped policies (bootstrap --addressing rooms), typed refusals kept inside the rooms"
                    )
                    ok &= passed
                finally:
                    if before is None:
                        admin.set_addressing_open(gh["SWITCH_AGENT_ID"])
                    else:
                        admin.set_addressing_rooms(gh["SWITCH_AGENT_ID"], before)
                vincent.close()
            mm_admin.close()
            admin.close()
        except (GatewayError, MattermostError, KeyError, StopIteration) as exc:
            lines.append(f"FAIL item 5: {str(exc)[:160]}")
            ok = False
    stamp = f"{datetime.now(UTC):%Y-%m-%d %H:%M:%S} UTC"
    a.report.write_text(
        f"# Verification run (DDD §12.4)\n\n_Run by `scripts/spike.py` on {stamp} against {api} and {mm_url} "
        f"(switch-core 0.31, Mattermost 11.9, profiles gateway collab)._\n\n"
        + "\n".join(f"- {line}" for line in lines)
        + f"\n\nGATE: {'PASS' if ok else 'FAIL'}\n"
    )
    for line in lines:
        print(line)
    print("GATE:", "PASS" if ok else "FAIL", f"(report: {a.report.relative_to(REPO)})")
    assert uuid  # kept for ad-hoc markers
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
