"""DDD §10.1: groups, proposers, executors, rooms, rules by kind; the P2/P3/P4 checks."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from validation.harness import (
    CODER_AGENT,
    FIXTURES,
    GITHUB_EXEC,
    ISAAC,
    LEONARDO,
    PRODUCT_AGENT,
    REPO,
    VINCENT,
    pr_proposal,
    release_proposal,
    spec_proposal,
)

from sindarin.policy import (
    Policy,
    check_facts,
    check_proposer,
    check_target,
    format_duration,
    load_policy,
    parse_duration,
)
from sindarin.schema import Kind


@pytest.fixture
def policy() -> Policy:
    return load_policy(FIXTURES / "policy.yaml")


@pytest.mark.parametrize(
    "text,seconds,shortest",
    [("7d", 604800, "7d"), ("24h", 86400, "1d"), ("10m", 600, "10m"), ("90m", 5400, "90m")],
)
def test_parse_duration(text, seconds, shortest):
    assert parse_duration(text) == seconds and format_duration(seconds) == shortest


@pytest.mark.parametrize("text", ["7", "7D", "1w", "0m", "-1d", "", "1.5h"])
def test_parse_duration_rejects(text):
    with pytest.raises(ValueError):
        parse_duration(text)


def test_fixture_policy_shape(policy):
    assert [r.name for r in policy.rules] == ["spec", "merge", "release"]
    assert [r.kind for r in policy.rules] == [Kind.SPEC, Kind.PR, Kind.RELEASE]
    assert policy.group_members("pm") == [LEONARDO]
    assert policy.in_group(ISAAC, "engineering") and not policy.in_group(VINCENT, "engineering")
    assert not policy.in_group(ISAAC.upper(), "engineering") and not policy.in_group("", "pm")
    assert policy.groups_of(ISAAC) == ["engineering"] and policy.groups_of(VINCENT) == []
    assert policy.proposer_id("coder") == CODER_AGENT and policy.role_of(CODER_AGENT) == "coder"
    assert policy.executor_id("sindarin-github") == GITHUB_EXEC
    assert policy.executor_of(GITHUB_EXEC) == "sindarin-github"
    assert GITHUB_EXEC in policy.agent_ids and PRODUCT_AGENT in policy.agent_ids
    assert ISAAC not in policy.agent_ids
    assert policy.room_key("!room-product:test") == "product" and policy.room_key("!x") is None
    assert policy.rules_for(Kind.PR)[0].room == "feature"
    assert policy.rule("merge").pending_ttl_seconds == 86400


def test_demo_policy_loads(repo_root):
    p = load_policy(repo_root / "agents" / "policy" / "policy.yaml")
    assert {r.kind for r in p.rules} == set(Kind)
    assert set(p.groups) == {"pm", "engineering", "release"}
    assert set(p.proposers) == {"product", "coder", "reviewer", "release"}
    assert set(p.executors) == {"sindarin-switch", "sindarin-github"}


def _base() -> dict:
    return {
        "version": 2,
        "groups": {"pm": ["@a:t"], "engineering": ["@b:t"], "release": ["@c:t"]},
        "proposers": {"product": "@p:t", "coder": "@q:t", "reviewer": "@r:t", "release": "@s:t"},
        "executors": {"sindarin-switch": "@x:t", "sindarin-github": "@y:t"},
        "rooms": {"product": "!p", "releases": "!r", "customer": "!c"},
        "rules": [],
    }


def _rule(**over) -> dict:
    base = {
        "name": "merge",
        "kind": "pr",
        "proposer": "coder",
        "approvers": "engineering",
        "room": "feature",
        "repo": "o/r",
    }
    base.update(over)
    return base


@pytest.mark.parametrize(
    "rule,fragment",
    [
        (_rule(approvers="ceo"), "approvers group 'ceo'"),
        (_rule(proposer="intern"), "proposer 'intern'"),
        (_rule(room="product"), "merges are approved in the feature room"),
        (_rule(repo=""), "needs repo owner/repo"),
        (
            _rule(kind="spec", name="spec", proposer="product", approvers="pm", room="product"),
            "needs allowed_repos",
        ),
        (
            _rule(
                kind="spec",
                name="spec",
                proposer="product",
                approvers="pm",
                room="feature",
                allowed_repos=["o/r"],
            ),
            "named room",
        ),
        (
            _rule(
                kind="release",
                name="rel",
                proposer="release",
                approvers="release",
                room="lounge",
                repo="o/r",
            ),
            "not a known room",
        ),
        (_rule(pending_ttl="1w"), "bad duration"),
        (
            _rule(
                kind="release",
                name="rel",
                proposer="release",
                approvers="release",
                room="releases",
                repo="o/r",
                tag_pattern="(",
            ),
            "tag_pattern",
        ),
        (_rule(merge_method="ff"), "merge_method"),
    ],
)
def test_rule_validation(rule, fragment):
    with pytest.raises(ValidationError) as exc:
        Policy.model_validate({**_base(), "rules": [rule]})
    assert fragment in str(exc.value)


def test_policy_level_validation():
    with pytest.raises(ValidationError, match="unique"):
        Policy.model_validate({**_base(), "rules": [_rule(), _rule()]})
    with pytest.raises(ValidationError, match="not a participant id"):
        Policy.model_validate({**_base(), "groups": {"pm": ["Leonardo"]}})
    with pytest.raises(ValidationError, match="unknown proposer role"):
        Policy.model_validate({**_base(), "proposers": {"ceo": "@a:t"}})
    with pytest.raises(ValidationError, match="unknown executor"):
        Policy.model_validate({**_base(), "executors": {"frodo": "@a:t"}})
    with pytest.raises(ValidationError, match="unknown room key"):
        Policy.model_validate({**_base(), "rooms": {"lounge": "!x"}})


def test_check_proposer(policy):
    assert check_proposer(policy, spec_proposal(), PRODUCT_AGENT) is None
    assert "only product may propose" in check_proposer(policy, spec_proposal(), CODER_AGENT)
    assert "only product may propose" in check_proposer(policy, spec_proposal(), ISAAC)
    assert "unknown rule" in check_proposer(policy, pr_proposal(rule="nope"), CODER_AGENT)
    bad_kind = pr_proposal(rule="spec")
    assert "is for spec proposals" in check_proposer(policy, bad_kind, CODER_AGENT)
    empty = Policy.model_validate({**_base(), "proposers": {"coder": ""}, "rules": [_rule()]})
    assert "only coder may propose" in check_proposer(empty, pr_proposal(repo="o/r"), "")


def test_check_target(policy):
    assert check_target(policy, spec_proposal()) is None
    assert "not allowed by rule spec" in check_target(policy, spec_proposal(repo="acme/other"))
    assert "engineer is not in group engineering" in check_target(
        policy, spec_proposal(engineer=VINCENT)
    )
    assert check_target(policy, pr_proposal()) is None
    assert "not allowed by rule merge" in check_target(policy, pr_proposal(repo="acme/other"))
    assert check_target(policy, release_proposal(target_sha="a" * 40)) is None
    assert "does not match" in check_target(policy, release_proposal(tag="v9", target_sha="a" * 40))
    assert REPO in check_target(policy, pr_proposal(repo="acme/other"))


def test_check_facts(policy):
    good = {"head_sha": "a" * 40, "base": "main", "state": "open", "merged": False, "draft": False}
    assert check_facts(policy, pr_proposal(), good) is None
    assert "not open" in check_facts(policy, pr_proposal(), {**good, "state": "closed"})
    assert "already merged" in check_facts(policy, pr_proposal(), {**good, "merged": True})
    assert "draft" in check_facts(policy, pr_proposal(), {**good, "draft": True})
    assert "merges into main" in check_facts(policy, pr_proposal(), {**good, "base": "develop"})
    assert "head sha" in check_facts(policy, pr_proposal(), {**good, "head_sha": ""})
    rel = release_proposal(target_sha="a" * 40)
    assert check_facts(policy, rel, {"commit_exists": True, "tag_exists": False}) is None
    assert "already exists" in check_facts(policy, rel, {"commit_exists": True, "tag_exists": True})
    assert "not a commit" in check_facts(policy, rel, {"commit_exists": False, "tag_exists": False})
    assert check_facts(policy, spec_proposal(), {}) is None


def test_no_secret_strings_in_agent_dirs(repo_root):
    """The agents' folders hold no GitHub credential and no key outside gitignored .env files."""
    import subprocess

    for path in (repo_root / "agents").rglob("*"):
        if not path.is_file() or ".switch" in path.parts or ".claude" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if path.name == ".env":
            # the coder's scoped GitHub token lives here by design (DDD §4.2); the file must be ignored
            ignored = subprocess.run(
                ["git", "check-ignore", "-q", str(path)], cwd=repo_root, check=False
            )
            assert ignored.returncode == 0, f"{path} must be gitignored"
        elif path.name != ".env.example":
            has_token = "ghp_" in text or "github_pat_" in text
            assert not has_token, path
            assert "SWITCH_AGENT_API_KEY" not in text and "ANTHROPIC_API_KEY" not in text, path
            assert "GITHUB_TOKEN" not in text, path
