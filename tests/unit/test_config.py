from __future__ import annotations

import os
from pathlib import Path

import pytest

from sindarin.executor.config import (
    Env,
    ExecutorConfig,
    find_executor_dir,
    load_config,
    load_env,
    load_executor,
    parse_env_text,
    process_env,
    render_config,
    save_config,
    write_env_values,
)
from sindarin.names import LEDGER_EXECUTOR, SWITCH_EXECUTOR


def test_parse_env_text():
    text = """
# comment
SWITCH_BRIDGE_URL=http://localhost:8000
export SWITCH_AGENT_ID='a1'
SWITCH_AGENT_API_KEY="k e y"
EMPTY=
"""
    assert parse_env_text(text) == {
        "SWITCH_BRIDGE_URL": "http://localhost:8000",
        "SWITCH_AGENT_ID": "a1",
        "SWITCH_AGENT_API_KEY": "k e y",
        "EMPTY": "",
    }


def test_write_env_values_preserves_lines_and_mode(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# keep me\nSWITCH_BRIDGE_URL=http://old\nANTHROPIC_API_KEY=\n")
    write_env_values(p, {"SWITCH_BRIDGE_URL": "http://new", "ANALYST_SWITCH_AGENT_ID": "a1"})
    assert p.read_text() == (
        "# keep me\nSWITCH_BRIDGE_URL=http://new\nANTHROPIC_API_KEY=\nANALYST_SWITCH_AGENT_ID=a1\n"
    )
    assert (p.stat().st_mode & 0o777) == 0o600


def test_load_env_process_env_overrides(tmp_path, monkeypatch):
    p = tmp_path / "x.env"
    p.write_text("SWITCH_GATEWAY_PASSWORD=file-pw\n")
    monkeypatch.setenv("SWITCH_GATEWAY_PASSWORD", "env-pw")
    env = Env.from_values(load_env(p))
    assert env.gateway_password == "env-pw"
    assert Env.from_values(load_env(p, env_override=False)).gateway_password == "file-pw"
    assert env.missing("SWITCH_AGENT_ID", "SWITCH_GATEWAY_PASSWORD") == ["SWITCH_AGENT_ID"]
    assert Env.from_values({"SWITCH_BRIDGE_URL": "http://x/"}).gateway_url == "http://x"


def test_config_round_trip_and_load_executor(tmp_path):
    cfg = ExecutorConfig(
        executor=LEDGER_EXECUTOR, self_id="@switch-agent-7:s", peer_ids=["@switch-agent-8:s"]
    )
    exec_dir = tmp_path / "executor"
    exec_dir.mkdir()
    save_config(exec_dir / "ledger.yaml", cfg)
    again = load_config(exec_dir / "ledger.yaml")
    assert again == cfg and again.missing() == [] and again.short == "ledger"
    assert "the only process that writes" in render_config(cfg)
    assert again.db_path(exec_dir) == exec_dir / "ledger.db"
    assert again.resolve(exec_dir, again.ledger_db) == exec_dir / "books.db"
    assert load_config(exec_dir / "missing.yaml").missing() == ["self_id"]
    (tmp_path / ".env").write_text(
        "SWITCH_GATEWAY_EMAIL=a@b\nSINDARIN_SWITCH_SWITCH_AGENT_ID=sw-id\nANTHROPIC_API_KEY=sk\n"
    )
    env, scfg = load_executor(exec_dir, "switch")
    assert scfg.executor == SWITCH_EXECUTOR and env.gateway_email == "a@b"
    assert env.agent_id == "sw-id"
    env, lcfg = load_executor(exec_dir, "ledger")
    assert lcfg.executor == LEDGER_EXECUTOR and env.gateway_email == ""  # not its credential
    assert cfg.resolve(exec_dir, "/abs/policy.yaml") == Path("/abs/policy.yaml")


def test_find_executor_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("SINDARIN_EXECUTOR_DIR", raising=False)
    assert find_executor_dir(tmp_path / "x") == (tmp_path / "x").resolve()
    monkeypatch.setenv("SINDARIN_EXECUTOR_DIR", str(tmp_path / "y"))
    assert find_executor_dir() == (tmp_path / "y").resolve()
    monkeypatch.delenv("SINDARIN_EXECUTOR_DIR")
    (tmp_path / "proj" / "executor").mkdir(parents=True)
    (tmp_path / "proj" / "executor" / "ledger.yaml").write_text("")
    (tmp_path / "proj" / "sub").mkdir()
    monkeypatch.chdir(tmp_path / "proj" / "sub")
    assert find_executor_dir() == (tmp_path / "proj" / "executor").resolve()


def test_committed_examples_and_gitignore(repo_root):
    """One env template at the root: every key a person fills and every identity bootstrap writes,
    all empty but the defaults; the real .env and the state files are ignored."""
    from sindarin.executor.config import IDENTITY_KEYS, PROCESSES, env_prefix

    root = parse_env_text((repo_root / ".env.example").read_text())
    assert set(root) >= {"ANTHROPIC_API_KEY", "MATTERMOST_URL", "MATTERMOST_USER"}
    assert set(root) >= {"SWITCH_GATEWAY_EMAIL", "SWITCH_GATEWAY_PASSWORD"}
    assert not any("GITHUB" in k or "CODER" in k for k in root)
    for process in PROCESSES:
        for key in IDENTITY_KEYS:
            assert env_prefix(process) + key in root, (process, key)
    defaults = {"MATTERMOST_TEAM_NAME", "SWITCH_BRIDGE_URL"}
    assert all(v == "" for k, v in root.items() if k not in defaults), root
    assert not list((repo_root / "executor").glob("*.env*"))
    for short in ("switch", "ledger"):
        assert os.path.exists(repo_root / "executor" / f"{short}.yaml"), short
    gitignore = (repo_root / ".gitignore").read_text()
    for line in ("/.env", "executor/*.db"):
        assert line in gitignore, line
    assert load_config(repo_root / "executor" / "ledger.yaml").executor == LEDGER_EXECUTOR
    assert load_config(repo_root / "executor" / "switch.yaml").peer_state_db == "ledger.db"
    assert load_config(repo_root / "executor" / "ledger.yaml").peer_state_db == "switch.db"


def test_process_env_hands_each_process_only_its_keys(tmp_path, monkeypatch):
    """One root .env (design v4 §5): each process gets SWITCH_BRIDGE_URL, its own prefixed
    identity and the keys named for it. The Gateway credential reaches sindarin-switch alone, the
    model key the three agents, and sindarin-ledger nothing but its identity."""
    from sindarin.executor.config import ensure_root_env, identity_values

    (tmp_path / ".env").write_text(
        "ANTHROPIC_API_KEY=sk-model\nMATTERMOST_ADMIN_PASSWORD=mm-secret\n"
        "SWITCH_BRIDGE_URL=http://localhost:8000\n"
        "SWITCH_GATEWAY_EMAIL=admin@x\nSWITCH_GATEWAY_PASSWORD=gw\n"
        "SWITCH_AGENT_API_KEY=never-unprefixed\n"
        "ANALYST_SWITCH_AGENT_ID=a-id\nANALYST_SWITCH_AGENT_API_KEY=a-key\n"
        "PLANNER_SWITCH_AGENT_ID=t-id\nPLANNER_SWITCH_AGENT_API_KEY=t-key\n"
        "SINDARIN_LEDGER_SWITCH_AGENT_ID=l-id\nSINDARIN_LEDGER_SWITCH_AGENT_API_KEY=l-key\n"
        "SINDARIN_SWITCH_SWITCH_AGENT_ID=sw-id\nSINDARIN_SWITCH_SWITCH_AGENT_API_KEY=sw-key\n"
    )
    analyst = process_env(tmp_path, "analyst")
    assert analyst == {
        "SWITCH_BRIDGE_URL": "http://localhost:8000",
        "ANTHROPIC_API_KEY": "sk-model",
        "SWITCH_AGENT_ID": "a-id",
        "SWITCH_AGENT_API_KEY": "a-key",
    }
    planner = process_env(tmp_path, "planner")
    assert planner["SWITCH_AGENT_ID"] == "t-id" and "MATTERMOST_ADMIN_PASSWORD" not in planner
    assert not any(k.startswith(("ANALYST_", "SINDARIN_")) for k in planner)
    ledger = process_env(tmp_path, "sindarin-ledger")
    assert ledger == {
        "SWITCH_BRIDGE_URL": "http://localhost:8000",
        "SWITCH_AGENT_ID": "l-id",
        "SWITCH_AGENT_API_KEY": "l-key",
    }
    switch = process_env(tmp_path, "sindarin-switch")
    assert switch["SWITCH_AGENT_API_KEY"] == "sw-key" and switch["SWITCH_GATEWAY_PASSWORD"] == "gw"
    assert "ANTHROPIC_API_KEY" not in switch and "SWITCH_GATEWAY_PASSWORD" not in ledger

    # the shell wins for executors' ENV_KEYS when asked; never otherwise
    monkeypatch.setenv("SWITCH_GATEWAY_PASSWORD", "shell-pw")
    over = process_env(tmp_path, "sindarin-switch", env_override=True)
    assert over["SWITCH_GATEWAY_PASSWORD"] == "shell-pw"
    assert process_env(tmp_path, "sindarin-switch")["SWITCH_GATEWAY_PASSWORD"] == "gw"
    assert process_env(tmp_path / "elsewhere", "sindarin-switch") == {}
    with pytest.raises(ValueError):
        process_env(tmp_path, "coder")

    (tmp_path / "fresh").mkdir()
    (tmp_path / "fresh" / ".env.example").write_text("# template\nANTHROPIC_API_KEY=\n")
    path = ensure_root_env(tmp_path / "fresh")
    assert path.read_text().startswith("# template") and (path.stat().st_mode & 0o777) == 0o600
    identity = {"SWITCH_AGENT_ID": "g", "SWITCH_AGENT_API_KEY": "k", "SWITCH_CONNECTION_ID": "c"}
    write_env_values(
        path, {"SWITCH_BRIDGE_URL": "http://b", **identity_values("sindarin-ledger", identity)}
    )
    assert "SINDARIN_LEDGER_SWITCH_AGENT_API_KEY=k" in path.read_text()
    assert process_env(tmp_path / "fresh", "sindarin-ledger") == {
        "SWITCH_BRIDGE_URL": "http://b",
        **identity,
    }


def test_mattermost_target_resolution(tmp_path):
    """Bundled server from the stack env by default; a server of your own once the root .env
    sets MATTERMOST_URL; the driver account rides along."""
    from sindarin.bootstrap import MattermostTarget

    stack = {
        "MATTERMOST_HOST_PORT": "8065",
        "MATTERMOST_ADMIN_USER": "admin",
        "MATTERMOST_ADMIN_PASSWORD": "pw",
        "MATTERMOST_TEAM_NAME": "switch",
        "MATTERMOST_USER": "user",
        "MATTERMOST_USER_PASSWORD": "upw",
    }
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk\nMATTERMOST_URL=\n")
    bundled = MattermostTarget.resolve(tmp_path, stack)
    assert bundled.url == "http://localhost:8065" and bundled.team_name == "switch"
    assert not bundled.own and bundled.user == "user" and bundled.user_password == "upw"

    (tmp_path / ".env").write_text(
        "MATTERMOST_URL=https://acme.cloud.mattermost.com/\n"
        "MATTERMOST_ADMIN_USER=ops\nMATTERMOST_ADMIN_PASSWORD=secret\n"
        "MATTERMOST_USER=kartik\nMATTERMOST_USER_PASSWORD=kpw\n"
        "MATTERMOST_TEAM_NAME=acme\nMATTERMOST_INTERNAL_URL=\n"
    )
    own = MattermostTarget.resolve(tmp_path, stack)
    assert own.own and own.url == "https://acme.cloud.mattermost.com" and own.user == "kartik"
    assert own.bridge_name == "Mattermost acme.cloud.mattermost.com"
    assert own.connection_config() == {
        "url": "https://acme.cloud.mattermost.com",
        "admin_user": "ops",
        "admin_password": "secret",
        "team_name": "acme",
    }
    local = MattermostTarget.from_values(
        parse_env_text(
            "MATTERMOST_URL=http://localhost:8066\n"
            "MATTERMOST_INTERNAL_URL=http://host.docker.internal:8066\n"
            "MATTERMOST_ADMIN_USER=a\nMATTERMOST_ADMIN_PASSWORD=b\nSWITCH_BRIDGE_NAME=Lab\n"
        )
    )
    assert local.connection_config()["url"] == "http://host.docker.internal:8066"
    assert local.connection_config()["public_url"] == "http://localhost:8066"
    (tmp_path / ".env").write_text("MATTERMOST_URL=https://x\nMATTERMOST_ADMIN_USER=a\n")
    with pytest.raises(ValueError, match="MATTERMOST_ADMIN_PASSWORD"):
        MattermostTarget.resolve(tmp_path, stack)


def test_ensure_bridge_connects_once_and_makes_it_default():
    from sindarin.bootstrap import MattermostTarget, ensure_bridge

    class Admin:
        def __init__(self):
            self.bridges = [
                {"bridge_id": "b-bundled", "display_name": "Mattermost", "is_default": True}
            ]
            self.created = []
            self.defaults = []

        def collaborations(self):
            return list(self.bridges)

        def default_bridge_id(self):
            return "b-bundled"

        def create_bridge(self, *, bridge_type, display_name, connection_config):
            assert bridge_type == "mattermost" and "admin_password" in connection_config
            self.created.append(display_name)
            b = {"bridge_id": f"b-{len(self.created)}", "display_name": display_name}
            self.bridges.append(b)
            return b

        def set_default_bridge(self, bridge_id):
            self.defaults.append(bridge_id)

    admin = Admin()
    bundled = MattermostTarget.from_stack(
        {"MATTERMOST_ADMIN_USER": "a", "MATTERMOST_ADMIN_PASSWORD": "b"}
    )
    assert ensure_bridge(admin, bundled) == ("b-bundled", False) and not admin.created
    own = MattermostTarget.from_values(
        {
            "MATTERMOST_URL": "https://mm.example.com",
            "MATTERMOST_ADMIN_USER": "a",
            "MATTERMOST_ADMIN_PASSWORD": "b",
            "MATTERMOST_TEAM_NAME": "t",
        }
    )
    assert ensure_bridge(admin, own) == ("b-1", True)
    assert admin.created == ["Mattermost mm.example.com"] and admin.defaults == ["b-1"]
    assert ensure_bridge(admin, own) == ("b-1", False)
    assert admin.defaults == ["b-1", "b-1"]
