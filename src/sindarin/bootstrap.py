"""`sindarin bootstrap` (design v5 §7): five agents, two bridged rooms, the demo people, every
participant id, the policy and executor files, an empty board. Idempotent and headless.

Runs as the Gateway admin and the Mattermost admin whose credentials sit in the Docker stack's
`.env`, or in a Mattermost server of your own named in the repository's root `.env`. Writes each
process's Switch identity into that root `.env` under the process's prefix, ids into
`agents/policy/*.yaml` and `executor/*.yaml`. Prints what it did; never prints a key or a password.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import httpx

from sindarin.executor.config import (
    MATTERMOST_KEYS,
    ensure_root_env,
    identity_values,
    load_config,
    load_root_env,
    parse_env_text,
    process_env,
    save_config,
    write_env_values,
)
from sindarin.names import (
    ANALYST,
    BOOKKEEPER,
    DISPLAY,
    LEDGER_EXECUTOR,
    PLANNER,
    SWITCH_EXECUTOR,
)
from sindarin.switch.client import ConnectionState, SwitchClient, SwitchError
from sindarin.switch.events import MessageEvent
from sindarin.switch.gateway import GatewayClient, GatewayError
from sindarin.switch.mattermost import MattermostClient, MattermostError

PEOPLE_MODES = ("me", "cast", "both")


@dataclass(frozen=True)
class Person:
    """A human the bootstrap puts in rooms and groups. `everywhere`: a member of every room and
    the feature rooms' PM and engineer (the driver). `create`: bootstrap creates the account."""

    username: str
    display: str
    password: str
    groups: tuple[str, ...]
    everywhere: bool = False
    create: bool = True


def _email(username: str) -> str:
    return f"{username}@sindarin.local"


def cast_people(demo_pass: str) -> list[Person]:
    """The management team (design v5 §6): John the CFO, Leonardo the controller, Isaac the
    CEO who asks for the plan and sits on the board, Vincent in sales and in no group, for the
    refusals. Created on the Mattermost server with `demo_pass`."""
    return [
        Person("isaac", "Isaac", demo_pass, ("board",)),
        Person("leonardo", "Leonardo", demo_pass, ("controllers",)),
        Person("john", "John", demo_pass, ("cfo",)),
        Person("vincent", "Vincent", demo_pass, ()),
    ]


def driver_person(target: MattermostTarget, demo_pass: str) -> Person:
    """You: the Mattermost user account Switch's stack hands out next to the admin
    (`MATTERMOST_USER` / `MATTERMOST_USER_PASSWORD`), put in every approver group and every room
    so one person drives the demo from the ask to the register entries. Not the admin: Switch
    signs in as that account, so its posts are the bridge's own and never become events
    (seen 2026-11-06)."""
    username = target.user.strip()
    if not username:
        raise ValueError(
            "MATTERMOST_USER is empty: the account you drive the demo from. The Switch stack's "
            ".env has one; for a server of your own set MATTERMOST_USER and "
            "MATTERMOST_USER_PASSWORD in .env, or run bootstrap with --people cast"
        )
    if username == target.admin_user:
        raise ValueError(
            f"MATTERMOST_USER {username!r} is the admin Switch signs in as; its posts are the "
            "bridge's own and never reach the executors. Use another account."
        )
    return Person(
        username,
        username.capitalize(),
        target.user_password or demo_pass,
        ("cfo", "controllers", "board"),
        everywhere=True,
    )


def build_people(mode: str, target: MattermostTarget, demo_pass: str) -> list[Person]:
    if mode not in PEOPLE_MODES:
        raise ValueError(f"people must be one of {', '.join(PEOPLE_MODES)}, not {mode!r}")
    people: list[Person] = []
    if mode in ("me", "both"):
        people.append(driver_person(target, demo_pass))
    if mode in ("cast", "both"):
        people += [
            c for c in cast_people(demo_pass) if c.username not in {p.username for p in people}
        ]
    return people


def _describe(p: Person) -> str:
    groups = ", ".join(p.groups) or "no group"
    return f"{p.username} ({groups}{'; drives the demo' if p.everywhere else ''})"


def room_users(spec: RoomSpec, people: list[Person]) -> list[str]:
    """Who sits in a demo room: everyone marked `everywhere`, then the cast the spec names."""
    names = {p.username for p in people}
    out = [p.username for p in people if p.everywhere]
    out += [u for u in spec.users if u in names and u not in out]
    return out


AGENTS: dict[str, tuple[str, str]] = {
    ANALYST: (
        DISPLAY[ANALYST],
        "Reports the month from the ledger, against its targets, and tells the business when the "
        "period is planned. Proposes nothing.",
    ),
    PLANNER: (
        DISPLAY[PLANNER],
        "Proposes the period's goals from the basis month's actuals, then the budget under the "
        "approved cap. Cannot record or spend.",
    ),
    BOOKKEEPER: (
        DISPLAY[BOOKKEEPER],
        "Proposes the budget register entries once the budget is recorded. Cannot post.",
    ),
    SWITCH_EXECUTOR: (
        DISPLAY[SWITCH_EXECUTOR],
        "Deterministic. Verifies the CFO's approval by participant id and opens the plan room.",
    ),
    LEDGER_EXECUTOR: (
        DISPLAY[LEDGER_EXECUTOR],
        "Deterministic. Verifies approvals by participant id, records the budget and posts its "
        "register entries. The only process that writes the ledger.",
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
    "acme-finance": RoomSpec(
        "finance",
        "channel_public",
        (ANALYST, PLANNER, SWITCH_EXECUTOR),
        ("isaac", "john", "leonardo"),
        "Acme's finance room. The month is asked for, reported and its goals set here; the "
        "board is pinned here.",
        "The business asks `@analyst @planner plan <month>`. @analyst reports the basis month; "
        "@planner proposes the goals as a `sindarin-proposal` fence. Only the cfo's "
        "`@sindarin-switch approve GOALS-n` opens the plan room. deny / changes / status / "
        "whoami / help / board / brief.",
    ),
    "planning": RoomSpec(
        "planning",
        "channel_private",
        (PLANNER, LEDGER_EXECUTOR),
        ("john", "isaac", "leonardo"),
        "Budgets are proposed and approved here. Approve with `@sindarin-ledger approve BUDGET-n`.",
        "@planner proposes the budget under the approved cap. Only `@sindarin-ledger approve "
        "BUDGET-n` from the cfo records it (the board above the limit). deny / changes / "
        "status / whoami / help.",
    ),
}
PROBE_ROOM = "sindarin-ids"  # private, all five agents and the people; never in the demo


@dataclass
class Report:
    lines: list[str] = field(default_factory=list)
    ok: bool = True
    agents: dict[str, dict] = field(default_factory=dict)  # handle → {id, api_key}
    people: dict[str, dict] = field(default_factory=dict)  # username → mattermost user
    rooms: dict[str, str] = field(default_factory=dict)  # room name → switch room id
    people_spec: list[Person] = field(default_factory=list)  # who the demo is for (--people)
    channels: dict[str, str] = field(default_factory=dict)  # room name → mattermost channel id
    participant_ids: dict[str, str] = field(default_factory=dict)  # handle | username → id
    bridge_id: str = ""

    def say(self, line: str) -> None:
        self.lines.append(line)

    def fail(self, line: str) -> None:
        self.ok = False
        self.lines.append("FAIL " + line)


@dataclass
class MattermostTarget:
    """Which Mattermost the demo runs in: the bundled one from the stack env, or a server of your
    own named by `MATTERMOST_URL` in the root `.env` (DDD §12.2 "Connect Mattermost")."""

    url: str  # as people and this machine reach it
    admin_user: str
    admin_password: str
    team_name: str
    internal_url: str = ""  # as the Switch container reaches it; defaults to url
    bridge_name: str = ""  # the Gateway connection's display name
    own: bool = False  # True when it came from the root .env
    user: str = ""  # the person's account next to the admin (MATTERMOST_USER); drives --people me
    user_password: str = ""

    @classmethod
    def from_stack(cls, stack: dict[str, str], url: str | None = None) -> MattermostTarget:
        return cls(
            url=(url or f"http://localhost:{stack.get('MATTERMOST_HOST_PORT') or '8065'}").rstrip(
                "/"
            ),
            admin_user=stack.get("MATTERMOST_ADMIN_USER", ""),
            admin_password=stack.get("MATTERMOST_ADMIN_PASSWORD", ""),
            team_name=stack.get("MATTERMOST_TEAM_NAME") or "switch",
            user=stack.get("MATTERMOST_USER", ""),
            user_password=stack.get("MATTERMOST_USER_PASSWORD", ""),
        )

    @classmethod
    def from_values(cls, v: dict[str, str]) -> MattermostTarget:
        """The `MATTERMOST_*` keys of a root .env."""
        url = v.get("MATTERMOST_URL", "").rstrip("/")
        missing = [
            k
            for k in ("MATTERMOST_URL", "MATTERMOST_ADMIN_USER", "MATTERMOST_ADMIN_PASSWORD")
            if not v.get(k)
        ]
        if missing:
            raise ValueError(f".env names a Mattermost server but lacks {', '.join(missing)}")
        host = url.split("://", 1)[-1].split("/", 1)[0]
        return cls(
            url=url,
            admin_user=v["MATTERMOST_ADMIN_USER"],
            admin_password=v["MATTERMOST_ADMIN_PASSWORD"],
            team_name=v.get("MATTERMOST_TEAM_NAME") or "switch",
            internal_url=(v.get("MATTERMOST_INTERNAL_URL") or url).rstrip("/"),
            bridge_name=v.get("SWITCH_BRIDGE_NAME") or f"Mattermost {host}",
            own=True,
            user=v.get("MATTERMOST_USER", ""),
            user_password=v.get("MATTERMOST_USER_PASSWORD", ""),
        )

    @classmethod
    def resolve(
        cls, repo: Path, stack: dict[str, str], *, url: str | None = None
    ) -> MattermostTarget:
        """A server of your own when the root .env sets `MATTERMOST_URL`; else the bundled
        Mattermost of the compose stack."""
        values = load_root_env(repo, MATTERMOST_KEYS)
        if values.get("MATTERMOST_URL"):
            return cls.from_values(values)
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


def create_room_patiently(admin: GatewayClient, **kw: Any) -> dict:
    """`POST /gateway/rooms`, retried on a 5xx. Right after a bridge is connected, Switch's admin
    session on the new Mattermost is not ready for a second or two and the Gateway answers 500
    ("Invalid or expired session") instead of waiting (seen 2026-11-06)."""
    last: GatewayError | None = None
    for attempt in range(6):
        try:
            return admin.create_room(**kw)
        except GatewayError as exc:
            if exc.status < 500:
                raise
            last = exc
            time.sleep(2 + 2 * attempt)
    assert last is not None
    raise last


def rooms_on(admin: GatewayClient, bridge_id: str) -> list[dict]:
    """The Gateway's rooms on one bridge. With a second Mattermost connected, `#planning` exists
    once per bridge; only the rooms of the bridge the demo runs on count."""
    rooms = admin.list_rooms()
    if not bridge_id:
        return rooms
    return [r for r in rooms if str(r.get("bridge_id") or "") == bridge_id]


def rooms_by_name(admin: GatewayClient, bridge_id: str) -> dict[str, dict]:
    return {(r.get("name") or r.get("slug")): r for r in rooms_on(admin, bridge_id)}


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


def render_policy(ids: dict[str, str], rooms: dict[str, str], people: list[Person]) -> str:
    def pid(key: str) -> str:
        return ids.get(key, f"@switch-member-REPLACE-ME-{key}:localhost")

    members_of = {g: [p for p in people if g in p.groups] for g in ("cfo", "controllers", "board")}
    groups = "\n".join(
        f"  {g}: [{', '.join(chr(34) + pid(p.username) + chr(34) for p in members)}]"
        + f"            # {', '.join(p.display for p in members)}"
        for g, members in members_of.items()
    )
    outsiders = [p.display for p in people if not p.groups]
    if outsiders:
        groups += f"\n  # {', '.join(outsiders)}: in no group (sales approves nothing)"
    proposers = "\n".join(f'  {r}: "{pid(r)}"' for r in (ANALYST, PLANNER, BOOKKEEPER))
    executors = "\n".join(f'  {e}: "{pid(e)}"' for e in (SWITCH_EXECUTOR, LEDGER_EXECUTOR))
    room_lines = "\n".join(f'  {k}: "{rooms.get(k, "")}"' for k in ("finance", "planning"))
    return f"""# Sindarin policy (design v5 §6). Agents draft from it; both executors enforce it.
# Every id is a Switch participant id; nothing in this file is a display name.
# `sindarin bootstrap` writes the ids and room ids. Learn yours with "@sindarin-switch whoami".
version: 3

groups:
{groups}

proposers:
{proposers}

executors:
{executors}

rooms:
{room_lines}

rules:
  - name: goals
    kind: goals
    proposer: planner
    approvers: cfo
    also_invite: controllers  # sit in the plan room too
    room: finance
    currency: USD
    pending_ttl: 24h
  - name: budget
    kind: budget
    proposer: planner
    approvers: board
    approvers_under_limit: cfo
    limit_cents: 1000000      # 10,000.00 USD: up to it the cfo approves alone
    room: planning
    currency: USD
    pending_ttl: 24h
  - name: entries
    kind: entries
    proposer: bookkeeper
    approvers: controllers
    room: period             # the plan room the goals opened
    currency: USD
    pending_ttl: 24h
"""


def render_users(ids: dict[str, str], people: list[Person]) -> str:
    rows = []
    for p in people:
        pid = ids.get(p.username, f"@switch-member-REPLACE-ME-{p.username}:localhost")
        rows.append(f'  "{pid}": {{ name: {p.display} }}')
    body = "\n".join(rows)
    return f"""# Participant id → display name (design v5 §6). Names are for reading, never for
# authorization. Written by `sindarin bootstrap`.
version: 2
users:
{body}
"""


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
    addressing: str = "rooms",
    people: str = "me",
) -> Report:
    rep = Report()
    stack = parse_env_text(stack_env.read_text(encoding="utf-8"))
    api = (api_url or f"http://localhost:{stack.get('API_HOST_PORT') or '8000'}").rstrip("/")
    try:
        target = MattermostTarget.resolve(repo, stack, url=mattermost_url)
        cast = build_people(people, target, demo_pass)
    except ValueError as exc:
        rep.fail(str(exc))
        return rep
    rep.people_spec = cast
    rep.say("people: " + ", ".join(_describe(p) for p in cast))
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

    # 2. the five agents; identities go to the root .env under each process's prefix
    env_path = ensure_root_env(repo)
    server_agents = {a.get("name"): a for a in admin.list_agents()}
    for handle, (display, description) in AGENTS.items():
        values = process_env(repo, handle)
        on_server = server_agents.get(handle)
        have_key = bool(values.get("SWITCH_AGENT_ID") and values.get("SWITCH_AGENT_API_KEY"))
        if on_server and have_key and values["SWITCH_AGENT_ID"] == on_server.get("id"):
            rep.agents[handle] = {"id": on_server["id"], "api_key": values["SWITCH_AGENT_API_KEY"]}
            rep.say(f"agent {handle}: exists id={on_server['id']} (key in .env reused)")
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
        identity = {
            "SWITCH_AGENT_ID": reg["id"],
            "SWITCH_AGENT_API_KEY": reg["api_key"],
            "SWITCH_CONNECTION_ID": str(uuid.uuid4()),
        }
        write_env_values(env_path, {"SWITCH_BRIDGE_URL": api, **identity_values(handle, identity)})
        rep.agents[handle] = {"id": reg["id"], "api_key": reg["api_key"]}
        rep.say(f"agent {handle}: registered id={reg['id']}; key written to .env (mode 600)")
    if len(rep.agents) != len(AGENTS):
        return rep
    for handle, a in rep.agents.items():  # open until the rooms exist; scoped below
        try:
            admin.set_addressing_open(a["id"])
        except GatewayError as exc:
            rep.fail(f"addressing {handle}: {exc}")
    # sindarin-switch needs a Gateway credential to open rooms (§4.2); only it is handed these
    write_env_values(
        env_path,
        {
            "SWITCH_GATEWAY_URL": api,
            "SWITCH_GATEWAY_EMAIL": admin_email,
            "SWITCH_GATEWAY_PASSWORD": admin_password,
        },
    )
    rep.say(".env: Gateway credential for sindarin-switch written (mode 600)")

    # 3. the demo people in Mattermost (§17 step 3)
    mm: MattermostClient | None = None
    team_id = ""
    try:
        mm = MattermostClient.login(mm_url, target.admin_user, target.admin_password)
        team_id = mm.team_by_name(team_name)["id"]
        for i, person in enumerate(cast):
            user = None if person.create else mm.user_by_username(person.username)
            created = False
            if user is None:
                user, created = mm.ensure_user(
                    person.username, _email(person.username), person.password, person.display
                )
            rep.people[person.username] = user
            profile = user.get("nickname") or user.get("first_name")  # how Mattermost shows them
            if profile and profile != person.display:
                cast[i] = replace(person, display=str(profile))
            with contextlib.suppress(MattermostError):
                mm.add_to_team(team_id, user["id"])
            rep.say(f"mattermost user {person.username}: {'created' if created else 'exists'}")
    except (MattermostError, httpx.HTTPError, KeyError) as exc:
        rep.fail(
            f"mattermost at {mm_url}: {str(exc)[:160]} "
            + ("(check MATTERMOST_* in .env)" if target.own else "(is the collab profile up?)")
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
        existing = rooms_by_name(admin, rep.bridge_id)
        for name, spec in ROOMS.items():
            room = existing.get(name)
            if room is None:
                room = create_room_patiently(
                    admin,
                    name=name,
                    description=spec.description,
                    instructions=spec.instructions,
                    bridge_id=rep.bridge_id,
                    channel_type=spec.channel_type,
                    agent_ids=[rep.agents[h]["id"] for h in spec.agents],
                    user_names=room_users(spec, cast),
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
                want_users = [u for u in room_users(spec, cast) if u not in set(members["users"])]
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
        _finish_files(repo, rep, write_ids=False)
        admin.close()
        return rep

    # 5. participant ids (verify item 2): a private probe room holds all five agents
    # and the four people. We watch sindarin-switch's stream while the other agents post markers
    # and the people post `whoami` from Mattermost; then the analyst's stream while sindarin-switch
    # posts its own marker (an agent does not see its own posts).
    probe_room = ""
    channel: str | None = None
    try:
        existing = rooms_by_name(admin, rep.bridge_id)
        probe = existing.get(PROBE_ROOM)
        if probe is None:
            probe = create_room_patiently(
                admin,
                name=PROBE_ROOM,
                description="Where participant ids are learned at bootstrap. Not part of the demo.",
                instructions="Bootstrap only. Agents post one marker each; people answer whoami.",
                bridge_id=rep.bridge_id,
                channel_type="channel_private",
                agent_ids=[a["id"] for a in rep.agents.values()],
                user_names=[p.username for p in cast],
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
        probe_room = rep.rooms["acme-finance"]
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
    unverified: list[str] = []
    if channel and rep.people:
        for person in cast:
            marker = f"@{SWITCH_EXECUTOR} whoami [{uuid.uuid4().hex[:6]}]"
            markers[marker] = person.username

            def _say(person=person, m=marker):
                try:
                    u = MattermostClient.login(mm_url, person.username, person.password)
                except MattermostError:
                    # an account we do not hold the password of: the id is stable by
                    # construction (spike item 2); `whoami` in #acme-finance confirms it
                    rep.participant_ids[person.username] = (
                        f"@switch-mattermost-{rep.bridge_id}-{person.username}:localhost"
                    )
                    unverified.append(person.username)
                    return None
                try:
                    return u.create_post(channel, m)
                finally:
                    u.close()

            actions.append(_say)
    else:
        rep.say(
            "people's ids: no Mattermost channel to post from; ask each person to type "
            f"`@{SWITCH_EXECUTOR} whoami` in #acme-finance and paste the ids into policy.yaml"
        )
    try:
        events = observe_as_agent(api, sw["id"], sw["api_key"], actions)
        prod = rep.agents[ANALYST]
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
    people_seen = [p.username for p in cast if p.username in addressed]
    if people_seen:
        rep.say(
            "addressed (verify item 2): "
            + ", ".join(f"{u}={'yes' if addressed[u] else 'no'}" for u in people_seen)
        )
    if unverified:
        rep.say(
            f"participant id by construction for {', '.join(unverified)} (no password to post "
            f"as them); confirm with `@{SWITCH_EXECUTOR} whoami` in #acme-finance"
        )
    expected = [*AGENTS, *(p.username for p in cast)]
    missing = [n for n in expected if n not in rep.participant_ids]
    if missing:
        rep.fail(f"no participant id for: {', '.join(missing)} ({len(events)} frames seen)")

    # 6. files (§17 step 5)
    _finish_files(repo, rep, write_ids=write_ids and not missing)

    # 7. an empty board, pinned (§17 step 6, §14)
    finance_room = rep.rooms["acme-finance"]
    finance_channel = rep.channels.get("acme-finance")
    try:
        SwitchClient(api, sw["id"], sw["api_key"]).post(
            finance_room,
            "**Board** · bootstrap\n**In flight**\nnothing in flight\n"
            "**Waiting on**\nno pending approvals",
        )
        pinned = False
        if mm is not None and finance_channel:
            for _ in range(10):
                posts = mm.posts(finance_channel, per_page=20)
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
            "board: posted and pinned in #acme-finance"
            if pinned
            else "board: posted in #acme-finance; pin it in Mattermost"
        )
    except (SwitchError, MattermostError) as exc:
        rep.say(f"board: not posted ({str(exc)[:80]})")
    if mm is not None:
        mm.close()
    admin.close()
    return rep


def _set_addressing(admin: GatewayClient, rep: Report, mode: str) -> None:
    """Layer 0: each agent may be addressed only in the rooms it belongs to (`rooms`), or by
    anyone (`open`). Plan rooms are added by sindarin-switch when it opens them. The probe
    room is included so ids can be re-learned. Verified live 2026-11-05 (§12.4 item 5): a
    mention from elsewhere arrives `addressed: false` and Switch refuses it in the room."""
    if mode != "rooms":
        rep.say("addressing: anyone, for all five (the executors refuse in code)")
        return
    listing = rooms_on(admin, rep.bridge_id)
    probe = [str(r.get("id")) for r in listing if r.get("name") == PROBE_ROOM and r.get("id")]
    plan_rooms = [
        str(r.get("id"))
        for r in listing
        if str(r.get("name", "")).startswith("plan-") and not r.get("archived") and r.get("id")
    ]
    for handle, a in rep.agents.items():
        rooms = [rep.rooms[name] for name, spec in ROOMS.items() if handle in spec.agents]
        if handle in (PLANNER, BOOKKEEPER, LEDGER_EXECUTOR, SWITCH_EXECUTOR):
            rooms += plan_rooms
        rooms += probe
        try:
            admin.set_addressing_rooms(a["id"], rooms)
        except GatewayError as exc:
            rep.fail(f"addressing {handle}: {exc}")
    rep.say(
        "addressing: room-scoped for all five (Switch refuses a mention from elsewhere on the "
        "agent's behalf; the executors still refuse in code inside their rooms)"
    )


def _finish_files(repo: Path, rep: Report, *, write_ids: bool) -> None:
    ids = dict(rep.participant_ids)
    rooms = {ROOMS[name].key: rid for name, rid in rep.rooms.items()}
    if write_ids:
        (repo / "agents" / "policy" / "policy.yaml").write_text(
            render_policy(ids, rooms, rep.people_spec)
        )
        (repo / "agents" / "policy" / "users.yaml").write_text(render_users(ids, rep.people_spec))
        rep.say("wrote agents/policy/policy.yaml and users.yaml (ids, rooms)")
    for short, handle in (("switch", SWITCH_EXECUTOR), ("ledger", LEDGER_EXECUTOR)):
        path = repo / "executor" / f"{short}.yaml"
        cfg = load_config(path)
        cfg.executor = handle
        if handle in ids:
            cfg.self_id = ids[handle]
        other = LEDGER_EXECUTOR if handle == SWITCH_EXECUTOR else SWITCH_EXECUTOR
        if other in ids:
            cfg.peer_ids = [ids[other]]
        cfg.peer_state_db = "ledger.db" if handle == SWITCH_EXECUTOR else "switch.db"
        if handle == SWITCH_EXECUTOR:
            cfg.bridge_id = rep.bridge_id
            directory = {
                ids[h]: rep.agents[h]["id"] for h in AGENTS if h in ids and h in rep.agents
            }
            directory.update(
                {ids[p.username]: p.username for p in rep.people_spec if p.username in ids}
            )
            cfg.directory = directory
        save_config(path, cfg)
        rep.say(f"wrote executor/{short}.yaml")
