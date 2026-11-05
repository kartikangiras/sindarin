from __future__ import annotations

import os
from pathlib import Path

from sindarin.executor.config import (
    Env,
    ExecutorConfig,
    find_executor_dir,
    load_config,
    load_env,
    load_executor,
    parse_env_text,
    render_config,
    save_config,
    write_env_values,
)
from sindarin.names import GITHUB_EXECUTOR, SWITCH_EXECUTOR


def test_parse_env_text():
    text = """
# comment
SWITCH_BRIDGE_URL=http://localhost:8080   # trailing comment
SWITCH_AGENT_ID="agent-1"
export GITHUB_TOKEN='github_pat_TEST'
EMPTY=
BAD LINE
"""
    assert parse_env_text(text) == {
        "SWITCH_BRIDGE_URL": "http://localhost:8080",
        "SWITCH_AGENT_ID": "agent-1",
        "GITHUB_TOKEN": "github_pat_TEST",
        "EMPTY": "",
    }


def test_write_env_values_preserves_lines_and_mode(tmp_path):
    p = tmp_path / "x.env"
    p.write_text("# keep me\nSWITCH_BRIDGE_URL=http://old\nGITHUB_TOKEN=\n")
    write_env_values(p, {"SWITCH_BRIDGE_URL": "http://new", "SWITCH_AGENT_ID": "a1"})
    assert (
        p.read_text()
        == "# keep me\nSWITCH_BRIDGE_URL=http://new\nGITHUB_TOKEN=\nSWITCH_AGENT_ID=a1\n"
    )
    assert oct(p.stat().st_mode & 0o777) == "0o600"
    q = tmp_path / "new.env"
    write_env_values(q, {"SWITCH_AGENT_API_KEY": "k"})
    assert oct(q.stat().st_mode & 0o777) == "0o600" and q.read_text() == "SWITCH_AGENT_API_KEY=k\n"


def test_load_env_process_env_overrides(tmp_path, monkeypatch):
    p = tmp_path / "github.env"
    p.write_text("GITHUB_TOKEN=file-token\n")
    monkeypatch.setenv("GITHUB_TOKEN", "env-token")
    env = Env.from_values(load_env(p))
    assert env.github_token == "env-token"
    assert Env.from_values(load_env(p, env_override=False)).github_token == "file-token"
    assert env.missing("SWITCH_AGENT_ID", "GITHUB_TOKEN") == ["SWITCH_AGENT_ID"]
    assert Env.from_values({"SWITCH_BRIDGE_URL": "http://x/"}).gateway_url == "http://x"


def test_config_round_trip_and_load_executor(tmp_path):
    cfg = ExecutorConfig(
        executor=GITHUB_EXECUTOR,
        self_id="@switch-agent-7:s",
        peer_ids=["@switch-agent-8:s"],
        github_mode="simulated",
    )
    save_config(tmp_path / "github.yaml", cfg)
    again = load_config(tmp_path / "github.yaml")
    assert again == cfg and again.missing() == [] and again.short == "github"
    assert "Secrets live in github.env" in render_config(cfg)
    assert again.db_path(tmp_path) == tmp_path / "github.db"
    assert load_config(tmp_path / "missing.yaml").missing() == ["self_id"]
    (tmp_path / "switch.env").write_text("SWITCH_GATEWAY_EMAIL=a@b\n")
    env, scfg = load_executor(tmp_path, "switch")
    assert scfg.executor == SWITCH_EXECUTOR and env.gateway_email == "a@b"
    assert cfg.resolve(tmp_path, "/abs/policy.yaml") == Path("/abs/policy.yaml")
    assert (
        cfg.resolve(tmp_path, "../agents/policy/policy.yaml")
        == (tmp_path / "../agents/policy/policy.yaml").resolve()
    )


def test_find_executor_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("SINDARIN_EXECUTOR_DIR", raising=False)
    assert find_executor_dir(tmp_path / "x") == (tmp_path / "x").resolve()
    monkeypatch.setenv("SINDARIN_EXECUTOR_DIR", str(tmp_path / "y"))
    assert find_executor_dir() == (tmp_path / "y").resolve()
    monkeypatch.delenv("SINDARIN_EXECUTOR_DIR")
    (tmp_path / "proj" / "executor").mkdir(parents=True)
    (tmp_path / "proj" / "executor" / "switch.yaml").write_text("")
    (tmp_path / "proj" / "sub").mkdir()
    monkeypatch.chdir(tmp_path / "proj" / "sub")
    assert find_executor_dir() == (tmp_path / "proj" / "executor").resolve()


def test_committed_examples_and_gitignore(repo_root):
    for short in ("switch", "github"):
        values = parse_env_text((repo_root / "executor" / f"{short}.env.example").read_text())
        assert values["SWITCH_AGENT_API_KEY"] == ""
        assert (
            values.get("GITHUB_TOKEN", "") == "" and values.get("SWITCH_GATEWAY_PASSWORD", "") == ""
        )
        assert os.path.exists(repo_root / "executor" / f"{short}.yaml")
    gitignore = (repo_root / ".gitignore").read_text()
    for line in (
        "executor/*.env",
        "agents/*/.env",
        "executor/*.db",
        "executor/simulated-github.json",
    ):
        assert line in gitignore, line
    assert load_config(repo_root / "executor" / "github.yaml").executor == GITHUB_EXECUTOR
    assert load_config(repo_root / "executor" / "switch.yaml").peer_state_db == "github.db"
