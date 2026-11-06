"""Who the bootstrap sets the demo up for (design v5 §6): you alone by default, the management
team on request, both together; the rendered policy puts each person in exactly their groups."""

from __future__ import annotations

import pytest
import yaml

from sindarin.bootstrap import (
    ROOMS,
    MattermostTarget,
    build_people,
    render_policy,
    render_users,
    room_users,
)
from sindarin.policy import load_policy

TARGET = MattermostTarget(
    url="http://mm", admin_user="admin", admin_password="pw", team_name="t", user="kartik"
)
IDS = {
    "kartik": "@switch-mattermost-b-kartik:localhost",
    "isaac": "@switch-mattermost-b-isaac:localhost",
    "leonardo": "@switch-mattermost-b-leonardo:localhost",
    "john": "@switch-mattermost-b-john:localhost",
    "vincent": "@switch-mattermost-b-vincent:localhost",
    "analyst": "@switch-agent-a:localhost",
    "planner": "@switch-agent-p:localhost",
    "bookkeeper": "@switch-agent-b:localhost",
    "sindarin-switch": "@switch-agent-s:localhost",
    "sindarin-ledger": "@switch-agent-l:localhost",
}
ROOM_IDS = {"finance": "r-finance", "planning": "r-planning"}


def test_people_modes():
    me = build_people("me", TARGET, "demo")
    assert [p.username for p in me] == ["kartik"]
    driver = me[0]
    assert driver.groups == ("cfo", "controllers", "board") and driver.everywhere
    assert driver.password == "demo" and driver.display == "Kartik"  # no password given: demo's
    with_pw = MattermostTarget("http://mm", "admin", "pw", "t", user="ann", user_password="s3")
    assert build_people("me", with_pw, "demo")[0].password == "s3"
    # the bridge's admin cannot drive: Switch posts as that account itself
    with pytest.raises(ValueError, match="admin Switch signs in as"):
        build_people("me", MattermostTarget("http://mm", "kartik", "pw", "t", user="kartik"), "d")
    with pytest.raises(ValueError, match="MATTERMOST_USER is empty"):
        build_people("me", MattermostTarget("http://mm", "admin", "pw", "t"), "demo")

    cast = build_people("cast", TARGET, "demo")
    assert [p.username for p in cast] == ["isaac", "leonardo", "john", "vincent"]
    assert cast[1].groups == ("controllers",) and cast[2].groups == ("cfo",)
    assert cast[0].groups == ("board",) and cast[3].groups == ()
    assert not any(p.everywhere for p in cast)

    both = build_people("both", TARGET, "demo")
    assert [p.username for p in both] == ["kartik", "isaac", "leonardo", "john", "vincent"]
    with pytest.raises(ValueError, match="people must be one of"):
        build_people("nobody", TARGET, "demo")


def test_room_seats_follow_the_people():
    both = build_people("both", TARGET, "demo")
    assert room_users(ROOMS["acme-finance"], both) == ["kartik", "isaac", "john", "leonardo"]
    assert room_users(ROOMS["planning"], both) == ["kartik", "john", "isaac", "leonardo"]
    me = build_people("me", TARGET, "demo")
    assert room_users(ROOMS["planning"], me) == ["kartik"]
    cast = build_people("cast", TARGET, "demo")
    assert room_users(ROOMS["planning"], cast) == ["john", "isaac", "leonardo"]


def test_rendered_policy_and_users(tmp_path):
    me = build_people("me", TARGET, "demo")
    text = render_policy(IDS, ROOM_IDS, me)
    (tmp_path / "policy.yaml").write_text(text)
    policy = load_policy(tmp_path / "policy.yaml")
    assert policy.groups == {
        "cfo": [IDS["kartik"]],
        "controllers": [IDS["kartik"]],
        "board": [IDS["kartik"]],
    }
    assert policy.proposer_id("planner") == IDS["planner"]
    assert policy.room_id("planning") == "r-planning"
    assert policy.rule("budget").limit_cents == 1000000
    assert policy.rule("budget").approvers_under_limit == "cfo"
    assert "Vincent" not in text
    users = yaml.safe_load(render_users(IDS, me))["users"]
    assert users == {IDS["kartik"]: {"name": "Kartik"}}

    both = build_people("both", TARGET, "demo")
    text = render_policy(IDS, ROOM_IDS, both)
    (tmp_path / "policy.yaml").write_text(text)
    policy = load_policy(tmp_path / "policy.yaml")
    assert policy.groups["controllers"] == [IDS["kartik"], IDS["leonardo"]]
    assert policy.groups["cfo"] == [IDS["kartik"], IDS["john"]]
    assert policy.groups["board"] == [IDS["kartik"], IDS["isaac"]]
    assert not policy.groups_of(IDS["vincent"])
    assert "Vincent: in no group" in text
    users = yaml.safe_load(render_users(IDS, both))["users"]
    assert users[IDS["isaac"]] == {"name": "Isaac"} and users[IDS["john"]] == {"name": "John"}
