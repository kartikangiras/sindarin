"""Proposal v2, review v1, snapshots, artifacts and audit rows (DDD §8, §11). Pure data.

Every fence an executor reads is validated here with `extra="forbid"`. The snapshot is the
canonical proposal plus the facts the executor read from the platform; its SHA-256 is what an
approval binds to (§8.1, Appendix A).
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PROPOSAL_SCHEMA = "sindarin/proposal/v2"
REVIEW_SCHEMA = "sindarin/review/v1"
SNAPSHOT_SCHEMA = "sindarin/snapshot/v1"
STATUS_SCHEMA = "sindarin/status/v2"

PROPOSAL_FENCE = "sindarin-proposal"
REVIEW_FENCE = "sindarin-review"
STATUS_FENCE = "sindarin-status"

REPO_PATTERN = r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9_.-]{1,100}$"
SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{0,47}$"
SHA_PATTERN = r"^[0-9a-f]{40}$"
RULE_PATTERN = r"^[a-z0-9][a-z0-9-]{0,63}$"
ARTIFACT_ID_PATTERN = r"^(?:SPEC|PR|REL)-\d+$"
SPEC_ID_PATTERN = r"^SPEC-\d+$"
PR_ID_PATTERN = r"^PR-\d+$"
LOGIN_PATTERN = r"^[A-Za-z0-9-]{1,39}$"
MESSAGE_ID_MAX = 200
MAX_JUSTIFICATION = 280
MAX_NOTES = 2000

_PR_TARGET_RE = re.compile(r"^([A-Za-z0-9-]{1,39}/[A-Za-z0-9_.-]{1,100})#(\d+)$")
_REL_TARGET_RE = re.compile(r"^([A-Za-z0-9-]{1,39}/[A-Za-z0-9_.-]{1,100})@([^\s@]{1,100})$")


class Kind(StrEnum):
    SPEC = "spec"
    PR = "pr"
    RELEASE = "release"


PREFIX: dict[Kind, str] = {Kind.SPEC: "SPEC", Kind.PR: "PR", Kind.RELEASE: "REL"}
KIND_OF_PREFIX: dict[str, Kind] = {v: k for k, v in PREFIX.items()}
PLATFORM_OF: dict[Kind, str] = {Kind.SPEC: "switch", Kind.PR: "github", Kind.RELEASE: "github"}
ACTION_OF: dict[Kind, str] = {
    Kind.SPEC: "open_feature_room",
    Kind.PR: "merge_pr",
    Kind.RELEASE: "create_release",
}


def kind_of_id(artifact_id: str) -> Kind | None:
    prefix = artifact_id.split("-", 1)[0]
    return KIND_OF_PREFIX.get(prefix)


# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------


def canonical_json(obj: Any) -> str:
    """Canonical form for snapshots and fences (§8.1)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def utcnow() -> datetime:
    return datetime.now(tz=UTC)


def fmt_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def short_sha(sha: str | None) -> str:
    return (sha or "")[:7]


def fence_pattern(tag: str) -> re.Pattern[str]:
    return re.compile(rf"```{re.escape(tag)}[ \t]*\r?\n(.*?)\r?\n[ \t]*```", re.DOTALL)


def extract_fences(body: str, tag: str) -> list[str]:
    return fence_pattern(tag).findall(body or "")


def has_fence(body: str, tag: str) -> bool:
    return f"```{tag}" in (body or "")


def render_fence(tag: str, obj: Any) -> str:
    return f"```{tag}\n{canonical_json(obj)}\n```"


# ---------------------------------------------------------------------------------------------
# proposal v2 (§8.1)
# ---------------------------------------------------------------------------------------------


class SpecBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    title: str = Field(min_length=1, max_length=120)
    press_release: str = Field(min_length=1, max_length=400)
    faq: list[str] = Field(min_length=3, max_length=3)
    criteria: list[str] = Field(min_length=3, max_length=5)
    scope_in: list[str] = Field(default_factory=list, max_length=10)
    scope_out: list[str] = Field(default_factory=list, max_length=10)
    target_date: date
    engineer: str = Field(min_length=1, max_length=200)  # participant id, never a name
    repo: str = Field(pattern=REPO_PATTERN)

    @field_validator("faq", "criteria", "scope_in", "scope_out")
    @classmethod
    def _lines(cls, v: list[str]) -> list[str]:
        for s in v:
            if not s.strip() or len(s) > 300:
                raise ValueError("each line must be 1-300 characters")
        return v


class PrBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    number: int = Field(strict=True, ge=1)
    title: str = Field(min_length=1, max_length=200)
    branch: str = Field(min_length=1, max_length=200)
    spec: str = Field(pattern=SPEC_ID_PATTERN)


class ReleaseBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    tag: str = Field(min_length=1, max_length=100)
    target_sha: str = Field(pattern=SHA_PATTERN)
    notes: str = Field(min_length=1, max_length=MAX_NOTES)
    specs: list[str] = Field(min_length=1, max_length=20)
    asks: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("specs")
    @classmethod
    def _spec_ids(cls, v: list[str]) -> list[str]:
        for s in v:
            if not re.match(SPEC_ID_PATTERN, s):
                raise ValueError(f"bad spec id {s!r}")
        return v

    @field_validator("asks")
    @classmethod
    def _ask_ids(cls, v: list[str]) -> list[str]:
        for s in v:
            if not s or len(s) > MESSAGE_ID_MAX:
                raise ValueError("bad ask message id")
        return v


BODY_OF: dict[Kind, type[BaseModel]] = {
    Kind.SPEC: SpecBody,
    Kind.PR: PrBody,
    Kind.RELEASE: ReleaseBody,
}


class Proposal(BaseModel):
    """What an agent posts inside a ```sindarin-proposal fence (§8.1). No extras, all typed."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_: Literal["sindarin/proposal/v2"] = Field(alias="schema")
    kind: Kind
    platform: Literal["switch", "github"]
    action: Literal["open_feature_room", "merge_pr", "create_release"]
    target: str = Field(min_length=1, max_length=200)
    snapshot_hint: str = Field(default="", max_length=64)
    body: SpecBody | PrBody | ReleaseBody
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
        if self.kind is Kind.SPEC:
            if not re.match(SLUG_PATTERN, self.target):
                raise ValueError("spec target is the feature room slug, e.g. feat-csv-export")
            if self.snapshot_hint:
                raise ValueError("spec proposals carry no snapshot_hint")
        elif self.kind is Kind.PR:
            m = _PR_TARGET_RE.match(self.target)
            if not m:
                raise ValueError("pr target is owner/repo#number")
            if int(m.group(2)) != self.body.number:  # type: ignore[union-attr]
                raise ValueError("pr target number differs from body.number")
        else:
            m = _REL_TARGET_RE.match(self.target)
            if not m:
                raise ValueError("release target is owner/repo@tag")
            if m.group(2) != self.body.tag:  # type: ignore[union-attr]
                raise ValueError("release target tag differs from body.tag")
        if self.snapshot_hint and not re.match(SHA_PATTERN, self.snapshot_hint):
            raise ValueError("snapshot_hint must be a 40-hex commit sha")
        return self

    @property
    def repo(self) -> str:
        if self.kind is Kind.SPEC:
            return self.body.repo  # type: ignore[union-attr]
        return self.target.split("#", 1)[0].split("@", 1)[0]

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, mode="json")

    def canonical(self) -> str:
        return canonical_json(self.to_dict())

    def render(self) -> str:
        return render_fence(PROPOSAL_FENCE, self.to_dict())


# ---------------------------------------------------------------------------------------------
# review v1 (§8.2)
# ---------------------------------------------------------------------------------------------


class ReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    criterion: int = Field(strict=True, ge=1)
    met: bool
    evidence: str = Field(min_length=1, max_length=300)


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)
    schema_: Literal["sindarin/review/v1"] = Field(alias="schema")
    pr: str = Field(pattern=PR_ID_PATTERN)
    head_sha: str = Field(pattern=SHA_PATTERN)
    results: list[ReviewResult] = Field(min_length=1, max_length=10)
    summary: str = Field(min_length=1, max_length=80)

    @property
    def met(self) -> int:
        return sum(1 for r in self.results if r.met)

    @property
    def total(self) -> int:
        return len(self.results)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, mode="json")

    def render(self) -> str:
        return render_fence(REVIEW_FENCE, self.to_dict())


# ---------------------------------------------------------------------------------------------
# snapshot (§8.1, Appendix A)
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
# states (§7.3)
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
    RELEASED = "released"
    MERGED = "merged"
    MERGE_FAILED = "merge_failed"
    PUBLISHED = "published"
    PUBLISH_FAILED = "publish_failed"
    FAILED = "failed"
    DENIED = "denied"
    EXPIRED = "expired"


TERMINAL_STATES = frozenset(
    {State.RELEASED, State.MERGED, State.PUBLISHED, State.FAILED, State.DENIED, State.EXPIRED}
)
WAITING_STATES = frozenset({State.PENDING, State.CHANGES_REQUESTED})
RETRY_STATES = frozenset({State.OPEN_FAILED, State.MERGE_FAILED, State.PUBLISH_FAILED})
IN_FLIGHT_STATES = frozenset({State.APPROVED, State.EXECUTING, State.OPENING})
SUCCESS_OF: dict[Kind, State] = {
    Kind.SPEC: State.OPEN,
    Kind.PR: State.MERGED,
    Kind.RELEASE: State.PUBLISHED,
}
FAILURE_OF: dict[Kind, State] = {
    Kind.SPEC: State.OPEN_FAILED,
    Kind.PR: State.MERGE_FAILED,
    Kind.RELEASE: State.PUBLISH_FAILED,
}
RUNNING_OF: dict[Kind, State] = {
    Kind.SPEC: State.OPENING,
    Kind.PR: State.EXECUTING,
    Kind.RELEASE: State.EXECUTING,
}


# ---------------------------------------------------------------------------------------------
# artifact (§11 `artifacts`)
# ---------------------------------------------------------------------------------------------


class Artifact(BaseModel):
    """The executor's record of a spec, a PR or a release (Appendix A)."""

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
        return self.snapshot().facts

    def result(self) -> dict[str, Any]:
        return json.loads(self.result_json) if self.result_json else {}

    @property
    def target(self) -> str:
        return self.proposal().target


# ---------------------------------------------------------------------------------------------
# optional status fence (§8.3), off by default
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
# audit (§11 `audit`)
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
