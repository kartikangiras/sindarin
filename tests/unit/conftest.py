from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def repo_root() -> Path:
    return REPO


@pytest.fixture
def demo_policy_path() -> Path:
    return REPO / "agents" / "policy" / "policy.yaml"


@pytest.fixture
def demo_users_path() -> Path:
    return REPO / "agents" / "policy" / "users.yaml"


from validation.harness import harness, temp_policy  # noqa: E402, F401
