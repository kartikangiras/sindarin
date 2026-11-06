"""The validators behind the agents' tools (design v5 §4).

An agent never hand-writes a fence: it calls one of these with plain arguments and posts the
returned block verbatim. The executor re-validates everything; this layer exists so that a
model's output can only ever be a well-formed proposal or a one-line refusal.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import ValidationError

from sindarin.names import EXECUTOR_OF_KIND
from sindarin.policy import Policy, check_target, load_policy
from sindarin.schema import (
    MAX_JUSTIFICATION,
    PROPOSAL_SCHEMA,
    Kind,
    Proposal,
    budget_target,
    close_target,
    deal_target,
    entries_target,
    goals_target,
    lock_target,
    pay_target,
)


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


def build_goals(
    policy: Policy,
    *,
    period: str,
    basis: str,
    title: str,
    summary: str,
    report: list[str],
    revenue_target_cents: int,
    margin_target_bps: int,
    cost_cap_cents: int,
    currency: str = "USD",
    ask: str = "",
    justification: str,
    rule: str | None = None,
) -> Proposal:
    rule_name = _rule_for(policy, Kind.GOALS, rule)
    return _finish(
        policy,
        {
            "schema": PROPOSAL_SCHEMA,
            "kind": "goals",
            "platform": "switch",
            "action": "open_plan_room",
            "target": goals_target(period),
            "body": {
                "period": period,
                "basis": basis,
                "title": title,
                "summary": summary,
                "report": list(report),
                "revenue_target_cents": int(revenue_target_cents),
                "margin_target_bps": int(margin_target_bps),
                "cost_cap_cents": int(cost_cap_cents),
                "currency": currency,
            },
            "ask": ask,
            "rule": rule_name,
            "justification": justification[:MAX_JUSTIFICATION],
        },
    )


def build_budget(
    policy: Policy,
    *,
    period: str,
    goals: str,
    lines: list[dict[str, Any]],
    currency: str = "USD",
    digest: str = "",
    ask: str = "",
    justification: str,
    rule: str | None = None,
) -> Proposal:
    rule_name = _rule_for(policy, Kind.BUDGET, rule)
    total = sum(int(x.get("amount_cents", 0)) for x in lines)
    return _finish(
        policy,
        {
            "schema": PROPOSAL_SCHEMA,
            "kind": "budget",
            "platform": "ledger",
            "action": "record_budget",
            "target": budget_target(period),
            "snapshot_hint": digest or "",
            "body": {
                "period": period,
                "goals": goals,
                "lines": list(lines),
                "total_cents": total,
                "currency": currency,
            },
            "ask": ask,
            "rule": rule_name,
            "justification": justification[:MAX_JUSTIFICATION],
        },
    )


def build_entries(
    policy: Policy,
    *,
    period: str,
    goals: str,
    budget: str,
    lines: list[dict[str, Any]],
    memo: str,
    digest: str = "",
    ask: str = "",
    justification: str,
    rule: str | None = None,
) -> Proposal:
    rule_name = _rule_for(policy, Kind.ENTRIES, rule)
    return _finish(
        policy,
        {
            "schema": PROPOSAL_SCHEMA,
            "kind": "entries",
            "platform": "ledger",
            "action": "post_budget_entries",
            "target": entries_target(period),
            "snapshot_hint": digest or "",
            "body": {
                "period": period,
                "goals": goals,
                "budget": budget,
                "lines": list(lines),
                "memo": memo,
            },
            "ask": ask,
            "rule": rule_name,
            "justification": justification[:MAX_JUSTIFICATION],
        },
    )


def build_close(
    policy: Policy,
    *,
    period: str,
    title: str,
    summary: str,
    report: list[str],
    entries: list[dict[str, Any]],
    payables: list[str],
    total_payables_cents: int,
    currency: str = "USD",
    ask: str = "",
    justification: str,
    rule: str | None = None,
) -> Proposal:
    rule_name = _rule_for(policy, Kind.CLOSE, rule)
    return _finish(
        policy,
        {
            "schema": PROPOSAL_SCHEMA,
            "kind": "close",
            "platform": "switch",
            "action": "open_period_room",
            "target": close_target(period),
            "body": {
                "period": period,
                "title": title,
                "summary": summary,
                "report": list(report),
                "entries": list(entries),
                "payables": list(payables),
                "total_payables_cents": int(total_payables_cents),
                "currency": currency,
            },
            "ask": ask,
            "rule": rule_name,
            "justification": justification[:MAX_JUSTIFICATION],
        },
    )


def build_pay(
    policy: Policy,
    *,
    period: str,
    plan: str,
    run: list[dict[str, Any]],
    currency: str = "USD",
    digest: str = "",
    ask: str = "",
    justification: str,
    rule: str | None = None,
) -> Proposal:
    rule_name = _rule_for(policy, Kind.PAY, rule)
    total = sum(int(x.get("amount_cents", 0)) for x in run)
    return _finish(
        policy,
        {
            "schema": PROPOSAL_SCHEMA,
            "kind": "pay",
            "platform": "ledger",
            "action": "pay_run",
            "target": pay_target(period),
            "snapshot_hint": digest or "",
            "body": {
                "period": period,
                "plan": plan,
                "run": list(run),
                "total_cents": total,
                "currency": currency,
            },
            "ask": ask,
            "rule": rule_name,
            "justification": justification[:MAX_JUSTIFICATION],
        },
    )


def build_lock(
    policy: Policy,
    *,
    period: str,
    plan: str,
    pay_run: str = "",
    entries: list[dict[str, Any]],
    memo: str,
    digest: str = "",
    ask: str = "",
    justification: str,
    rule: str | None = None,
) -> Proposal:
    rule_name = _rule_for(policy, Kind.LOCK, rule)
    return _finish(
        policy,
        {
            "schema": PROPOSAL_SCHEMA,
            "kind": "lock",
            "platform": "ledger",
            "action": "lock_period",
            "target": lock_target(period),
            "snapshot_hint": digest or "",
            "body": {
                "period": period,
                "plan": plan,
                "pay_run": pay_run,
                "entries": list(entries),
                "memo": memo,
            },
            "ask": ask,
            "rule": rule_name,
            "justification": justification[:MAX_JUSTIFICATION],
        },
    )


def build_deal(
    policy: Policy,
    *,
    period: str,
    deal_id: str,
    account: str,
    product: str,
    list_cents: int,
    price_cents: int,
    terms: str = "standard",
    currency: str = "USD",
    digest: str = "",
    ask: str = "",
    justification: str,
    rule: str | None = None,
) -> Proposal:
    rule_name = _rule_for(policy, Kind.DEAL, rule)
    bps = round((int(list_cents) - int(price_cents)) * 10000 / int(list_cents)) if list_cents else 0
    return _finish(
        policy,
        {
            "schema": PROPOSAL_SCHEMA,
            "kind": "deal",
            "platform": "ledger",
            "action": "record_deal",
            "target": deal_target(deal_id),
            "snapshot_hint": digest or "",
            "body": {
                "period": period,
                "deal_id": deal_id,
                "account": account,
                "product": product,
                "list_cents": int(list_cents),
                "price_cents": int(price_cents),
                "discount_bps": int(bps),
                "terms": terms,
                "currency": currency,
            },
            "ask": ask,
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
