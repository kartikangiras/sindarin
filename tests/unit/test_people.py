"""`sindarin people`: approvers without editing YAML by hand. The files are edited in place, so
the limits and comments a team set stay."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner
from validation.harness import FIXTURES, JOHN, LEONARDO, VINCENT

from sindarin.cli import app
from sindarin.people import (
    GROUPS,
    PeopleError,
    bridge_id_in,
    check_groups,
    members,
    participant_id,
    set_groups_text,
    set_user_text,
    write_member,
)
from sindarin.policy import load_policy

runner = CliRunner()
BRIDGE = "b76af5d9-670f-4573-9f7b-6655cdb00753"
ALICE = f"@switch-mattermost-{BRIDGE}-alice:localhost"


def _files(tmp_path: Path) -> tuple[Path, Path]:
    pol, usr = tmp_path / "policy.yaml", tmp_path / "users.yaml"
    shutil.copy(FIXTURES / "policy.yaml", pol)
    shutil.copy(FIXTURES / "users.yaml", usr)
    return pol, usr


def test_participant_id_and_groups():
    assert participant_id(BRIDGE, "alice") == ALICE
    assert check_groups(["cfo", "cfo", "board"]) == ["cfo", "board"]
    with pytest.raises(PeopleError, match="unknown group ceo"):
        check_groups(["ceo"])


def test_add_then_remove_edits_only_the_group_lines(tmp_path):
    pol, usr = _files(tmp_path)
    before = pol.read_text()
    write_member(pol, usr, ALICE, "Alice", ["cfo", "controllers"])
    policy = load_policy(pol)
    assert ALICE in policy.groups["cfo"] and ALICE in policy.groups["controllers"]
    assert ALICE not in policy.groups["board"]
    assert JOHN in policy.groups["cfo"] and LEONARDO in policy.groups["controllers"]
    assert "limit_cents: 1000000" in pol.read_text()  # the rest of the file is untouched
    changed = [
        (a, b)
        for a, b in zip(before.splitlines(), pol.read_text().splitlines(), strict=True)
        if a != b
    ]
    assert len(changed) == 2  # the two group lines, nothing else
    assert '"' + ALICE + '": { name: Alice }' in usr.read_text()
    alice = next(m for m in members(pol, usr) if m.username == "alice")
    assert alice.groups == ("cfo", "controllers") and alice.display == "Alice"
    assert bridge_id_in(pol, usr) == BRIDGE
    # groups are set, not appended: a second add moves her
    write_member(pol, usr, ALICE, "Alice K", ["board"])
    policy = load_policy(pol)
    assert ALICE in policy.groups["board"] and ALICE not in policy.groups["cfo"]
    assert usr.read_text().count(ALICE) == 1 and "Alice K" in usr.read_text()
    # remove leaves the account named and in no group
    write_member(pol, usr, ALICE, "Alice K", [])
    policy = load_policy(pol)
    assert all(ALICE not in policy.groups[g] for g in GROUPS)
    assert ALICE in usr.read_text()
    assert VINCENT not in sum(policy.groups.values(), [])


def test_group_line_comments_name_the_members(tmp_path):
    pol, usr = _files(tmp_path)
    users_text = set_user_text(usr.read_text(), ALICE, "Alice")
    text = set_groups_text(pol.read_text(), users_text, ALICE, ["cfo"])
    line = next(ln for ln in text.splitlines() if ln.startswith("  cfo:"))
    assert line.endswith("# John, Alice")
    with pytest.raises(PeopleError, match="no `cfo:` group line"):
        set_groups_text("version: 3\ngroups:\n", users_text, ALICE, ["cfo"])
    with pytest.raises(PeopleError, match="no `users:` block"):
        set_user_text("version: 2\n", ALICE, "Alice")


def test_cli_list_and_remove(tmp_path):
    repo = tmp_path / "repo"
    (repo / "agents" / "policy").mkdir(parents=True)
    shutil.copy(FIXTURES / "policy.yaml", repo / "agents" / "policy" / "policy.yaml")
    shutil.copy(FIXTURES / "users.yaml", repo / "agents" / "policy" / "users.yaml")
    r = runner.invoke(app, ["people", "list", "--repo-root", str(repo)])
    assert r.exit_code == 0 and "john" in r.output and "cfo" in r.output, r.output
    r = runner.invoke(app, ["people", "remove", "john", "--repo-root", str(repo)])
    assert r.exit_code == 0, r.output
    assert JOHN not in load_policy(repo / "agents" / "policy" / "policy.yaml").groups["cfo"]
    r = runner.invoke(app, ["people", "remove", "nobody", "--repo-root", str(repo)])
    assert r.exit_code != 0
