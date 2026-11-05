"""`sindarin bootstrap` (DDD §17): six agents, four bridged rooms, the demo people, every
participant id, the policy and executor files, an empty board. Idempotent and headless.

Runs as the Gateway admin and the Mattermost admin whose credentials sit in the Docker stack's
`.env`. Writes agent keys to each process's env file, ids into `agents/policy/*.yaml` and
`executor/*.yaml`. Prints what it did; never prints a key or a password.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from sindarin.executor.config import (
    load_config,
    load_env,
    parse_env_text,
    save_config,
    write_env_values,
)
from sindarin.names import (
    CODER,
    DISPLAY,
    GITHUB_EXECUTOR,
    PRODUCT,
    RELEASE,
    REVIEWER,
    SWITCH_EXECUTOR,
)
from sindarin.switch.client import ConnectionState, SwitchClient, SwitchError
from sindarin.switch.events import MessageEvent
from sindarin.switch.gateway import GatewayClient, GatewayError
from sindarin.switch.mattermost import MattermostClient, MattermostError

PEOPLE = (("isaac", "Isaac"), ("leonardo", "Leonardo"), ("john", "John"), ("vincent", "Vincent"))
DISPLAY_OF = dict(PEOPLE)
GROUPS = {"pm": ("leonardo",), "engineering": ("isaac",), "release": ("john",)}
GITHUB_LOGINS = {"isaac": "isaac-gh"}  # demo value; edit users.yaml for a real login
DEMO_REPO = "kartikangiras/sindarin-demo-app"
AGENTS: dict[str, tuple[str, str, str]] = {
    PRODUCT: (
        DISPLAY[PRODUCT],
        "Turns a customer ask into one spec a PM approves. Cannot approve.",
        "agents/product/.env",
    ),
    CODER: (
        DISPLAY[CODER],
        "Implements an approved spec on a branch and proposes the merge. Cannot merge.",
        "agents/coder/.env",
    ),
    REVIEWER: (
        DISPLAY[REVIEWER],
        "Checks every acceptance criterion and reports. Its pass is not an approval.",
        "agents/reviewer/.env",
    ),
    RELEASE: (
        DISPLAY[RELEASE],
        "Drafts release notes and proposes the tag; tells the customer afterwards.",
        "agents/release/.env",
    ),
    SWITCH_EXECUTOR: (
        DISPLAY[SWITCH_EXECUTOR],
        "Deterministic. Verifies a PM's approval by participant id and opens the feature room.",
        "executor/switch.env",
    ),
    GITHUB_EXECUTOR: (
        DISPLAY[GITHUB_EXECUTOR],
        "Deterministic. Verifies approvals by participant id, merges and publishes. "
        "Holds the GitHub token.",
        "executor/github.env",
    ),
}


@dataclass(frozen=True)
class RoomSpec:
    key: str  # policy.yaml rooms key
    channel_type: str
    agents: tuple[str, ...]
    users: tuple[str, ...]
    description: str
    instructions: str


ROOMS: dict[str, RoomSpec] = {
    "acme-shared": RoomSpec(
        "customer",
        "channel_private",
        (PRODUCT, RELEASE),
        ("isaac", "leonardo"),
        "Shared channel with Acme. Asks start here; releases are announced here.",
        "Customer asks are threads. @product asks at most two questions and drafts the spec in "
        "#product. "
        "@release posts the release note here once sindarin-github has published.",
    ),
    "product": RoomSpec(
        "product",
        "channel_public",
        (PRODUCT, RELEASE, SWITCH_EXECUTOR),
        ("leonardo", "isaac", "john", "vincent"),
        "Specs are proposed and approved here. Approve with `@sindarin-switch approve SPEC-n`.",
        "@product proposes specs as `sindarin-proposal` fences. Only a PM's "
        "`@sindarin-switch approve SPEC-n` "
        "opens a feature room. deny / changes / status / whoami / help / board.",
    ),
    "releases": RoomSpec(
        "releases",
        "channel_public",
        (RELEASE, GITHUB_EXECUTOR),
        ("john", "leonardo", "isaac"),
        "Releases are proposed and approved here. Approve with `@sindarin-github approve REL-n`.",
        "@release drafts notes and proposes the tag. Only a release approver's "
        "`@sindarin-github approve REL-n` "
        "publishes. deny / changes / status / whoami / help.",
    ),
}
PROBE_ROOM = "sindarin-ids"  # private, all six agents and the four people; never in the demo


@dataclass
class Report:
    lines: list[str] = field(default_factory=list)
    ok: bool = True
    agents: dict[str, dict] = field(default_factory=dict)  # handle → {id, api_key}
    people: dict[str, dict] = field(default_factory=dict)  # username → mattermost user
    rooms: dict[str, str] = field(default_factory=dict)  # room name → switch room id
    channels: dict[str, str] = field(default_factory=dict)  # room name → mattermost channel id
    participant_ids: dict[str, str] = field(default_factory=dict)  # handle | username → id
    bridge_id: str = ""

    def say(self, line: str) -> None:
        self.lines.append(line)

    def fail(self, line: str) -> None:
        self.ok = False
        self.lines.append("FAIL " + line)


def _email(username: str) -> str:
    return f"{username}@sindarin.local"


MATTERMOST_ENV = Path("executor") / "mattermost.env"  # gitignored; present = a server of your own


@dataclass
class MattermostTarget:
    """Which Mattermost the demo runs in: the bundled one from the stack env, or a server of your
    own described in `executor/mattermost.env` (DDD §12.2 "Connect Mattermost")."""

    url: str  # as people and this machine reach it
    admin_user: str
    admin_password: str
    team_name: str
    internal_url: str = ""  # as the Switch container reaches it; defaults to url
    bridge_name: str = ""  # the Gateway connection's display name
    own: bool = False  # True when it came from mattermost.env

    @classmethod
    def from_stack(cls, stack: dict[str, str], url: str | None = None) -> MattermostTarget:
        return cls(
            url=(url or f"http://localhost:{stack.get('MATTERMOST_HOST_PORT') or '8065'}").rstrip(
                "/"
            ),
            admin_user=stack.get("MATTERMOST_ADMIN_USER", ""),
            admin_password=stack.get("MATTERMOST_ADMIN_PASSWORD", ""),
            team_name=stack.get("MATTERMOST_TEAM_NAME") or "switch",
        )

    @classmethod
    def from_env_text(cls, text: str) -> MattermostTarget:
        v = parse_env_text(text)
        url = v.get("MATTERMOST_URL", "").rstrip("/")
        missing = [
            k
            for k in ("MATTERMOST_URL", "MATTERMOST_ADMIN_USER", "MATTERMOST_ADMIN_PASSWORD")
            if not v.get(k)
        ]
        if missing:
            raise ValueError(f"mattermost.env lacks {', '.join(missing)}")
        host = url.split("://", 1)[-1].split("/", 1)[0]
        return cls(
            url=url,
            admin_user=v["MATTERMOST_ADMIN_USER"],
            admin_password=v["MATTERMOST_ADMIN_PASSWORD"],
            team_name=v.get("MATTERMOST_TEAM_NAME") or "switch",
            internal_url=(v.get("MATTERMOST_INTERNAL_URL") or url).rstrip("/"),
            bridge_name=v.get("SWITCH_BRIDGE_NAME") or f"Mattermost {host}",
            own=True,
        )

    @classmethod
    def resolve(
        cls,
        repo: Path,
        stack: dict[str, str],
        *,
        mattermost_env: Path | None = None,
        url: str | None = None,
    ) -> MattermostTarget:
        """An explicit file wins; else `executor/mattermost.env` if it exists; else the bundled
        Mattermost of the compose stack."""
        path = mattermost_env or (
            repo / MATTERMOST_ENV if (repo / MATTERMOST_ENV).exists() else None
        )
        if path is not None:
            return cls.from_env_text(path.read_text(encoding="utf-8"))
        return cls.from_stack(stack, url)

    def connection_config(self) -> dict[str, str]:
        cfg = {
            "url": self.internal_url or self.url,
            "admin_user": self.admin_user,
            "admin_password": self.admin_password,
            "team_name": self.team_name,
        }
        if self.internal_url and self.internal_url != self.url:
            cfg["public_url"] = self.url
        return cfg


def ensure_bridge(admin: GatewayClient, target: MattermostTarget) -> tuple[str, bool]:
    """(bridge id, created). A server of your own gets a Gateway connection named `bridge_name`,
    created once and made the default; the bundled server keeps the seeder's default bridge."""
    bridges = admin.collaborations()
    if not target.own:
        return admin.default_bridge_id() or "", False
    for b in bridges:
        if b.get("display_name") == target.bridge_name:
            bid = str(b.get("bridge_id") or b.get("id") or "")
            if bid and not (b.get("is_default") or b.get("default")):
                admin.set_default_bridge(bid)
            return bid, False
    made = admin.create_bridge(
        bridge_type="mattermost",
        display_name=target.bridge_name,
        connection_config=target.connection_config(),
    )
    bid = str(made.get("bridge_id") or made.get("id") or "")
    if bid:
        admin.set_default_bridge(bid)
    return bid, True


def observe_as_agent(
    api: str,
    agent_id: str,
    api_key: str,
    actions: list[Callable[[], object]],
    *,
    settle_seconds: float = 6.0,
) -> list[MessageEvent]:
    """Open the agent's event connection, run `actions` once connected, return the message events
    seen. This is how the executors learn participant ids: from frames, never from the Gateway."""
    seen: list[MessageEvent] = []

    async def main() -> None:
        client = SwitchClient(api, agent_id, api_key, client_name="sindarin-bootstrap")
        state = ConnectionState(connection_id=str(uuid.uuid4()))
        stop = asyncio.Event()

        async def on_frame(frame: object) -> None:
            if isinstance(frame, MessageEvent):
                seen.append(frame)

        task = asyncio.create_task(client.run_connection(state, on_frame, stop=stop))
        for _ in range(100):
            if state.connected:
                break
            await asyncio.sleep(0.1)
        for act in actions:
            try:
                await asyncio.to_thread(act)
            except Exception as exc:  # one failed marker must not stop the others
                seen.append(
                    MessageEvent(
                        None, "", False, "", "", "", f"[marker failed: {exc.__class__.__name__}]", 0
                    )
                )
        await asyncio.sleep(settle_seconds)
        stop.set()
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        client.close()

    asyncio.run(main())
    return seen


# -- the policy files are rendered whole so comments survive and ids are never regex-patched ------


def render_policy(ids: dict[str, str], rooms: dict[str, str], repo: str) -> str:
    def pid(key: str) -> str:
        return ids.get(key, f"@switch-member-REPLACE-ME-{key}:localhost")

    groups = "\n".join(
        f"  {g}: [{', '.join(chr(34) + pid(u) + chr(34) for u in members)}]"
        + f"            # {', '.join(DISPLAY_OF[u] for u in members)}"
        for g, members in GROUPS.items()
    )
    proposers = "\n".join(f'  {r}: "{pid(r)}"' for r in (PRODUCT, CODER, REVIEWER, RELEASE))
    executors = "\n".join(f'  {e}: "{pid(e)}"' for e in (SWITCH_EXECUTOR, GITHUB_EXECUTOR))
    room_lines = "\n".join(
        f'  {k}: "{rooms.get(k, "")}"' for k in ("product", "releases", "customer")
    )
    return f"""# Sindarin policy (DDD §10.1). Agents draft from it; both executors enforce it.
# Every id is a Switch participant id; nothing in this file is a display name.
# `sindarin bootstrap` writes the ids and room ids. Learn yours with "@sindarin-switch whoami".
version: 2

groups:
{groups}
  # Vincent is in no group: used for refusals in the demo (§3.2)

proposers:
{proposers}

executors:
{executors}

rooms:
{room_lines}

rules:
  - name: spec
    kind: spec
    proposer: product
    approvers: pm
    room: product
    allowed_repos: ["{repo}"]
    engineers: engineering
    pending_ttl: 24h
  - name: merge
    kind: pr
    proposer: coder
    approvers: engineering
    room: feature            # the artifact's own feature room
    repo: "{repo}"
    base: main
    merge_method: squash
    pending_ttl: 24h
  - name: release
    kind: release
    proposer: release
    approvers: release
    room: releases
    repo: "{repo}"
    tag_pattern: "^v\\\\d+\\\\.\\\\d+\\\\.\\\\d+$"
    pending_ttl: 24h
"""


def render_users(ids: dict[str, str], logins: dict[str, str] | None = None) -> str:
    logins = logins if logins is not None else GITHUB_LOGINS
    rows = []
    for username, display in PEOPLE:
        pid = ids.get(username, f"@switch-member-REPLACE-ME-{username}:localhost")
        extra = f", github: {logins[username]}" if username in logins else ""
        rows.append(f'  "{pid}": {{ name: {display}{extra} }}')
    body = "\n".join(rows)
    return f"""# Participant id → display name and, for engineers, GitHub login (DDD §10.2).
# Names are for reading; the login names the PR assignee and recognises a human PR author.
# Never used for authorization. Written by `sindarin bootstrap`.
version: 2
users:
{body}
"""


def current_repo(repo: Path) -> str:
    try:
        import yaml

        data = yaml.safe_load((repo / "agents" / "policy" / "policy.yaml").read_text()) or {}
        for rule in data.get("rules", []):
            if rule.get("repo"):
                return str(rule["repo"])
    except Exception:
        pass
    return DEMO_REPO


def _channel_id(
    admin: GatewayClient, mm: MattermostClient, team_id: str, room_id: str, name: str
) -> str:
    """The Mattermost channel a bridged room mirrors to: `external_channel_id` on the Gateway's
    room detail (verified 2026-11-05), else a lookup by name. Waits briefly for the bridge."""
    for _ in range(10):
        if room_id:
            with contextlib.suppress(GatewayError):
                cid = admin.get_room(room_id).get("external_channel_id")
                if cid:
                    return str(cid)
        with contextlib.suppress(MattermostError):
            ch = mm.channel_by_name(team_id, name)
            if ch:
                return str(ch["id"])
        time.sleep(1)
    return ""


# -- the bootstrap ---------------------------------------------------------------------------------


def run_bootstrap(
    repo: Path,
    stack_env: Path,
    *,
    api_url: str | None = None,
    mattermost_url: str | None = None,
    demo_pass: str = "Sindarin-demo-1",
    write_ids: bool = True,
    github_mode: str | None = None,
    addressing: str = "rooms",
    mattermost_env: Path | None = None,
) -> Report:
    rep = Report()
    stack = parse_env_text(stack_env.read_text(encoding="utf-8"))
    api = (api_url or f"http://localhost:{stack.get('API_HOST_PORT') or '8000'}").rstrip("/")
    try:
        target = MattermostTarget.resolve(
            repo, stack, mattermost_env=mattermost_env, url=mattermost_url
        )
    except ValueError as exc:
        rep.fail(str(exc))
        return rep
    mm_url = target.url
    team_name = target.team_name
    rep.say(
        f"mattermost: {mm_url} team {team_name}"
        + (
            f" (your server; Gateway connection '{target.bridge_name}')"
            if target.own
            else " (bundled)"
        )
    )
    admin_email = stack.get("GATEWAY_ADMIN_EMAIL", "")
    admin_password = stack.get("GATEWAY_ADMIN_PASSWORD", "")
    if not (admin_email and admin_password):
        rep.fail(f"{stack_env} lacks GATEWAY_ADMIN_EMAIL / GATEWAY_ADMIN_PASSWORD")
        return rep

    # 1. health, admin session
    try:
        r = httpx.get(f"{api}/health", timeout=10)
        rep.say(f"health {api}/health → {r.status_code}")
    except httpx.HTTPError as exc:
        rep.fail(f"bridge unreachable at {api}: {exc.__class__.__name__}")
        return rep
    try:
        admin = GatewayClient.login(api, admin_email, admin_password)
    except GatewayError as exc:
        rep.fail(f"admin login: {exc}")
        return rep
    who = admin.user or {}
    rep.say(
        f"gateway admin signed in as {who.get('name', admin_email)} (role {who.get('role', '?')})"
    )

    # 2. the six agents (§17 step 1)
    server_agents = {a.get("name"): a for a in admin.list_agents()}
    for handle, (display, description, env_rel) in AGENTS.items():
        env_path = repo / env_rel
        values = load_env(env_path, env_override=False)
        on_server = server_agents.get(handle)
        have_key = bool(values.get("SWITCH_AGENT_ID") and values.get("SWITCH_AGENT_API_KEY"))
        if on_server and have_key and values["SWITCH_AGENT_ID"] == on_server.get("id"):
            rep.agents[handle] = {"id": on_server["id"], "api_key": values["SWITCH_AGENT_API_KEY"]}
            rep.say(f"agent {handle}: exists id={on_server['id']} (key in {env_rel} reused)")
            continue
        if on_server:
            try:
                admin.delete_agent_by_name(handle)
                rep.say(f"agent {handle}: stale registration without a local key; re-registering")
            except GatewayError as exc:
                rep.fail(f"agent {handle}: cannot delete stale registration: {exc}")
                continue
        try:
            key = admin.create_registration_key(f"sindarin bootstrap {handle}")
            reg = SwitchClient(api).register(
                key, name=handle, display_name=display, description=description
            )
        except (GatewayError, SwitchError) as exc:
            rep.fail(f"agent {handle}: registration failed: {exc}")
            continue
        env_path.parent.mkdir(parents=True, exist_ok=True)
        write_env_values(
            env_path,
            {
                "SWITCH_BRIDGE_URL": api,
                "SWITCH_AGENT_ID": reg["id"],
                "SWITCH_AGENT_API_KEY": reg["api_key"],
                "SWITCH_CONNECTION_ID": str(uuid.uuid4()),
            },
        )
        rep.agents[handle] = {"id": reg["id"], "api_key": reg["api_key"]}
        rep.say(f"agent {handle}: registered id={reg['id']}; key written to {env_rel} (mode 600)")
    if len(rep.agents) != len(AGENTS):
        return rep
    for handle, a in rep.agents.items():  # open until the rooms exist; scoped below
        try:
            admin.set_addressing_open(a["id"])
        except GatewayError as exc:
            rep.fail(f"addressing {handle}: {exc}")
    # sindarin-switch needs a Gateway credential to open rooms (§4.2)
    write_env_values(
        repo / "executor" / "switch.env",
        {
            "SWITCH_GATEWAY_URL": api,
            "SWITCH_GATEWAY_EMAIL": admin_email,
            "SWITCH_GATEWAY_PASSWORD": admin_password,
        },
    )
    rep.say("executor/switch.env: Gateway credential written (mode 600)")

    # 3. the demo people in Mattermost (§17 step 3)
    mm: MattermostClient | None = None
    team_id = ""
    try:
        mm = MattermostClient.login(mm_url, target.admin_user, target.admin_password)
        team_id = mm.team_by_name(team_name)["id"]
        for username, display in PEOPLE:
            user, created = mm.ensure_user(username, _email(username), demo_pass, display)
            rep.people[username] = user
            with contextlib.suppress(MattermostError):
                mm.add_to_team(team_id, user["id"])
            rep.say(f"mattermost user {username}: {'created' if created else 'exists'}")
    except (MattermostError, httpx.HTTPError, KeyError) as exc:
        rep.fail(
            f"mattermost at {mm_url}: {str(exc)[:160]} "
            + ("(check executor/mattermost.env)" if target.own else "(is the collab profile up?)")
        )

    # 4. the bridged rooms (§17 step 2, verify item 1)
    try:
        rep.bridge_id, made = ensure_bridge(admin, target)
        rep.say(f"bridge: {rep.bridge_id or 'none found'}{' (connected now)' if made else ''}")
        if rep.bridge_id:
            # Switch posts a canned "Hey! Ping @agent" when an agent joins a channel; the agents
            # speak for themselves (§13), so the greeting is turned off (verified 2026-11-05)
            with contextlib.suppress(GatewayError):
                admin._req(
                    "PATCH",
                    f"/gateway/collaborations/{rep.bridge_id}",
                    json={"agent_greetings_enabled": False},
                )
        existing = {(r.get("name") or r.get("slug")): r for r in admin.list_rooms()}
        for name, spec in ROOMS.items():
            room = existing.get(name)
            if room is None:
                room = admin.create_room(
                    name=name,
                    description=spec.description,
                    instructions=spec.instructions,
                    bridge_id=rep.bridge_id,
                    channel_type=spec.channel_type,
                    agent_ids=[rep.agents[h]["id"] for h in spec.agents],
                    user_names=list(spec.users),
                )
                rep.say(f"room {name}: created id={room.get('id')}")
            else:
                rep.say(f"room {name}: exists id={room.get('id')}")
                members = admin.room_members(room["id"])
                want_agents = [
                    rep.agents[h]["id"]
                    for h in spec.agents
                    if rep.agents[h]["id"] not in set(members["agents"])
                ]
                want_users = [u for u in spec.users if u not in set(members["users"])]
                if want_agents:
                    admin.add_room_agents(room["id"], want_agents)
                if want_users:
                    with contextlib.suppress(GatewayError):
                        admin.add_room_users(room["id"], want_users)
            rep.rooms[name] = str(room.get("id"))
    except GatewayError as exc:
        rep.fail(
            f"bridged rooms: {exc}. Fallback (§20): create the channels in Mattermost and run "
            f"`!invite-agent @<agent>` in each, then re-run bootstrap."
        )
    if mm is not None and team_id:
        for name in ROOMS:
            cid = _channel_id(admin, mm, team_id, rep.rooms.get(name, ""), name)
            if cid:
                rep.channels[name] = cid
                for username in ROOMS[name].users:
                    if username in rep.people:
                        with contextlib.suppress(MattermostError):
                            mm.add_to_channel(cid, rep.people[username]["id"])
        rep.say(
            "mattermost channels found: "
            f"{', '.join(sorted(rep.channels)) or 'none yet (bridge still syncing?)'}"
        )
    if len(rep.rooms) != len(ROOMS):
        _finish_files(repo, rep, github_mode, write_ids=False)
        admin.close()
        return rep

    # 5. participant ids (§17 step 4, verify item 2): a private probe room holds all six agents
    # and the four people. We watch sindarin-switch's stream while the other agents post markers
    # and the people post `whoami` from Mattermost; then product's stream while sindarin-switch
    # posts its own marker (an agent does not see its own posts).
    probe_room = ""
    channel: str | None = None
    try:
        existing = {(r.get("name") or r.get("slug")): r for r in admin.list_rooms()}
        probe = existing.get(PROBE_ROOM)
        if probe is None:
            probe = admin.create_room(
                name=PROBE_ROOM,
                description="Where participant ids are learned at bootstrap. Not part of the demo.",
                instructions="Bootstrap only. Agents post one marker each; people answer whoami.",
                bridge_id=rep.bridge_id,
                channel_type="channel_private",
                agent_ids=[a["id"] for a in rep.agents.values()],
                user_names=[u for u, _ in PEOPLE],
            )
            rep.say(f"probe room {PROBE_ROOM}: created id={probe.get('id')}")
        else:
            members = admin.room_members(probe["id"])
            want = [a["id"] for a in rep.agents.values() if a["id"] not in set(members["agents"])]
            if want:
                admin.add_room_agents(probe["id"], want)
            rep.say(f"probe room {PROBE_ROOM}: exists id={probe.get('id')}")
        probe_room = str(probe.get("id"))
        _set_addressing(admin, rep, addressing)  # the demo rooms and the probe room exist now
    except GatewayError as exc:
        rep.fail(f"probe room: {exc}")
        probe_room = rep.rooms["product"]
    if mm is not None and team_id and probe_room:
        channel = _channel_id(admin, mm, team_id, probe_room, PROBE_ROOM) or None
        if channel:
            for person in rep.people.values():
                with contextlib.suppress(MattermostError):
                    mm.add_to_channel(channel, person["id"])
    sw = rep.agents[SWITCH_EXECUTOR]
    markers: dict[str, str] = {}
    actions: list[Callable[[], object]] = []
    for handle, a in rep.agents.items():
        if handle == SWITCH_EXECUTOR:
            continue
        marker = f"{handle} is online. [{uuid.uuid4().hex[:6]}]"
        markers[marker] = handle
        actions.append(
            lambda a=a, m=marker: SwitchClient(api, a["id"], a["api_key"]).post(probe_room, m)
        )
    if channel and rep.people:
        for username, _display in PEOPLE:
            marker = f"@{SWITCH_EXECUTOR} whoami [{uuid.uuid4().hex[:6]}]"
            markers[marker] = username

            def _say(username=username, m=marker):
                u = MattermostClient.login(mm_url, _email(username), demo_pass)
                try:
                    return u.create_post(channel, m)
                finally:
                    u.close()

            actions.append(_say)
    else:
        rep.say(
            "people's ids: no Mattermost channel to post from; ask each person to type "
            f"`@{SWITCH_EXECUTOR} whoami` in #product and paste the ids into policy.yaml"
        )
    try:
        events = observe_as_agent(api, sw["id"], sw["api_key"], actions)
        prod = rep.agents[PRODUCT]
        own = f"{SWITCH_EXECUTOR} is online. [{uuid.uuid4().hex[:6]}]"
        markers[own] = SWITCH_EXECUTOR
        events += observe_as_agent(
            api,
            prod["id"],
            prod["api_key"],
            [lambda: SwitchClient(api, sw["id"], sw["api_key"]).post(probe_room, own)],
            settle_seconds=3.0,
        )
    except Exception as exc:
        events = []
        rep.fail(f"event probe failed: {exc.__class__.__name__}: {str(exc)[:120]}")
    addressed: dict[str, bool] = {}
    for ev in events:
        for marker, who_ in markers.items():
            if marker in (ev.body or "") and ev.sender:
                rep.participant_ids[who_] = ev.sender
                addressed[who_] = ev.addressed
    for who_, pid in rep.participant_ids.items():
        rep.say(f"participant id {who_}: {pid}")
    people_seen = [u for u, _ in PEOPLE if u in addressed]
    if people_seen:
        rep.say(
            "addressed (verify item 2): "
            + ", ".join(f"{u}={'yes' if addressed[u] else 'no'}" for u in people_seen)
        )
    expected = [*AGENTS, *(u for u, _ in PEOPLE)]
    missing = [n for n in expected if n not in rep.participant_ids]
    if missing:
        rep.fail(f"no participant id for: {', '.join(missing)} ({len(events)} frames seen)")

    # 6. files (§17 step 5)
    _finish_files(repo, rep, github_mode, write_ids=write_ids and not missing)

    # 7. an empty board, pinned (§17 step 6, §14)
    product_room = rep.rooms["product"]
    product_channel = rep.channels.get("product")
    try:
        SwitchClient(api, sw["id"], sw["api_key"]).post(
            product_room,
            "**Board** · bootstrap\n**In flight**\nnothing in flight\n"
            "**Waiting on**\nno pending approvals",
        )
        pinned = False
        if mm is not None and product_channel:
            for _ in range(10):
                posts = mm.posts(product_channel, per_page=20)
                board = next(
                    (q for q in posts if str(q.get("message", "")).startswith("**Board**")), None
                )
                if board is not None:
                    if not board.get("is_pinned"):
                        mm.pin(board["id"])
                    pinned = True
                    break
                time.sleep(1)
        rep.say(
            "board: posted and pinned in #product"
            if pinned
            else "board: posted in #product; pin it in Mattermost"
        )
    except (SwitchError, MattermostError) as exc:
        rep.say(f"board: not posted ({str(exc)[:80]})")
    if mm is not None:
        mm.close()
    admin.close()
    return rep


def _set_addressing(admin: GatewayClient, rep: Report, mode: str) -> None:
    """Layer 0 (§5): each agent may be addressed only in the rooms it belongs to (`rooms`), or by
    anyone (`open`). Feature rooms are added by sindarin-switch when it opens them. The probe
    room is included so ids can be re-learned. Verified live 2026-11-05 (§12.4 item 5): a
    mention from elsewhere arrives `addressed: false` and Switch refuses it in the room."""
    if mode != "rooms":
        rep.say("addressing: anyone, for all six (the executors refuse in code)")
        return
    listing = admin.list_rooms()
    probe = [str(r.get("id")) for r in listing if r.get("name") == PROBE_ROOM and r.get("id")]
    feature_rooms = [
        str(r.get("id"))
        for r in listing
        if str(r.get("name", "")).startswith("feat-") and not r.get("archived") and r.get("id")
    ]
    for handle, a in rep.agents.items():
        rooms = [rep.rooms[name] for name, spec in ROOMS.items() if handle in spec.agents]
        if handle in (CODER, REVIEWER, GITHUB_EXECUTOR, SWITCH_EXECUTOR):
            rooms += feature_rooms
        rooms += probe
        try:
            admin.set_addressing_rooms(a["id"], rooms)
        except GatewayError as exc:
            rep.fail(f"addressing {handle}: {exc}")
    rep.say(
        "addressing: room-scoped for all six (Switch refuses a mention from elsewhere on the "
        "agent's behalf; the executors still refuse in code inside their rooms)"
    )


def _finish_files(repo: Path, rep: Report, github_mode: str | None, *, write_ids: bool) -> None:
    ids = dict(rep.participant_ids)
    rooms = {ROOMS[name].key: rid for name, rid in rep.rooms.items()}
    if write_ids:
        (repo / "agents" / "policy" / "policy.yaml").write_text(
            render_policy(ids, rooms, current_repo(repo))
        )
        (repo / "agents" / "policy" / "users.yaml").write_text(render_users(ids))
        rep.say("wrote agents/policy/policy.yaml and users.yaml (ids, rooms)")
    for short, handle in (("switch", SWITCH_EXECUTOR), ("github", GITHUB_EXECUTOR)):
        path = repo / "executor" / f"{short}.yaml"
        cfg = load_config(path)
        cfg.executor = handle
        if handle in ids:
            cfg.self_id = ids[handle]
        other = GITHUB_EXECUTOR if handle == SWITCH_EXECUTOR else SWITCH_EXECUTOR
        if other in ids:
            cfg.peer_ids = [ids[other]]
        if handle == SWITCH_EXECUTOR:
            cfg.bridge_id = rep.bridge_id
            cfg.peer_state_db = cfg.peer_state_db or "github.db"
            directory = {
                ids[h]: rep.agents[h]["id"] for h in AGENTS if h in ids and h in rep.agents
            }
            directory.update({ids[u]: u for u, _ in PEOPLE if u in ids})
            cfg.directory = directory
        if handle == GITHUB_EXECUTOR and github_mode in ("real", "simulated"):
            cfg.github_mode = github_mode
        save_config(path, cfg)
        rep.say(f"wrote executor/{short}.yaml")
