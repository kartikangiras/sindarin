"""Committed artifacts stay consistent and secret-free (DDD §16.2, §21)."""

from __future__ import annotations

import re
import subprocess

import yaml


def test_template_instructions_match_agent_md(repo_root):
    data = yaml.safe_load((repo_root / "templates" / "sindarin.template.yaml").read_text())
    assert data["version"] == 1
    by_name = {a["name"]: a for a in data["agents"]}
    assert set(by_name) == {"analyst", "planner", "bookkeeper"}
    for role, agent in by_name.items():
        agent_md = (repo_root / "agents" / role / "AGENT.md").read_text().rstrip("\n")
        assert agent["instructions"].rstrip("\n") == agent_md, role
    names = [r["name"] for r in data["rooms"]]
    assert names == ["{team}-acme-finance", "{team}-planning"]
    assert data["params"]["switch_executor"]["type"] == "agent"
    assert data["params"]["ledger_executor"]["type"] == "agent"


def test_scripts_exist_and_are_executable(repo_root):
    for name in ("demo.sh", "dev_stack.sh", "reset_demo.sh", "switch_stack.sh"):
        path = repo_root / "scripts" / name
        assert path.exists() and path.stat().st_mode & 0o111, name
        assert path.read_text().startswith("#!/usr/bin/env bash")
    assert (repo_root / "scripts" / "spike.py").exists()
    assert (
        'PROFILES="${PROFILES:-gateway collab}"'
        in (repo_root / "scripts" / "switch_stack.sh").read_text()
    )
    assert (
        "echo 'FEATURE_FLAGS_ENABLED=agent_chats'"
        not in (repo_root / "scripts" / "switch_stack.sh").read_text()
    )


def _would_commit(repo_root) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.split()


def test_committed_tree_has_no_secrets(repo_root):
    files = _would_commit(repo_root)
    assert files, "git sees no files"
    for rel in files:
        path = repo_root / rel
        if not path.exists() or path.suffix in {".png", ".gif", ".jpg", ".lock"}:
            continue
        assert not rel.endswith(".env"), f"{rel} would be committed"
        assert not rel.endswith((".db", ".db-wal", ".db-shm", ".log")), rel
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pat in ("ghp_", "github_pat_", "sk-ant-"):
            for m in re.finditer(pat + r"[A-Za-z0-9_-]{8,}", text):
                assert "FAKE" in m.group(0) or "TEST" in m.group(0), (rel, m.group(0)[:20])
        assert not re.search(r"switch_auth=[A-Za-z0-9._-]{16,}", text), rel


def test_policy_and_users_hold_only_participant_ids(repo_root):
    """Placeholders before bootstrap, opaque participant ids after; never a name or a secret."""
    policy = (repo_root / "agents" / "policy" / "policy.yaml").read_text()
    users = (repo_root / "agents" / "policy" / "users.yaml").read_text()
    for text in (policy, users):
        ids = re.findall(r'"(@switch-[^"]+)"', text)
        assert ids
        for pid in ids:
            assert re.fullmatch(r"@switch-[a-z]+-[A-Za-z0-9._-]+:localhost", pid), pid
        for pat in ("ghp_", "github_pat_", "sk-ant-", "API_KEY", "PASSWORD"):
            assert pat not in text, pat
