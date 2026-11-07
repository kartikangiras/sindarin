"""People on the team (design v5 §6): add a colleague to the rooms and to an approver group, list
who approves what, take someone out of the groups. The policy and users files are edited in
place, one line each, so limits, rooms and comments a team set by hand stay as they are. The
executors reload the policy when the file changes; nothing restarts."""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sindarin.policy import load_policy
from sindarin.users import load_users

GROUPS = ("cfo", "controllers", "board")
ID_RE = re.compile(r"^@switch-mattermost-([0-9a-f-]{36})-(.+):localhost$")
_GROUP_LINE = r"^(?P<indent>  ){group}: \[(?P<ids>[^\]]*)\](?P<tail>.*)$"


class PeopleError(Exception):
    """A refusal a person can read: an unknown group, a file the bootstrap has not written."""


def participant_id(bridge_id: str, username: str) -> str:
    """A Mattermost member's Switch participant id, by construction (verified live 2026-11-05)."""
    return f"@switch-mattermost-{bridge_id}-{username}:localhost"


def check_groups(groups: list[str]) -> list[str]:
    bad = [g for g in groups if g not in GROUPS]
    if bad:
        raise PeopleError(f"unknown group {', '.join(bad)}; the groups are {', '.join(GROUPS)}")
    return list(dict.fromkeys(groups))


@dataclass(frozen=True)
class Member:
    username: str
    display: str
    participant_id: str
    groups: tuple[str, ...]


def members(policy_path: Path, users_path: Path) -> list[Member]:
    """Everyone users.yaml names, with the groups policy.yaml puts them in."""
    policy = load_policy(policy_path)
    users = load_users(users_path)
    out = []
    for pid, entry in users.users.items():
        m = ID_RE.match(pid)
        # a Mattermost id carries the username; any other id is read back to its last segment
        username = m.group(2) if m else re.sub(r"^@switch-[a-z]+-", "", pid).split(":")[0]
        groups = tuple(g for g in GROUPS if pid in policy.groups.get(g, []))
        out.append(Member(username, entry.name, pid, groups))
    return out


def bridge_id_in(policy_path: Path, users_path: Path) -> str:
    """The bridge every known participant id carries, or '' when none is a Mattermost id."""
    for m in members(policy_path, users_path):
        hit = ID_RE.match(m.participant_id)
        if hit:
            return hit.group(1)
    return ""


# -- the files, edited in place ------------------------------------------------------------------


def _names(users_text: str) -> dict[str, str]:
    return {
        m.group(1): m.group(2).strip()
        for m in re.finditer(r'^  "([^"]+)": \{ name: (.*?) \}\s*$', users_text, re.M)
    }


def set_groups_text(policy_text: str, users_text: str, pid: str, groups: list[str]) -> str:
    """policy.yaml with `pid` in exactly `groups`: each group line is rewritten, its trailing
    comment listing the members' names as the bootstrap writes it."""
    names = _names(users_text)
    text = policy_text
    for g in GROUPS:
        m = re.search(_GROUP_LINE.format(group=g), text, re.M)
        if m is None:
            raise PeopleError(
                f"policy.yaml has no `{g}:` group line; run `sindarin bootstrap` first"
            )
        ids = [x.strip().strip('"') for x in m.group("ids").split(",") if x.strip()]
        before = list(ids)
        if g in groups and pid not in ids:
            ids.append(pid)
        if g not in groups and pid in ids:
            ids.remove(pid)
        if ids == before:
            continue  # a line whose members did not change is left exactly as it was
        comment = ", ".join(names.get(i, i.split("-")[-1].split(":")[0]) for i in ids)
        line = f"  {g}: [{', '.join(chr(34) + i + chr(34) for i in ids)}]"
        if comment:
            line += f"            # {comment}"
        text = text[: m.start()] + line + text[m.end() :]
    return text


def set_user_text(users_text: str, pid: str, display: str) -> str:
    """users.yaml with `pid` named `display`: the line is replaced or appended under `users:`."""
    line = f'  "{pid}": {{ name: {display} }}'
    pattern = re.compile(rf'^  "{re.escape(pid)}": \{{ name: .*? \}}\s*$', re.M)
    if pattern.search(users_text):
        return pattern.sub(line, users_text, count=1)
    if not re.search(r"^users:\s*$", users_text, re.M):
        raise PeopleError("users.yaml has no `users:` block; run `sindarin bootstrap` first")
    return users_text.rstrip("\n") + "\n" + line + "\n"


def write_member(
    policy_path: Path, users_path: Path, pid: str, display: str, groups: list[str]
) -> None:
    users_text = set_user_text(users_path.read_text(), pid, display)
    policy_text = set_groups_text(policy_path.read_text(), users_text, pid, groups)
    load_policy_text(policy_text)  # refuse to write a policy the executors would reject
    users_path.write_text(users_text)
    policy_path.write_text(policy_text)


def load_policy_text(text: str) -> None:
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(text)
        path = Path(f.name)
    try:
        load_policy(path)
    finally:
        path.unlink(missing_ok=True)


# -- the live part: the account, the team, the rooms --------------------------------------------


def add_to_rooms(
    *,
    gateway: Any,
    mattermost: Any,
    team_id: str,
    username: str,
    room_ids: list[str],
    channel_names: list[str],
) -> dict[str, int]:
    """Put the Mattermost account into every room on both sides: the Switch rooms by username,
    the bridged channels by Mattermost user id. Fail-soft per room; returns counts."""
    user = mattermost.user_by_username(username)
    if user is None:
        raise PeopleError(f"no Mattermost account {username!r}")
    joined = {"rooms": 0, "channels": 0}
    with contextlib.suppress(Exception):
        mattermost.add_to_team(team_id, user["id"])
    for rid in room_ids:
        with contextlib.suppress(Exception):
            gateway.add_room_users(rid, [username])
            joined["rooms"] += 1
    for name in channel_names:
        ch = mattermost.channel_by_name(team_id, name)
        if not ch:
            continue
        with contextlib.suppress(Exception):
            mattermost.add_to_channel(ch["id"], user["id"])
            joined["channels"] += 1
    return joined
