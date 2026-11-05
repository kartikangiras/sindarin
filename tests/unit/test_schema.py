"""DDD §8: proposal v2 per kind, review v1, snapshot hashing, fences, artifacts."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from validation.harness import ISAAC, REPO, pr_proposal, release_proposal, spec_proposal

from sindarin.schema import (
    PROPOSAL_FENCE,
    REVIEW_SCHEMA,
    Artifact,
    Kind,
    Proposal,
    Review,
    Snapshot,
    State,
    StatusFence,
    canonical_json,
    extract_fences,
    kind_of_id,
    make_snapshot,
    render_fence,
    sha256_hex,
)


def test_canonical_json_is_stable_under_key_order():
    a = {"b": 1, "a": {"y": 2, "x": 1}}
    b = {"a": {"x": 1, "y": 2}, "b": 1}
    assert canonical_json(a) == canonical_json(b) == '{"a":{"x":1,"y":2},"b":1}'
    assert canonical_json({"k": "é"}) == '{"k":"é"}'


def test_kind_of_id():
    assert kind_of_id("SPEC-3") is Kind.SPEC and kind_of_id("PR-41") is Kind.PR
    assert kind_of_id("REL-1") is Kind.RELEASE and kind_of_id("REQ-1") is None


def test_spec_proposal_round_trip_and_canonical():
    p = spec_proposal()
    again = Proposal.model_validate(json.loads(p.canonical()))
    assert again == p and again.canonical() == p.canonical()
    assert p.repo == REPO and p.kind is Kind.SPEC
    shuffled = dict(reversed(list(p.to_dict().items())))
    assert Proposal.model_validate(shuffled).canonical() == p.canonical()


def test_pr_and_release_targets_carry_repo():
    pr = pr_proposal(number=41)
    assert pr.repo == REPO and pr.target == f"{REPO}#41"
    rel = release_proposal(target_sha="a" * 40)
    assert rel.repo == REPO and rel.target == f"{REPO}@v1.4.0"


@pytest.mark.parametrize(
    "mutate,fragment",
    [
        (lambda d: d.update(platform="github"), "platform switch"),
        (lambda d: d.update(action="merge_pr"), "takes action open_feature_room"),
        (lambda d: d.update(target="Feat Room"), "room slug"),
        (lambda d: d.update(snapshot_hint="a" * 40), "no snapshot_hint"),
        (lambda d: d["body"].update(faq=["one", "two"]), "faq"),
        (lambda d: d["body"].update(criteria=["a", "b"]), "criteria"),
        (lambda d: d["body"].update(target_date="soon"), "target_date"),
        (lambda d: d["body"].update(repo="not a repo"), "repo"),
        (lambda d: d["body"].update(extra=1), "extra"),
        (lambda d: d.update(approved=True), "approved"),
        (lambda d: d.update(schema="sindarin/proposal/v1"), "schema"),
        (lambda d: d.update(justification="x" * 281), "justification"),
    ],
)
def test_spec_proposal_rejections(mutate, fragment):
    data = spec_proposal().to_dict()
    mutate(data)
    with pytest.raises(ValidationError) as exc:
        Proposal.model_validate(data)
    assert fragment in str(exc.value)


@pytest.mark.parametrize(
    "mutate,fragment",
    [
        (lambda d: d.update(target=f"{REPO}#42"), "differs from body.number"),
        (lambda d: d.update(target=REPO), "owner/repo#number"),
        (lambda d: d.update(snapshot_hint="abc"), "40-hex"),
        (lambda d: d["body"].update(number="41"), "number"),
        (lambda d: d["body"].update(spec="PR-1"), "spec"),
        (lambda d: d.update(body=spec_proposal().to_dict()["body"]), "required"),
    ],
)
def test_pr_proposal_rejections(mutate, fragment):
    data = pr_proposal().to_dict()
    mutate(data)
    with pytest.raises(ValidationError) as exc:
        Proposal.model_validate(data)
    assert fragment in str(exc.value)


@pytest.mark.parametrize(
    "mutate,fragment",
    [
        (lambda d: d.update(target=f"{REPO}@v9"), "differs from body.tag"),
        (lambda d: d["body"].update(target_sha="short"), "target_sha"),
        (lambda d: d["body"].update(specs=[]), "specs"),
        (lambda d: d["body"].update(specs=["PR-41"]), "bad spec id"),
        (lambda d: d["body"].update(notes="n" * 2001), "notes"),
    ],
)
def test_release_proposal_rejections(mutate, fragment):
    data = release_proposal(target_sha="b" * 40).to_dict()
    mutate(data)
    with pytest.raises(ValidationError) as exc:
        Proposal.model_validate(data)
    assert fragment in str(exc.value)


def test_review_counts_and_rejects_extras():
    r = Review(
        schema=REVIEW_SCHEMA,
        pr="PR-41",
        head_sha="a" * 40,
        results=[
            {"criterion": 1, "met": True, "evidence": "t1"},
            {"criterion": 2, "met": False, "evidence": "missing"},
        ],
        summary="1 of 2 met",
    )
    assert (r.met, r.total) == (1, 2)
    assert r.render().startswith("```sindarin-review\n")
    with pytest.raises(ValidationError):
        Review.model_validate({**r.to_dict(), "approved": True})
    with pytest.raises(ValidationError):
        Review.model_validate({**r.to_dict(), "pr": "SPEC-1"})


def test_snapshot_hash_binds_proposal_and_facts():
    p = pr_proposal()
    s1 = make_snapshot("PR-41", "@a:t", p, {"head_sha": "a" * 40, "base": "main"})
    s2 = make_snapshot("PR-41", "@a:t", p, {"head_sha": "b" * 40, "base": "main"})
    s3 = make_snapshot("PR-41", "@b:t", p, {"head_sha": "a" * 40, "base": "main"})
    s4 = make_snapshot(
        "PR-41", "@a:t", pr_proposal(title="other"), {"head_sha": "a" * 40, "base": "main"}
    )
    assert len({s1.hash, s2.hash, s3.hash, s4.hash}) == 4
    assert s1.hash == sha256_hex(s1.canonical())
    assert Snapshot.model_validate_json(s1.canonical()) == s1
    with pytest.raises(ValidationError):
        Snapshot.model_validate({**s1.to_dict(), "approved": True})


def test_fences():
    text = "hello\n" + render_fence(PROPOSAL_FENCE, {"a": 1}) + "\nbye"
    assert extract_fences(text, PROPOSAL_FENCE) == ['{"a":1}']
    assert extract_fences(text, "sindarin-review") == []
    assert extract_fences(
        "```sindarin-proposal\n{}\n```\n```sindarin-proposal\n{}\n```", PROPOSAL_FENCE
    ) == ["{}", "{}"]


def _artifact(**over) -> Artifact:
    now = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
    p = pr_proposal()
    snap = make_snapshot("PR-41", "@c:t", p, {"head_sha": "a" * 40})
    base = dict(
        id="PR-41",
        kind=Kind.PR,
        rule="merge",
        room_id="!r:t",
        thread_id="m1",
        proposal_message_id="m1",
        proposer_id="@c:t",
        proposer_name="coder",
        proposal_json=p.model_dump_json(by_alias=True),
        snapshot_json=snap.canonical(),
        snapshot_sha=snap.hash,
        created_at=now,
        expires_at=now,
    )
    base.update(over)
    return Artifact.model_validate(base)


def test_artifact_accessors_and_status_fence():
    a = _artifact()
    assert a.proposal().body.number == 41 and a.facts()["head_sha"] == "a" * 40
    assert a.target == f"{REPO}#41" and a.result() == {}
    f = StatusFence.from_artifact(a, datetime(2026, 11, 5, 12, 0, tzinfo=UTC), State.MERGED)
    assert f.state == "merged" and f.id == "PR-41" and "@switch" not in f.render()
    with pytest.raises(ValidationError):
        _artifact(id="REQ-1")
    with pytest.raises(ValidationError):
        Artifact.model_validate({**_artifact().model_dump(), "token": "x"})
    assert ISAAC.startswith("@switch-user-")
