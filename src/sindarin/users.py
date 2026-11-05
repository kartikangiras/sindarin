"""users.yaml: participant id → display name and, for engineers, GitHub login (DDD §10.2).

Used only to print names and to recognise a PR author; never for authorization.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from sindarin.schema import LOGIN_PATTERN


class UserEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)
    github: str | None = Field(default=None, pattern=LOGIN_PATTERN)


class Users(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = 2
    users: dict[str, UserEntry] = Field(default_factory=dict)

    def name_for(self, participant_id: str) -> str | None:
        entry = self.users.get(participant_id)
        return entry.name if entry else None

    def login_for(self, participant_id: str) -> str | None:
        entry = self.users.get(participant_id)
        return entry.github if entry else None

    def id_for_login(self, login: str) -> str | None:
        """The participant id registered with this GitHub login, if any (exact, case-insensitive
        as GitHub logins are)."""
        if not login:
            return None
        for pid, entry in self.users.items():
            if entry.github and entry.github.lower() == login.lower():
                return pid
        return None


def load_users(path: str | Path) -> Users:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: users file must be a mapping")
    return Users.model_validate(data)
