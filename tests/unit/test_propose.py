"""`sindarin propose` validators (DDD §6): the agents' one tool, as plain functions."""

from __future__ import annotations

import pytest
from validation.harness import FIXTURES, ISAAC, REPO, VINCENT

from sindarin.policy import load_policy
from sindarin.propose import build_pr, build_release, build_spec, render


@pytest.fixture
def policy():
    return load_policy(FIXTURES / "policy.yaml")


SPEC = dict(
    title="CSV export",
    press_release="Acme can export orders as CSV.",
    faq=["a", "b", "c"],
    criteria=["1", "2", "3"],
    scope_in=[],
    scope_out=[],
    target_date="2026-11-07",
    engineer=ISAAC,
    repo=REPO,
    slug="feat-csv-export",
    ask="m1",
    justification="asked",
)


def test_build_spec_and_render(policy):
    p = build_spec(policy, **SPEC)
    assert p.rule == "spec" and render(p).startswith(
        "@sindarin-switch proposal\n```sindarin-proposal\n"
    )
    with pytest.raises(ValueError, match="not allowed by rule spec"):
        build_spec(policy, **{**SPEC, "repo": "acme/other"})
    with pytest.raises(ValueError, match="engineer is not in group"):
        build_spec(policy, **{**SPEC, "engineer": VINCENT})
    with pytest.raises(ValueError, match="faq"):
        build_spec(policy, **{**SPEC, "faq": ["only one"]})
    with pytest.raises(ValueError, match="target_date"):
        build_spec(policy, **{**SPEC, "target_date": "soon"})
    with pytest.raises(ValueError, match="unknown spec rule"):
        build_spec(policy, **{**SPEC, "rule": "merge"})
    long = build_spec(policy, **{**SPEC, "justification": "x" * 400})
    assert len(long.justification) == 280


def test_build_pr_and_release(policy):
    p = build_pr(
        policy,
        number=41,
        title="t",
        branch="feat/SPEC-1",
        spec="SPEC-1",
        repo=REPO,
        head_sha="a" * 40,
        justification="ok",
    )
    assert p.target == f"{REPO}#41" and p.snapshot_hint == "a" * 40 and p.rule == "merge"
    assert render(p).startswith("@sindarin-github proposal\n")
    with pytest.raises(ValueError, match="not allowed by rule merge"):
        build_pr(
            policy,
            number=41,
            title="t",
            branch="b",
            spec="SPEC-1",
            repo="acme/other",
            justification="x",
        )
    with pytest.raises(ValueError, match="spec"):
        build_pr(
            policy, number=41, title="t", branch="b", spec="PR-1", repo=REPO, justification="x"
        )
    r = build_release(
        policy,
        tag="v1.4.0",
        target_sha="b" * 40,
        notes="n",
        specs=["SPEC-1"],
        asks=["m1"],
        repo=REPO,
        justification="go",
    )
    assert r.target == f"{REPO}@v1.4.0" and r.ask == "m1"
    with pytest.raises(ValueError, match="does not match"):
        build_release(
            policy,
            tag="1.4",
            target_sha="b" * 40,
            notes="n",
            specs=["SPEC-1"],
            asks=[],
            repo=REPO,
            justification="go",
        )
    with pytest.raises(ValueError, match="target_sha"):
        build_release(
            policy,
            tag="v1.4.0",
            target_sha="short",
            notes="n",
            specs=["SPEC-1"],
            asks=[],
            repo=REPO,
            justification="go",
        )
