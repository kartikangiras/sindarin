from __future__ import annotations

import pytest
from pydantic import ValidationError

from sindarin.users import Users, load_users


def test_demo_users_load(repo_root, tmp_path):
    """The file the bootstrap writes for the cast loads; the local copy may hold whoever the last
    bootstrap was run for, so the cast is rendered here."""
    from sindarin.bootstrap import cast_people, render_users

    ids = {
        u: f"@switch-mattermost-b-{u}:localhost" for u in ("isaac", "leonardo", "john", "vincent")
    }
    (tmp_path / "users.yaml").write_text(render_users(ids, cast_people("demo")))
    u = load_users(tmp_path / "users.yaml")
    assert u.version == 2
    ids_in_file = list(u.users)
    assert all(i.startswith("@switch-") and ":" in i for i in ids_in_file)
    assert "Isaac" in [e.name for e in u.users.values()]
    assert u.id_for_login("") is None and u.login_for(ids["leonardo"]) is None
    # a login is still accepted and matched case-insensitively when a team records one
    with_login = Users.model_validate(
        {"version": 2, "users": {ids["isaac"]: {"name": "Isaac", "github": "isaac-gh"}}}
    )
    assert with_login.id_for_login("ISAAC-GH") == ids["isaac"]
    live = load_users(repo_root / "agents" / "policy" / "users.yaml")  # whatever bootstrap wrote
    assert live.version == 2 and all(i.startswith("@switch-") for i in live.users)


def test_lookup_is_exact_on_id_not_name():
    u = Users.model_validate({"version": 2, "users": {"@switch-user-1:s": {"name": "Isaac"}}})
    assert u.name_for("@switch-user-1:s") == "Isaac"
    assert u.name_for("Isaac") is None and u.name_for("@SWITCH-USER-1:S") is None


def test_users_reject_bad_login_and_extras():
    with pytest.raises(ValidationError):
        Users.model_validate(
            {"version": 2, "users": {"@x:s": {"name": "A", "github": "bad login"}}}
        )
    with pytest.raises(ValidationError):
        Users.model_validate({"version": 2, "users": {"@x:s": {"name": "A", "token": "t"}}})
