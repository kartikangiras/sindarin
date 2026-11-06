"""Proposal v3, snapshots, artifacts and audit rows (design v5 §5, §6). Pure data.
Every fence an executor reads is validated here with `extra="forbid"`. The snapshot is the
canonical proposal plus the facts the executor read from the ledger; its SHA-256 is what an
approval binds to.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PROPOSAL_SCHEMA = "sindarin/proposal/v3"
SNAPSHOT_SCHEMA = "sindarin/snapshot/v1"
STATUS_SCHEMA = "sindarin/status/v2"
PROPOSAL_FENCE = "sindarin-proposal"
STATUS_FENCE = "sindarin-status"

SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{0,47}$"
RULE_PATTERN = r"^[a-z0-9][a-z0-9-]{0,63}$"
ARTIFACT_ID_PATTERN = r"^(?:GOALS|BUDGET|ENTRIES)-\d+$"
GOALS_ID_PATTERN = r"^GOALS-\d+$"
BUDGET_ID_PATTERN = r"^BUDGET-\d+$"
PERIOD_PATTERN = r"^\d{4}-(?:0[1-9]|1[0-2])$"
INVOICE_PATTERN = r"^[A-Z][A-Z0-9-]{2,30}$"
ACCOUNT_PATTERN = r"^\d{4}$"
DEPARTMENT_PATTERN = r"^[a-z][a-z0-9-]{1,39}$"
DIGEST_PATTERN = r"^[0-9a-f]{64}$"
LOGIN_PATTERN = r"^[A-Za-z0-9-]{1,39}$"
MESSAGE_ID_MAX = 200
MAX_JUSTIFICATION = 280
CURRENCIES = ("USD", "EUR", "GBP", "INR")
Currency = Literal["USD", "EUR", "GBP", "INR"]


class Kind(StrEnum):
    GOALS = "goals"  # the period's targets; approved by the cfo; opens the plan room
    BUDGET = "budget"  # the department lines under the cap; cfo, the board above the limit
    ENTRIES = "entries"  # the budget register entries into the books; approved by controllers


PREFIX: dict[Kind, str] = {Kind.GOALS: "GOALS", Kind.BUDGET: "BUDGET", Kind.ENTRIES: "ENTRIES"}
KIND_OF_PREFIX: dict[str, Kind] = {v: k for k, v in PREFIX.items()}
PLATFORM_OF: dict[Kind, str] = {
    Kind.GOALS: "switch",
    Kind.BUDGET: "ledger",
    Kind.ENTRIES: "ledger",
}
ACTION_OF: dict[Kind, str] = {
    Kind.GOALS: "open_plan_room",
    Kind.BUDGET: "record_budget",
    Kind.ENTRIES: "post_budget_entries",
}


def kind_of_id(artifact_id: str) -> Kind | None:
    prefix = artifact_id.split("-", 1)[0]
    return KIND_OF_PREFIX.get(prefix)


# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------


def canonical_json(obj: Any) -> str:
    """Canonical form for snapshots and fences."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def utcnow() -> datetime:
    return datetime.now(tz=UTC)


def fmt_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def short_sha(sha: str | None) -> str:
    return (sha or "")[:7]


def money(cents: int, currency: str = "USD") -> str:
    """Money the way a person reads it: `16,000.00 USD`. The only number format on screen."""
    sign = "-" if cents < 0 else ""
    return f"{sign}{abs(int(cents)) / 100:,.2f} {currency}"


def percent(bps: int) -> str:
    """Basis points the way a person reads them: `83.2%`."""
    return f"{int(bps) / 100:.1f}%"


def previous_period(period: str) -> str:
    year, month = period.split("-")
    y, m = int(year), int(month)
    if m == 1:
        return f"{y - 1}-12"
    return f"{y}-{m - 1:02d}"


def fence_pattern(tag: str) -> re.Pattern[str]:
    return re.compile(rf"```{re.escape(tag)}[ \t]*\r?\n(.*?)\r?\n[ \t]*```", re.DOTALL)


def extract_fences(body: str, tag: str) -> list[str]:
    return fence_pattern(tag).findall(body or "")


def has_fence(body: str, tag: str) -> bool:
    return f"```{tag}" in (body or "")


def render_fence(tag: str, obj: Any) -> str:
    return f"```{tag}\n{canonical_json(obj)}\n```"


# ---------------------------------------------------------------------------------------------
# proposal v3 bodies
# ---------------------------------------------------------------------------------------------


class BudgetLine(BaseModel):
    """One department's line of the period's budget, against one account of the chart."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    department: str = Field(pattern=DEPARTMENT_PATTERN)
    account: str = Field(pattern=ACCOUNT_PATTERN)
    amount_cents: int = Field(strict=True, gt=0)


def _unique(values: list[str], what: str) -> None:
    if len(set(values)) != len(values):
        raise ValueError(f"{what} repeat")


class GoalsBody(BaseModel):
    """The period's goals: a revenue target, a margin target and the cost cap the budget must
    fit under, with the prior period's report as their basis."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    period: str = Field(pattern=PERIOD_PATTERN)
    basis: str = Field(pattern=PERIOD_PATTERN)
    title: str = Field(min_length=1, max_length=120)
    summary: str = Field(min_length=1, max_length=300)
    report: list[str] = Field(default_factory=list, max_length=10)
    revenue_target_cents: int = Field(strict=True, gt=0)
    margin_target_bps: int = Field(strict=True, ge=0, le=10000)
    cost_cap_cents: int = Field(strict=True, gt=0)
    currency: Currency = "USD"

    @field_validator("report")
    @classmethod
    def _lines(cls, v: list[str]) -> list[str]:
        for s in v:
            if not s.strip() or len(s) > 160:
                raise ValueError("each report line must be 1-160 characters")
        return v

    @model_validator(mode="after")
    def _basis_before(self) -> GoalsBody:
        if self.basis >= self.period:
            raise ValueError("the basis period comes before the period planned")
        return self


class BudgetBody(BaseModel):
    """The period's budget: one line per department, their sum under the goals' cost cap."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    period: str = Field(pattern=PERIOD_PATTERN)
    goals: str = Field(pattern=GOALS_ID_PATTERN)
    lines: list[BudgetLine] = Field(min_length=1, max_length=20)
    total_cents: int = Field(strict=True, gt=0)
    currency: Currency = "USD"

    @model_validator(mode="after")
    def _sums(self) -> BudgetBody:
        _unique([x.department for x in self.lines], "departments")
        total = sum(x.amount_cents for x in self.lines)
        if total != self.total_cents:
            raise ValueError(
                f"total_cents {self.total_cents} is not the sum of the lines ({total})"
            )
        return self


class EntriesBody(BaseModel):
    """The budget register entries: the recorded budget's lines posted into the books as the
    period's planned amounts, and the period marked planned."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    period: str = Field(pattern=PERIOD_PATTERN)
    goals: str = Field(pattern=GOALS_ID_PATTERN)
    budget: str = Field(pattern=BUDGET_ID_PATTERN)
    lines: list[BudgetLine] = Field(min_length=1, max_length=20)
    memo: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def _departments(self) -> EntriesBody:
        _unique([x.department for x in self.lines], "departments")
        return self

    @property
    def total_cents(self) -> int:
        return sum(x.amount_cents for x in self.lines)


BODY_OF: dict[Kind, type[BaseModel]] = {
    Kind.GOALS: GoalsBody,
    Kind.BUDGET: BudgetBody,
    Kind.ENTRIES: EntriesBody,
}


def goals_target(period: str) -> str:
    return f"plan-{period}"


def budget_target(period: str) -> str:
    return f"budget:{period}"


def entries_target(period: str) -> str:
    return f"period:{period}"


class Proposal(BaseModel):
    """What an agent posts inside a ```sindarin-proposal fence. No extras, all typed."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_: Literal["sindarin/proposal/v3"] = Field(alias="schema")
    kind: Kind
    platform: Literal["switch", "ledger"]
    action: Literal["open_plan_room", "record_budget", "post_budget_entries"]
    target: str = Field(min_length=1, max_length=200)
    snapshot_hint: str = Field(default="", max_length=64)
    body: GoalsBody | BudgetBody | EntriesBody
    ask: str = Field(default="", max_length=MESSAGE_ID_MAX)
    rule: str = Field(pattern=RULE_PATTERN)
    justification: str = Field(max_length=MAX_JUSTIFICATION)

    @model_validator(mode="before")
    @classmethod
    def _typed_body(cls, data: Any) -> Any:
        if isinstance(data, dict) and isinstance(data.get("body"), dict):
            kind = data.get("kind")
            try:
                k = Kind(kind)
            except ValueError:
                return data  # the kind field's own validation reports it
            data = dict(data)
            data["body"] = BODY_OF[k].model_validate(data["body"])
        return data

    @model_validator(mode="after")
    def _consistent(self) -> Proposal:
        if self.platform != PLATFORM_OF[self.kind]:
            raise ValueError(f"kind {self.kind.value} is on platform {PLATFORM_OF[self.kind]}")
        if self.action != ACTION_OF[self.kind]:
            raise ValueError(f"kind {self.kind.value} takes action {ACTION_OF[self.kind]}")
        if not isinstance(self.body, BODY_OF[self.kind]):
            raise ValueError(f"body does not fit kind {self.kind.value}")
        period = self.body.period
        if self.kind is Kind.GOALS:
            if self.target != goals_target(period):
                raise ValueError(f"goals target is the plan room slug, {goals_target(period)}")
            if self.snapshot_hint:
                raise ValueError("goals proposals carry no snapshot_hint")
        elif self.kind is Kind.BUDGET and self.target != budget_target(period):
            raise ValueError(f"budget target is {budget_target(period)}")
        elif self.kind is Kind.ENTRIES and self.target != entries_target(period):
            raise ValueError(f"entries target is {entries_target(period)}")
        if self.snapshot_hint and not re.match(DIGEST_PATTERN, self.snapshot_hint):
            raise ValueError("snapshot_hint must be a sha256 hex digest")
        return self

    @property
    def period(self) -> str:
        return str(self.body.period)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, mode="json")

    def canonical(self) -> str:
        return canonical_json(self.to_dict())

    def render(self) -> str:
        return render_fence(PROPOSAL_FENCE, self.to_dict())


# ---------------------------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------------------------


class Snapshot(BaseModel):
    """Canonical proposal plus the platform facts read at proposal time. Hash = the gate."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)
    schema_: Literal["sindarin/snapshot/v1"] = Field(alias="schema")
    artifact_id: str = Field(pattern=ARTIFACT_ID_PATTERN)
    proposer_id: str
    proposal: dict[str, Any]
    facts: dict[str, Any] = Field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, mode="json")

    def canonical(self) -> str:
        return canonical_json(self.to_dict())

    @property
    def hash(self) -> str:
        return sha256_hex(self.canonical())


def make_snapshot(artifact_id: str, proposer_id: str, proposal: Proposal, facts: dict) -> Snapshot:
    return Snapshot(
        schema=SNAPSHOT_SCHEMA,
        artifact_id=artifact_id,
        proposer_id=proposer_id,
        proposal=proposal.to_dict(),
        facts=dict(facts),
    )


# ---------------------------------------------------------------------------------------------
# states
# ---------------------------------------------------------------------------------------------


class State(StrEnum):
    PENDING = "pending"
    CHANGES_REQUESTED = "changes_requested"
    APPROVED = "approved"
    EXECUTING = "executing"
    OPENING = "opening"
    OPEN = "open"
    OPEN_FAILED = "open_failed"
    DONE = "done"
    RECORDED = "recorded"
    RECORD_FAILED = "record_failed"
    PLANNED = "planned"
    PLAN_FAILED = "plan_failed"
    FAILED = "failed"
    DENIED = "denied"
    EXPIRED = "expired"


TERMINAL_STATES = frozenset(
    {State.DONE, State.RECORDED, State.PLANNED, State.FAILED, State.DENIED, State.EXPIRED}
)
WAITING_STATES = frozenset({State.PENDING, State.CHANGES_REQUESTED})
RETRY_STATES = frozenset({State.OPEN_FAILED, State.RECORD_FAILED, State.PLAN_FAILED})
IN_FLIGHT_STATES = frozenset({State.APPROVED, State.EXECUTING, State.OPENING})
SUCCESS_OF: dict[Kind, State] = {
    Kind.GOALS: State.OPEN,
    Kind.BUDGET: State.RECORDED,
    Kind.ENTRIES: State.PLANNED,
}
FAILURE_OF: dict[Kind, State] = {
    Kind.GOALS: State.OPEN_FAILED,
    Kind.BUDGET: State.RECORD_FAILED,
    Kind.ENTRIES: State.PLAN_FAILED,
}
RUNNING_OF: dict[Kind, State] = {
    Kind.GOALS: State.OPENING,
    Kind.BUDGET: State.EXECUTING,
    Kind.ENTRIES: State.EXECUTING,
}


# ---------------------------------------------------------------------------------------------
# artifact
# ---------------------------------------------------------------------------------------------


class Artifact(BaseModel):
    """The executor's record of the goals, a budget or its register entries."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=ARTIFACT_ID_PATTERN)
    kind: Kind
    state: State = State.PENDING
    rule: str
    room_id: str
    thread_id: str
    proposal_message_id: str
    proposer_id: str
    proposer_name: str
    proposal_json: str
    snapshot_json: str
    snapshot_sha: str
    ask_root_id: str = ""
    created_at: datetime
    updated_at: datetime | None = None
    expires_at: datetime
    seq: int = 1
    approver_id: str | None = None
    approver_name: str | None = None
    approved_at: datetime | None = None
    approval_message_id: str | None = None
    note: str | None = None
    error: str | None = None
    failed_at: datetime | None = None
    attempts: int = 0
    result_json: str | None = None

    def proposal(self) -> Proposal:
        return Proposal.model_validate_json(self.proposal_json)

    def snapshot(self) -> Snapshot:
        return Snapshot.model_validate_json(self.snapshot_json)

    def facts(self) -> dict[str, Any]:
        return dict(self.snapshot().facts)

    def result(self) -> dict[str, Any]:
        return json.loads(self.result_json) if self.result_json else {}

    @property
    def target(self) -> str:
        return self.proposal().target

    @property
    def period(self) -> str:
        return self.proposal().period


# ---------------------------------------------------------------------------------------------
# optional status fence, off by default
# ---------------------------------------------------------------------------------------------


class StatusFence(BaseModel):
    """Machine-readable projection of an artifact for anyone who wants it. No ids of people."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)
    schema_: Literal["sindarin/status/v2"] = Field(alias="schema")
    id: str
    kind: str
    state: str
    rule: str
    target: str
    approver_name: str | None = None
    snapshot_sha: str
    error: str | None = None
    updated_at: str

    @classmethod
    def from_artifact(cls, a: Artifact, now: datetime, state: State | None = None) -> StatusFence:
        st = state if state is not None else a.state
        return cls(
            schema=STATUS_SCHEMA,
            id=a.id,
            kind=a.kind.value,
            state=st.value,
            rule=a.rule,
            target=a.target,
            approver_name=a.approver_name,
            snapshot_sha=a.snapshot_sha,
            error=a.error,
            updated_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

    def render(self) -> str:
        return render_fence(STATUS_FENCE, self.model_dump(by_alias=True))


def is_status_fence(text: str) -> bool:
    return (text or "").lstrip().startswith(f"```{STATUS_FENCE}")


# ---------------------------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------------------------


class AuditEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int | None = None
    ts: datetime
    artifact_id: str | None = None
    event: str
    actor_id: str | None = None
    actor_name: str | None = None
    room_id: str | None = None
    message_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)

    def render_line(self) -> str:
        parts = [self.ts.strftime("%Y-%m-%d %H:%M:%S"), self.event]
        if self.artifact_id:
            parts.append(self.artifact_id)
        if self.actor_name or self.actor_id:
            parts.append(f"by {self.actor_name or self.actor_id}")
        if self.details:
            parts.append(canonical_json(self.details))
        return " · ".join(parts)
