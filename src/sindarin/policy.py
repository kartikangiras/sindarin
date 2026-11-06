"""policy.yaml: groups, proposers, rooms and rules by kind (design v5 §6); the P2/P3/P4 checks."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sindarin.names import AGENT_ROLES, EXECUTORS
from sindarin.schema import CURRENCIES, RULE_PATTERN, Kind, Proposal

DURATION_RE = re.compile(r"^(\d+)(m|h|d)$")
_UNITS = {"m": 60, "h": 3600, "d": 86400}
ROOM_KEYS = ("finance", "planning", "payments", "sales")
PERIOD_ROOM = "period"  # the artifact's own room: the plan room the goals opened


def parse_duration(text: str) -> int:
    """`^\\d+(m|h|d)$` → seconds. Raises ValueError on anything else."""
    m = DURATION_RE.match(text.strip()) if isinstance(text, str) else None
    if not m:
        raise ValueError(f"bad duration {text!r}: expected <N>m, <N>h or <N>d")
    n, unit = int(m.group(1)), m.group(2)
    if n <= 0:
        raise ValueError(f"bad duration {text!r}: must be positive")
    return n * _UNITS[unit]


def format_duration(seconds: int) -> str:
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=RULE_PATTERN)
    kind: Kind
    proposer: str = Field(min_length=1)  # a role in `proposers`
    approvers: str = Field(min_length=1)  # a group in `groups`
    room: str = Field(min_length=1)  # a key in `rooms`, or `period`
    pending_ttl: str = "24h"
    currency: str = "USD"
    # goals: a second group invited into the plan room (the controllers sit with the cfo)
    also_invite: str = ""
    # budget: below the limit a second group may approve (the cfo alone for small budgets)
    limit_cents: int | None = None
    approvers_under_limit: str = ""

    @field_validator("pending_ttl")
    @classmethod
    def _ttl(cls, v: str) -> str:
        parse_duration(v)
        return v

    @field_validator("currency")
    @classmethod
    def _currency(cls, v: str) -> str:
        if v not in CURRENCIES:
            raise ValueError(f"currency must be one of {', '.join(CURRENCIES)}")
        return v

    @model_validator(mode="after")
    def _fits_kind(self) -> Rule:
        in_room = self.kind in (Kind.ENTRIES, Kind.LOCK)
        if not in_room and self.room == PERIOD_ROOM:
            raise ValueError(f"rule {self.name}: {self.kind.value} is approved in a named room")
        if in_room and self.room != PERIOD_ROOM:
            raise ValueError(
                f"rule {self.name}: {self.kind.value} is approved in the room the chain opened"
            )
        if (self.limit_cents is None) != (not self.approvers_under_limit):
            raise ValueError(f"rule {self.name}: limit_cents and approvers_under_limit go together")
        if self.limit_cents is not None and self.kind not in (Kind.BUDGET, Kind.PAY, Kind.DEAL):
            raise ValueError(f"rule {self.name}: only a budget, pay or deal rule has a limit")
        return self

    @property
    def pending_ttl_seconds(self) -> int:
        return parse_duration(self.pending_ttl)

    def approver_groups(self, total_cents: int | None) -> list[str]:
        """The groups whose members may approve, given the amount the executor read."""
        groups = [self.approvers]
        if (
            self.limit_cents is not None
            and total_cents is not None
            and total_cents <= self.limit_cents
            and self.approvers_under_limit
        ):
            groups.append(self.approvers_under_limit)
        return groups


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = 3
    groups: dict[str, list[str]] = Field(default_factory=dict)
    proposers: dict[str, str] = Field(default_factory=dict)
    executors: dict[str, str] = Field(default_factory=dict)
    rooms: dict[str, str] = Field(default_factory=dict)
    rules: list[Rule]

    @field_validator("groups")
    @classmethod
    def _group_ids(cls, v: dict[str, list[str]]) -> dict[str, list[str]]:
        for name, ids in v.items():
            if not re.match(r"^[a-z][a-z0-9_-]{0,31}$", name):
                raise ValueError(f"bad group name {name!r}")
            for i in ids:
                if not isinstance(i, str) or not i.startswith("@"):
                    raise ValueError(f"group {name}: {i!r} is not a participant id")
        return v

    @field_validator("proposers")
    @classmethod
    def _proposer_roles(cls, v: dict[str, str]) -> dict[str, str]:
        for role, pid in v.items():
            if role not in AGENT_ROLES:
                raise ValueError(f"unknown proposer role {role!r}")
            if pid and not str(pid).startswith("@"):
                raise ValueError(f"proposer {role}: {pid!r} is not a participant id")
        return v

    @field_validator("executors")
    @classmethod
    def _executor_handles(cls, v: dict[str, str]) -> dict[str, str]:
        for handle, pid in v.items():
            if handle not in EXECUTORS:
                raise ValueError(f"unknown executor {handle!r}; known: {', '.join(EXECUTORS)}")
            if pid and not str(pid).startswith("@"):
                raise ValueError(f"executor {handle}: {pid!r} is not a participant id")
        return v

    @field_validator("rooms")
    @classmethod
    def _room_keys(cls, v: dict[str, str]) -> dict[str, str]:
        for k in v:
            if k not in ROOM_KEYS:
                raise ValueError(f"unknown room key {k!r}; known: {', '.join(ROOM_KEYS)}")
        return v

    @model_validator(mode="after")
    def _rules_resolve(self) -> Policy:
        names = [r.name for r in self.rules]
        if len(names) != len(set(names)):
            raise ValueError("rule names must be unique")
        for r in self.rules:
            if r.approvers not in self.groups:
                raise ValueError(f"rule {r.name}: approvers group {r.approvers!r} is not defined")
            if r.proposer not in self.proposers:
                raise ValueError(f"rule {r.name}: proposer {r.proposer!r} is not defined")
            if r.room != PERIOD_ROOM and r.room not in ROOM_KEYS:
                raise ValueError(f"rule {r.name}: room {r.room!r} is not a known room")
            for extra in (r.also_invite, r.approvers_under_limit):
                if extra and extra not in self.groups:
                    raise ValueError(f"rule {r.name}: group {extra!r} is not defined")
        return self

    # -- lookups (ids only, never names) ----------------------------------------------------

    def rule(self, name: str) -> Rule | None:
        return next((r for r in self.rules if r.name == name), None)

    def rules_for(self, kind: Kind) -> list[Rule]:
        return [r for r in self.rules if r.kind is kind]

    def group_members(self, group: str) -> list[str]:
        return list(self.groups.get(group, []))

    def in_group(self, participant_id: str, group: str) -> bool:
        """Exact match on the opaque participant id."""
        return bool(participant_id) and participant_id in self.groups.get(group, [])

    def groups_of(self, participant_id: str) -> list[str]:
        return [g for g, ids in self.groups.items() if participant_id and participant_id in ids]

    def proposer_id(self, role: str) -> str:
        return self.proposers.get(role, "")

    def role_of(self, participant_id: str) -> str | None:
        return next((r for r, pid in self.proposers.items() if pid and pid == participant_id), None)

    @property
    def agent_ids(self) -> frozenset[str]:
        """Every non-human participant: the three agents and the two executors (A3)."""
        return frozenset(pid for pid in (*self.proposers.values(), *self.executors.values()) if pid)

    def executor_id(self, handle: str) -> str:
        return self.executors.get(handle, "")

    def executor_of(self, participant_id: str) -> str | None:
        return next((h for h, pid in self.executors.items() if pid and pid == participant_id), None)

    def room_id(self, key: str) -> str:
        return self.rooms.get(key, "")

    def room_key(self, room_id: str) -> str | None:
        return next((k for k, v in self.rooms.items() if v and v == room_id), None)


def load_policy(path: str | Path) -> Policy:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: policy file must be a mapping")
    return Policy.model_validate(data)


# ---------------------------------------------------------------------------------------------
# P2 / P3 / P4 checks
# ---------------------------------------------------------------------------------------------


def check_proposer(policy: Policy, proposal: Proposal, proposer_id: str) -> str | None:
    """P2: the sender must be the one agent allowed to propose this kind under this rule."""
    rule = policy.rule(proposal.rule)
    if rule is None:
        return f"unknown rule {proposal.rule!r}"
    if rule.kind is not proposal.kind:
        return f"rule {rule.name} is for {rule.kind.value} proposals, not {proposal.kind.value}"
    allowed = policy.proposer_id(rule.proposer)
    if not allowed or proposer_id != allowed:
        return f"only {rule.proposer} may propose under rule {rule.name}"
    return None


def check_target(policy: Policy, proposal: Proposal) -> str | None:
    """P3: the proposal fits the rule: its currency, its shape."""
    rule = policy.rule(proposal.rule)
    if rule is None:
        return f"unknown rule {proposal.rule!r}"
    body: Any = proposal.body
    currency = getattr(body, "currency", None)
    if currency is not None and currency != rule.currency:
        return f"currency {currency} is not rule {rule.name}'s {rule.currency}"
    if proposal.kind is Kind.GOALS and body.cost_cap_cents >= body.revenue_target_cents:
        return "the cost cap must sit below the revenue target"
    if proposal.kind is Kind.CLOSE and body.payables and body.total_payables_cents <= 0:
        return "a plan that names payables must carry their total"
    return None


def check_facts(policy: Policy, proposal: Proposal, facts: dict[str, Any]) -> str | None:
    """P4: the ledger-read snapshot must describe something the rule can act on."""
    rule = policy.rule(proposal.rule)
    if rule is None:
        return f"unknown rule {proposal.rule!r}"
    body: Any = proposal.body
    if proposal.kind in (Kind.GOALS, Kind.CLOSE):
        return None
    if proposal.kind is Kind.PAY:
        rows = {r["invoice"]: r for r in facts.get("payables", [])}
        for line in body.run:
            r = rows.get(line.invoice)
            if r is None:
                return f"{line.invoice} is not in the ledger"
            if r["status"] != "open":
                return f"{line.invoice} is {r['status']}, not open"
            if int(r["amount_cents"]) != line.amount_cents or r["vendor"] != line.vendor:
                return f"{line.invoice} differs from the ledger"
        if int(facts.get("total_cents", -1)) != body.total_cents:
            return "the run's total differs from the ledger"
        return None
    if proposal.kind is Kind.DEAL:
        if not facts.get("list_cents"):
            return f"{body.product} is not in the price book"
        if int(facts["list_cents"]) != body.list_cents:
            from sindarin.schema import money

            have = money(int(facts["list_cents"]), rule.currency)
            want = money(body.list_cents, rule.currency)
            return f"the list price of {body.product} is {have}, not {want}"
        if facts.get("period_status") == "locked":
            return f"period {body.period} is locked; the deal cannot land in it"
        if facts.get("deal_stage") == "won":
            return f"{body.deal_id} is already won"
        return None
    if proposal.kind is Kind.LOCK:
        if facts.get("period_status") != "open":
            return f"period {body.period} is {facts.get('period_status') or 'unknown'}, not open"
        if int(facts.get("open_payables", 0)) > 0:
            return f"{facts['open_payables']} payable(s) still open in {body.period}"
        return None
    if proposal.kind is Kind.BUDGET:
        known = set(facts.get("accounts", []))
        for line in body.lines:
            if known and line.account not in known:
                return f"account {line.account} is not in the chart"
        cap = facts.get("cap_cents")
        if cap is not None and body.total_cents > int(cap):
            from sindarin.schema import money

            return (
                f"the lines total {money(body.total_cents, rule.currency)}, over the cap "
                f"{money(int(cap), rule.currency)} in {body.goals}"
            )
        if facts.get("basis_status") == "unknown":
            return "the basis period has no rows in the ledger"
        return None
    status = facts.get("period_status")
    if status not in ("open", "unknown"):
        return f"period {body.period} is {status}, already"
    recorded = facts.get("budget_lines")
    if not recorded:
        return f"no budget is recorded for {body.period} yet"
    if facts.get("budget_id") and facts["budget_id"] != body.budget:
        return f"the recorded budget for {body.period} is {facts['budget_id']}, not {body.budget}"
    mine = sorted((x.department, x.account, x.amount_cents) for x in body.lines)
    theirs = sorted((r["department"], r["account"], int(r["amount_cents"])) for r in recorded)
    if mine != theirs:
        return "the lines differ from the budget recorded in the ledger"
    return None
