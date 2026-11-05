from __future__ import annotations

import pytest
from pydantic import ValidationError

from sindarin.users import Users, load_users


def test_demo_users_load(repo_root):
    u = load_users(repo_root / "agents" / "policy" / "users.yaml")
    assert u.version == 2
    ids = list(u.users)
    assert all(i.startswith("@switch-") and ":" in i for i in ids)
    assert "Isaac" in [e.name for e in u.users.values()]
    assert u.id_for_login("isaac-gh") == ids[0] and u.id_for_login("ISAAC-GH") == ids[0]
    assert u.id_for_login("") is None and u.login_for(ids[1]) is None


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
