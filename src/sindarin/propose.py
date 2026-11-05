"""The validators behind `sindarin propose spec|pr|release` and the agents' tools (DDD §6).

An agent never hand-writes a fence: it calls one of these with plain arguments and posts the
returned block verbatim. The executor re-validates everything; this layer exists so that a
model's output can only ever be a well-formed proposal or a one-line refusal.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from sindarin.names import EXECUTOR_OF_KIND
from sindarin.policy import Policy, check_target, load_policy
from sindarin.schema import MAX_JUSTIFICATION, PROPOSAL_SCHEMA, Kind, Proposal


def _one_line(exc: ValidationError) -> str:
    first = exc.errors()[0]
    loc = ".".join(str(x) for x in first.get("loc", ()) if x != "body") or "proposal"
    return f"{loc}: {first.get('msg', 'invalid')}"


def _rule_for(policy: Policy, kind: Kind, rule: str | None) -> str:
    if rule:
        r = policy.rule(rule)
        if r is None or r.kind is not kind:
            raise ValueError(f"unknown {kind.value} rule {rule!r}")
        return r.name
    rules = policy.rules_for(kind)
    if not rules:
        raise ValueError(f"policy.yaml has no {kind.value} rule")
    return rules[0].name


def _finish(policy: Policy, data: dict[str, Any]) -> Proposal:
    try:
        p = Proposal.model_validate(data)
    except ValidationError as exc:
        raise ValueError(_one_line(exc)) from None
    reason = check_target(policy, p)
    if reason is not None:
        raise ValueError(reason)
    if len(p.justification) > MAX_JUSTIFICATION:
        raise ValueError(f"justification is longer than {MAX_JUSTIFICATION} characters")
    return p


def build_spec(
    policy: Policy,
    *,
    title: str,
    press_release: str,
    faq: list[str],
    criteria: list[str],
    scope_in: list[str],
    scope_out: list[str],
    target_date: str | date,
    engineer: str,
    repo: str,
    slug: str,
    ask: str = "",
    justification: str,
    rule: str | None = None,
) -> Proposal:
    rule_name = _rule_for(policy, Kind.SPEC, rule)
    return _finish(
        policy,
        {
            "schema": PROPOSAL_SCHEMA,
            "kind": "spec",
            "platform": "switch",
            "action": "open_feature_room",
            "target": slug,
            "body": {
                "title": title,
                "press_release": press_release,
                "faq": list(faq),
                "criteria": list(criteria),
                "scope_in": list(scope_in),
                "scope_out": list(scope_out),
                "target_date": str(target_date),
                "engineer": engineer,
                "repo": repo,
            },
            "ask": ask,
            "rule": rule_name,
            "justification": justification[:MAX_JUSTIFICATION],
        },
    )


def build_pr(
    policy: Policy,
    *,
    number: int,
    title: str,
    branch: str,
    spec: str,
    repo: str,
    head_sha: str = "",
    ask: str = "",
    justification: str,
    rule: str | None = None,
) -> Proposal:
    rule_name = _rule_for(policy, Kind.PR, rule)
    return _finish(
        policy,
        {
            "schema": PROPOSAL_SCHEMA,
            "kind": "pr",
            "platform": "github",
            "action": "merge_pr",
            "target": f"{repo}#{number}",
            "snapshot_hint": head_sha or "",
            "body": {"number": number, "title": title, "branch": branch, "spec": spec},
            "ask": ask,
            "rule": rule_name,
            "justification": justification[:MAX_JUSTIFICATION],
        },
    )


def build_release(
    policy: Policy,
    *,
    tag: str,
    target_sha: str,
    notes: str,
    specs: list[str],
    asks: list[str],
    repo: str,
    justification: str,
    rule: str | None = None,
) -> Proposal:
    rule_name = _rule_for(policy, Kind.RELEASE, rule)
    return _finish(
        policy,
        {
            "schema": PROPOSAL_SCHEMA,
            "kind": "release",
            "platform": "github",
            "action": "create_release",
            "target": f"{repo}@{tag}",
            "snapshot_hint": target_sha,
            "body": {
                "tag": tag,
                "target_sha": target_sha,
                "notes": notes,
                "specs": list(specs),
                "asks": list(asks),
            },
            "ask": asks[0] if asks else "",
            "rule": rule_name,
            "justification": justification[:MAX_JUSTIFICATION],
        },
    )


def render(p: Proposal) -> str:
    """The exact block to post: the owning executor's mention, the word `proposal`, the fence."""
    return f"@{EXECUTOR_OF_KIND[p.kind.value]} proposal\n{p.render()}"


def policy_from(path: str | Path) -> Policy:
    try:
        return load_policy(path)
    except FileNotFoundError:
        raise ValueError(f"policy file not found: {path} (set SINDARIN_POLICY_PATH)") from None
