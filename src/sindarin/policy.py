"""policy.yaml: groups, proposers, rooms and rules by kind (DDD §10.1); the P2/P3 checks."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sindarin.names import AGENT_ROLES, EXECUTORS
from sindarin.schema import REPO_PATTERN, RULE_PATTERN, Kind, Proposal

DURATION_RE = re.compile(r"^(\d+)(m|h|d)$")
_UNITS = {"m": 60, "h": 3600, "d": 86400}
ROOM_KEYS = ("product", "releases", "customer")
FEATURE_ROOM = "feature"  # the artifact's own room (§10.1 merge rule)


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
    room: str = Field(min_length=1)  # a key in `rooms`, or `feature`
    pending_ttl: str = "24h"
    # spec
    allowed_repos: list[str] = Field(default_factory=list)
    engineers: str = "engineering"  # group whose members a spec may name as its engineer
    # pr / release
    repo: str = ""
    base: str = "main"
    tag_pattern: str = r"^v\d+\.\d+\.\d+$"
    merge_method: str = Field(default="squash", pattern="^(squash|merge|rebase)$")

    @field_validator("pending_ttl")
    @classmethod
    def _ttl(cls, v: str) -> str:
        parse_duration(v)
        return v

    @field_validator("allowed_repos")
    @classmethod
    def _repos(cls, v: list[str]) -> list[str]:
        for r in v:
            if not re.match(REPO_PATTERN, r):
                raise ValueError(f"bad repository {r!r}")
        return v

    @field_validator("tag_pattern")
    @classmethod
    def _tag_re(cls, v: str) -> str:
        try:
            re.compile(v)
        except re.error as exc:
            raise ValueError(f"tag_pattern is not a regular expression: {exc}") from None
        return v

    @model_validator(mode="after")
    def _fits_kind(self) -> Rule:
        if self.kind is Kind.SPEC:
            if not self.allowed_repos:
                raise ValueError(f"rule {self.name}: a spec rule needs allowed_repos")
            if self.room == FEATURE_ROOM:
                raise ValueError(f"rule {self.name}: specs are approved in a named room")
        else:
            if not re.match(REPO_PATTERN, self.repo):
                raise ValueError(
                    f"rule {self.name}: a {self.kind.value} rule needs repo owner/repo"
                )
            if self.kind is Kind.PR and self.room != FEATURE_ROOM:
                raise ValueError(f"rule {self.name}: merges are approved in the feature room")
        return self

    @property
    def pending_ttl_seconds(self) -> int:
        return parse_duration(self.pending_ttl)


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = 2
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
            if r.room != FEATURE_ROOM and r.room not in ROOM_KEYS:
                raise ValueError(f"rule {r.name}: room {r.room!r} is not a known room")
            if r.kind is Kind.SPEC and r.engineers not in self.groups:
                raise ValueError(f"rule {r.name}: engineers group {r.engineers!r} is not defined")
        return self

    # -- lookups (ids only, never names) ----------------------------------------------------

    def rule(self, name: str) -> Rule | None:
        return next((r for r in self.rules if r.name == name), None)

    def rules_for(self, kind: Kind) -> list[Rule]:
        return [r for r in self.rules if r.kind is kind]

    def group_members(self, group: str) -> list[str]:
        return list(self.groups.get(group, []))

    def in_group(self, participant_id: str, group: str) -> bool:
        """Exact match on the opaque participant id (§4.3 rule 3)."""
        return bool(participant_id) and participant_id in self.groups.get(group, [])

    def groups_of(self, participant_id: str) -> list[str]:
        return [g for g, ids in self.groups.items() if participant_id and participant_id in ids]

    def proposer_id(self, role: str) -> str:
        return self.proposers.get(role, "")

    def role_of(self, participant_id: str) -> str | None:
        return next((r for r, pid in self.proposers.items() if pid and pid == participant_id), None)

    @property
    def agent_ids(self) -> frozenset[str]:
        """Every non-human participant: the four agents and the two executors (A3)."""
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
# P2 / P3 / P4 checks (DDD §7.1)
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
    """P3: action and target are in the rule's allow-list."""
    rule = policy.rule(proposal.rule)
    if rule is None:
        return f"unknown rule {proposal.rule!r}"
    body: Any = proposal.body
    if proposal.kind is Kind.SPEC:
        if body.repo not in rule.allowed_repos:
            return (
                f"repository {body.repo!r} is not allowed by rule {rule.name} "
                f"(allowed: {', '.join(rule.allowed_repos)})"
            )
        if not policy.in_group(body.engineer, rule.engineers):
            return f"engineer is not in group {rule.engineers}"
        return None
    if proposal.repo != rule.repo:
        return (
            f"repository {proposal.repo!r} is not allowed by rule {rule.name} "
            f"(allowed: {rule.repo})"
        )
    if proposal.kind is Kind.RELEASE and not re.match(rule.tag_pattern, body.tag):
        return f"tag {body.tag!r} does not match rule {rule.name} pattern {rule.tag_pattern}"
    return None


def check_facts(policy: Policy, proposal: Proposal, facts: dict[str, Any]) -> str | None:
    """P4: the platform-read snapshot must describe something the rule can act on."""
    rule = policy.rule(proposal.rule)
    if rule is None:
        return f"unknown rule {proposal.rule!r}"
    if proposal.kind is Kind.SPEC:
        return None
    if proposal.kind is Kind.PR:
        if facts.get("state") != "open":
            return f"pull request is {facts.get('state') or 'unknown'}, not open"
        if facts.get("merged"):
            return "pull request is already merged"
        if facts.get("draft"):
            return "pull request is a draft"
        if facts.get("base") != rule.base:
            return (
                f"pull request targets {facts.get('base')!r}, rule {rule.name} merges into "
                f"{rule.base}"
            )
        if not facts.get("head_sha"):
            return "pull request head sha could not be read"
        return None
    if facts.get("tag_exists"):
        return f"tag {proposal.body.tag} already exists"  # type: ignore[union-attr]
    if not facts.get("commit_exists", False):
        return "target sha is not a commit in the repository"
    return None
