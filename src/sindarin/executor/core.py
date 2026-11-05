"""Pure pipelines: events in, Effects out (DDD §4.3, §7). No I/O in this module, ever.

Inputs are a MessageEvent (parsed), a read-only View of the store, the policy and users files,
the facts the runner read from the platform, and a clock. The only text interpreted here is the
regex table in commands.py (§9) and the JSON inside ```sindarin-proposal / ```sindarin-review
fences (§8). Nothing else is input. Natural language is never interpreted.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from pydantic import ValidationError

from sindarin.adapters.base import ActionResult, Facts, VerifyResult
from sindarin.executor import commands as cmd
from sindarin.names import CODER, EXECUTOR_OF_KIND, GITHUB_EXECUTOR, REVIEWER, SWITCH_EXECUTOR
from sindarin.policy import (
    FEATURE_ROOM,
    Policy,
    Rule,
    check_facts,
    check_proposer,
    check_target,
)
from sindarin.schema import (
    FAILURE_OF,
    IN_FLIGHT_STATES,
    PROPOSAL_FENCE,
    RETRY_STATES,
    REVIEW_FENCE,
    RUNNING_OF,
    SUCCESS_OF,
    WAITING_STATES,
    Artifact,
    Kind,
    Proposal,
    Review,
    State,
    StatusFence,
    extract_fences,
    fmt_dt,
    has_fence,
    make_snapshot,
    short_sha,
)
from sindarin.store import MessageRecord
from sindarin.switch.events import MessageEvent
from sindarin.users import Users

# ---------------------------------------------------------------------------------------------
# Effects
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Post:
    room_id: str
    text: str
    thread_id: str | None = None


@dataclass(frozen=True)
class Audit:
    event: str
    artifact_id: str | None = None
    actor_id: str | None = None
    actor_name: str | None = None
    room_id: str | None = None
    message_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RecordMessage:
    message: MessageRecord


@dataclass(frozen=True)
class CreateArtifact:
    artifact: Artifact


@dataclass(frozen=True)
class Transition:
    artifact_id: str
    state: State
    fields: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class UpdateSnapshot:
    artifact_id: str
    proposal_json: str
    snapshot_json: str
    snapshot_sha: str
    proposal_message_id: str | None = None
    expires_at: datetime | None = None


@dataclass(frozen=True)
class RecordApproval:
    artifact_id: str
    approver_id: str
    room_id: str
    message_id: str
    snapshot_sha: str
    decision: str
    note: str | None = None


@dataclass(frozen=True)
class RecordReview:
    artifact_id: str
    review: Review
    reviewer_id: str
    reviewer_name: str


@dataclass(frozen=True)
class Execute:
    """Runner: adapter.execute → adapter.verify → core.after_execute()."""

    artifact_id: str


@dataclass(frozen=True)
class Verify:
    """Runner: adapter.verify only (crash recovery) → core.after_verify()."""

    artifact_id: str


@dataclass(frozen=True)
class SetValue:
    key: str
    value: str


Effect = (
    Post
    | Audit
    | RecordMessage
    | CreateArtifact
    | Transition
    | UpdateSnapshot
    | RecordApproval
    | RecordReview
    | Execute
    | Verify
    | SetValue
)


@dataclass(frozen=True)
class Lookup:
    """A platform read the runner performs before `handle_message` (P4, A6)."""

    proposal: Proposal


# ---------------------------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------------------------


class View(Protocol):
    """Read-only slice of the store the pipelines need."""

    def get_message(self, message_id: str) -> MessageRecord | None: ...
    def get(self, artifact_id: str) -> Artifact | None: ...
    def by_thread(self, thread_id: str, *states: State) -> list[Artifact]: ...
    def in_states(self, *states: State, kind: Kind | None = None) -> list[Artifact]: ...
    def peek_next_id(self, kind: Kind) -> str: ...
    def latest_review(self, artifact_id: str) -> tuple[Review, str, str] | None: ...
    def get_value(self, key: str) -> str | None: ...


@dataclass(frozen=True)
class Context:
    executor: str  # handle: sindarin-switch | sindarin-github
    self_id: str
    policy: Policy
    users: Users
    view: View
    now: datetime
    facts: Mapping[str, Facts] = field(default_factory=dict)  # target → facts read just now
    peer_ids: frozenset[str] = frozenset()  # the other executor(s): agents, never approvers
    room_names: Mapping[str, str] = field(default_factory=dict)  # room id → "#name"
    executor_names: tuple[str, ...] = cmd.DEFAULT_NAMES
    retry_after: timedelta = timedelta(hours=1)
    max_attempts: int = 2  # merge / publish: one retry (§1.4); rooms retry hourly for ever
    post_status_fences: bool = False
    peer_artifacts: tuple[Artifact, ...] = ()  # the other executor's state (board, spec stages)

    @property
    def kinds(self) -> frozenset[Kind]:
        return frozenset(k for k in Kind if EXECUTOR_OF_KIND[k.value] == self.executor)

    @property
    def agent_ids(self) -> frozenset[str]:
        ids = set(self.policy.agent_ids) | set(self.peer_ids)
        if self.self_id:
            ids.add(self.self_id)
        return frozenset(ids)

    @property
    def github_executor_id(self) -> str:
        """sindarin-github's participant id: from policy, else the one configured peer."""
        pid = self.policy.executor_id(GITHUB_EXECUTOR)
        if pid:
            return pid
        peers = sorted(self.peer_ids)
        return peers[0] if len(peers) == 1 else ""

    def room_name(self, room_id: str) -> str:
        if room_id in self.room_names:
            return self.room_names[room_id]
        key = self.policy.room_key(room_id)
        if key:
            return self.room_names.get(key, f"#{key}")
        return "this room"

    def name_of(self, participant_id: str, fallback: str = "") -> str:
        return (
            self.users.name_for(participant_id)
            or self.policy.role_of(participant_id)
            or self.policy.executor_of(participant_id)
            or (self.executor if participant_id and participant_id == self.self_id else None)
            or fallback
            or participant_id
        )


# ---------------------------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------------------------


def _reply_thread(ev: MessageEvent) -> str:
    return ev.thread_id or ev.message_id


def _record(ev: MessageEvent) -> RecordMessage:
    return RecordMessage(
        MessageRecord(
            message_id=ev.message_id,
            room_id=ev.room_id,
            sender=ev.sender,
            sender_name=ev.sender_name,
            thread_id=ev.thread_id,
            timestamp_ms=ev.timestamp,
            addressed=ev.addressed,
        )
    )


def _audit(ev: MessageEvent, event: str, artifact_id: str | None = None, **details: Any) -> Audit:
    return Audit(
        event=event,
        artifact_id=artifact_id,
        actor_id=ev.sender,
        actor_name=ev.sender_name,
        room_id=ev.room_id,
        message_id=ev.message_id,
        details=details,
    )


def _status(ctx: Context, a: Artifact, state: State | None = None) -> list[Effect]:
    if not ctx.post_status_fences:
        return []
    return [Post(a.room_id, StatusFence.from_artifact(a, ctx.now, state).render(), a.thread_id)]


def parse_proposal(text: str) -> tuple[Proposal | None, str | None]:
    """(proposal, None) or (None, one-line reason). The only fence an executor accepts as input."""
    fences = extract_fences(text, PROPOSAL_FENCE)
    if len(fences) != 1:
        return None, f"expected exactly one fence, found {len(fences)}"
    try:
        data = json.loads(fences[0])
    except ValueError as e:
        return None, f"not JSON ({e.__class__.__name__})"
    try:
        return Proposal.model_validate(data), None
    except ValidationError as e:
        first = e.errors()[0]
        loc = ".".join(str(x) for x in first.get("loc", ())) or "body"
        return None, f"{loc}: {first.get('msg', 'invalid')}"


def parse_review(text: str) -> tuple[Review | None, str | None]:
    fences = extract_fences(text, REVIEW_FENCE)
    if len(fences) != 1:
        return None, f"expected exactly one fence, found {len(fences)}"
    try:
        data = json.loads(fences[0])
    except ValueError as e:
        return None, f"not JSON ({e.__class__.__name__})"
    try:
        return Review.model_validate(data), None
    except ValidationError as e:
        first = e.errors()[0]
        loc = ".".join(str(x) for x in first.get("loc", ())) or "body"
        return None, f"{loc}: {first.get('msg', 'invalid')}"


def summarize(ctx: Context, a: Artifact) -> str:
    """One line a person reads in the pending template (§7.4)."""
    p = a.proposal()
    body: Any = p.body
    facts = a.facts()
    if a.kind is Kind.SPEC:
        return (
            f"{body.title} · {body.repo} · engineer {ctx.name_of(body.engineer)} · "
            f"due {body.target_date.isoformat()}"
        )
    if a.kind is Kind.PR:
        return (
            f"#{body.number} {body.title} · `{body.branch}` → {facts.get('base', '?')} at "
            f"`{short_sha(facts.get('head_sha'))}` · {body.spec}"
        )
    return f"{body.tag} at `{short_sha(body.target_sha)}` · {', '.join(body.specs)}"


def action_phrase(a: Artifact, result: dict[str, Any]) -> str:
    if a.kind is Kind.PR:
        sha = short_sha(result.get("merge_commit_sha"))
        return f"merged as `{sha}`" if sha else "merged"
    if a.kind is Kind.RELEASE:
        url = result.get("html_url")
        return f"published {url}" if url else "published"
    room = result.get("room_name") or a.target
    return f"room **#{room.lstrip('#')}** opened"


def _spec_plan(ctx: Context, proposal: Proposal, rule: Rule) -> dict[str, Any]:
    """The membership a spec approval binds to (§7.2): the named engineer, the PM group, the
    coder, the reviewer and sindarin-github. Computed from policy, never from chat text."""
    body: Any = proposal.body
    humans = [body.engineer] + [
        pid for pid in ctx.policy.group_members(rule.approvers) if pid != body.engineer
    ]
    agents = [
        pid
        for pid in (
            ctx.policy.proposer_id(CODER),
            ctx.policy.proposer_id(REVIEWER),
            ctx.github_executor_id,
            ctx.self_id,  # so the brief can be posted into the new room (§5, §7.2)
        )
        if pid
    ]
    return {
        "slug": proposal.target,
        "humans": humans,
        "agents": agents,
        "instructions_sha": _spec_instructions_sha(proposal),
    }


def _spec_instructions_sha(proposal: Proposal) -> str:
    from sindarin.schema import sha256_hex

    return sha256_hex(spec_instructions(proposal))


def spec_instructions(proposal: Proposal) -> str:
    """The room instructions text sindarin-switch sets on the feature room (§5, §7.2)."""
    body: Any = proposal.body
    lines = [
        f"# {body.title}",
        "",
        body.press_release,
        "",
        "## FAQ",
        *[f"- {q}" for q in body.faq],
        "",
        "## Acceptance criteria",
        *[f"{i}. {c}" for i, c in enumerate(body.criteria, 1)],
        "",
        "## Scope",
        f"In: {'; '.join(body.scope_in) or '-'}",
        f"Out: {'; '.join(body.scope_out) or '-'}",
        "",
        f"Target date: {body.target_date.isoformat()} · Repository: {body.repo}",
        "",
        f"Merge is approved here with `@{GITHUB_EXECUTOR} approve PR-n`. "
        "Agents propose; only the named engineer approves; the executor merges.",
    ]
    return "\n".join(lines)


def _live_facts(ctx: Context, a: Artifact, proposal: Proposal, rule: Rule) -> Facts:
    if a.kind is Kind.SPEC:
        return Facts(ok=True, data=_spec_plan(ctx, proposal, rule))
    f = ctx.facts.get(proposal.target)
    if f is None:
        return Facts(ok=False, error="no fresh read of the target")
    return f


def _what_changed(old: dict[str, Any], new: dict[str, Any]) -> str:
    parts = []
    for k in sorted(set(old) | set(new)):
        if old.get(k) != new.get(k):
            o, n = old.get(k), new.get(k)
            if k.endswith("sha") or k.endswith("head"):
                o, n = short_sha(str(o or "")), short_sha(str(n or ""))
            parts.append(f"{k} {o} → {n}")
    return ", ".join(parts) or "snapshot"


# ---------------------------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------------------------


def lookups(ctx: Context, ev: MessageEvent) -> list[Lookup]:
    """Pure: which platform reads the runner must do before handling this event."""
    if not ev.addressed or not ev.sender or ev.sender == ctx.self_id:
        return []
    if has_fence(ev.body, PROPOSAL_FENCE):
        proposal, _ = parse_proposal(ev.body)
        if proposal is not None and proposal.kind in ctx.kinds and proposal.kind is not Kind.SPEC:
            return [Lookup(proposal)]
        return []
    command = cmd.parse_command(ev.body, ctx.executor_names)
    if isinstance(command, cmd.Approve):
        a = ctx.view.get(command.artifact_id)
        if (
            a is not None
            and a.kind in ctx.kinds
            and a.kind is not Kind.SPEC
            and a.state in WAITING_STATES
        ):
            return [Lookup(a.proposal())]
    return []


def handle_message(ctx: Context, ev: MessageEvent) -> list[Effect]:
    """One message event → effects. Identity is recorded first, always."""
    effects: list[Effect] = [_record(ev)]
    if not ev.sender or (ctx.self_id and ev.sender == ctx.self_id):
        return effects  # id-less frames never act; our own posts are echoed back
    if not ev.addressed:
        return effects
    if has_fence(ev.body, PROPOSAL_FENCE):
        effects.extend(_handle_proposal(ctx, ev))
    elif has_fence(ev.body, REVIEW_FENCE):
        effects.extend(_handle_review(ctx, ev))
    else:
        effects.extend(_handle_command(ctx, ev))
    return effects


def handle_sweep(ctx: Context) -> list[Effect]:
    """Expiry, retries, crash recovery, derived spec stages, the daily board (§7.3, §14)."""
    effects: list[Effect] = []
    for a in ctx.view.in_states(*WAITING_STATES):
        if a.kind not in ctx.kinds:
            continue
        if a.expires_at <= ctx.now:
            effects.extend(_expire(ctx, a))
    for a in ctx.view.in_states(*RETRY_STATES):
        if a.kind not in ctx.kinds:
            continue
        due = (a.failed_at or ctx.now) + ctx.retry_after
        if due <= ctx.now:
            effects.append(
                Transition(a.id, RUNNING_OF[a.kind], {"attempts": a.attempts + 1, "error": None})
            )
            effects.append(Audit("retry", a.id, details={"attempt": a.attempts + 1}))
            effects.append(Execute(a.id))
    # crash recovery: anything mid-flight when we sweep was interrupted
    for a in ctx.view.in_states(State.APPROVED):
        if a.kind not in ctx.kinds:
            continue
        effects.append(Transition(a.id, RUNNING_OF[a.kind], {"attempts": a.attempts + 1}))
        effects.append(Audit("recovery_resume", a.id, details={"from": "approved"}))
        effects.append(Verify(a.id))
    for a in ctx.view.in_states(State.EXECUTING, State.OPENING):
        if a.kind in ctx.kinds:
            effects.append(Verify(a.id))
    if ctx.executor == SWITCH_EXECUTOR:
        effects.extend(_spec_stages(ctx))
        effects.extend(_daily_board(ctx))
    return effects


# ---------------------------------------------------------------------------------------------
# P — proposals (§7.1 P1–P5)
# ---------------------------------------------------------------------------------------------


def _reject(ctx: Context, ev: MessageEvent, code: str, text: str, **details: Any) -> list[Effect]:
    return [
        Post(ev.room_id, f"Proposal not recorded — {text}", thread_id=_reply_thread(ev)),
        _audit(ev, f"rejected_proposal:{code}", **details),
    ]


def _proposal_room_ok(ctx: Context, rule: Rule, room_id: str) -> bool:
    if rule.room == FEATURE_ROOM:
        return room_id not in {v for v in ctx.policy.rooms.values() if v}
    return bool(ctx.policy.room_id(rule.room)) and room_id == ctx.policy.room_id(rule.room)


def _rule_room_name(ctx: Context, rule: Rule, a: Artifact | None = None) -> str:
    if rule.room == FEATURE_ROOM:
        return ctx.room_name(a.room_id) if a else "the feature room"
    return ctx.room_names.get(rule.room, f"#{rule.room}")


def _handle_proposal(ctx: Context, ev: MessageEvent) -> list[Effect]:
    # P1
    proposal, reason = parse_proposal(ev.body)
    if proposal is None:
        return _reject(ctx, ev, "malformed", f"malformed: {reason}.")
    if proposal.kind not in ctx.kinds:
        owner = EXECUTOR_OF_KIND[proposal.kind.value]
        return _reject(
            ctx,
            ev,
            "wrong_executor",
            f"{proposal.kind.value} proposals go to @{owner}, not me.",
            kind=proposal.kind.value,
        )
    # P2
    reason = check_proposer(ctx.policy, proposal, ev.sender)
    if reason is not None:
        return _reject(ctx, ev, "bad_sender", f"{reason}.", kind=proposal.kind.value)
    rule = ctx.policy.rule(proposal.rule)
    assert rule is not None  # check_proposer resolved it
    if not _proposal_room_ok(ctx, rule, ev.room_id):
        return _reject(
            ctx,
            ev,
            "wrong_room",
            f"I take {proposal.kind.value} proposals in {_rule_room_name(ctx, rule)} only.",
        )
    # P3
    reason = check_target(ctx.policy, proposal)
    if reason is not None:
        return _reject(ctx, ev, "policy", f"policy: {reason}.", reason=reason)
    # P4 — the snapshot comes from the platform, never from the fence
    if proposal.kind is Kind.SPEC:
        facts = Facts(ok=True, data=_spec_plan(ctx, proposal, rule))
    else:
        facts = ctx.facts.get(proposal.target) or Facts(ok=False, error="target not read")
        if not facts.ok:
            return _reject(
                ctx,
                ev,
                "no_facts",
                f"could not read {proposal.target} from {proposal.platform}: {facts.error}.",
            )
        if (
            proposal.snapshot_hint
            and proposal.kind is Kind.PR
            and facts.data.get("head_sha") != proposal.snapshot_hint
        ):
            return _reject(
                ctx,
                ev,
                "stale",
                f"head is `{short_sha(facts.data.get('head_sha'))}`, the proposal names "
                f"`{short_sha(proposal.snapshot_hint)}`; re-propose at the current head.",
            )
        reason = check_facts(ctx.policy, proposal, facts.data)
        if reason is not None:
            return _reject(ctx, ev, "policy", f"{reason}.", reason=reason)
    thread = _reply_thread(ev)
    # P5 — id, pending, template. Re-proposals keep their id (§6.1 "on changes").
    if proposal.kind is Kind.PR:
        aid = f"PR-{proposal.body.number}"  # type: ignore[union-attr]
        existing = ctx.view.get(aid)
    else:
        aid = ""
        existing = next(
            (
                a
                for a in ctx.view.by_thread(thread, State.CHANGES_REQUESTED, State.PENDING)
                if a.kind is proposal.kind
            ),
            None,
        )
    if existing is not None:
        return _reproposal(ctx, ev, existing, proposal, facts, rule)
    if proposal.kind is not Kind.PR:
        aid = ctx.view.peek_next_id(proposal.kind)
    snapshot = make_snapshot(aid, ev.sender, proposal, facts.data)
    artifact = Artifact(
        id=aid,
        kind=proposal.kind,
        state=State.PENDING,
        rule=rule.name,
        room_id=ev.room_id,
        thread_id=thread,
        proposal_message_id=ev.message_id,
        proposer_id=ev.sender,
        proposer_name=ev.sender_name or ctx.name_of(ev.sender),
        proposal_json=proposal.model_dump_json(by_alias=True),
        snapshot_json=snapshot.canonical(),
        snapshot_sha=snapshot.hash,
        ask_root_id=proposal.ask,
        created_at=ctx.now,
        expires_at=ctx.now + timedelta(seconds=rule.pending_ttl_seconds),
    )
    return [
        CreateArtifact(artifact),
        _audit(
            ev,
            "proposal_accepted",
            aid,
            kind=proposal.kind.value,
            target=proposal.target,
            rule=rule.name,
            snapshot_sha=snapshot.hash,
        ),
        Post(ev.room_id, pending_text(ctx, artifact, rule), thread_id=thread),
        *_status(ctx, artifact),
    ]


def _reproposal(
    ctx: Context, ev: MessageEvent, a: Artifact, proposal: Proposal, facts: Facts, rule: Rule
) -> list[Effect]:
    if a.state not in WAITING_STATES:
        return _reject(
            ctx, ev, "not_open", f"{a.id} is {a.state.value}, not open for proposals.", id=a.id
        )
    if a.room_id != ev.room_id:
        return _reject(ctx, ev, "wrong_room", f"{a.id} lives in {ctx.room_name(a.room_id)}.")
    snapshot = make_snapshot(a.id, ev.sender, proposal, facts.data)
    if a.state is State.PENDING and snapshot.hash == a.snapshot_sha:
        return _reject(ctx, ev, "duplicate", f"{a.id} is already pending with this snapshot.")
    effects: list[Effect] = [
        UpdateSnapshot(
            a.id,
            proposal_json=proposal.model_dump_json(by_alias=True),
            snapshot_json=snapshot.canonical(),
            snapshot_sha=snapshot.hash,
            proposal_message_id=ev.message_id,
            expires_at=ctx.now + timedelta(seconds=rule.pending_ttl_seconds),
        )
    ]
    if a.state is State.CHANGES_REQUESTED:
        effects.append(Transition(a.id, State.PENDING, {"note": None}))
    effects.append(
        _audit(
            ev,
            "reproposed",
            a.id,
            snapshot_sha=snapshot.hash,
            previous=a.snapshot_sha,
            seq=a.seq + 1,
        )
    )
    updated = a.model_copy(
        update={
            "state": State.PENDING,
            "proposal_json": proposal.model_dump_json(by_alias=True),
            "snapshot_json": snapshot.canonical(),
            "snapshot_sha": snapshot.hash,
        }
    )
    effects.append(
        Post(ev.room_id, pending_text(ctx, updated, rule, revised=True), thread_id=a.thread_id)
    )
    effects.extend(_status(ctx, updated, State.PENDING))
    return effects


def pending_text(ctx: Context, a: Artifact, rule: Rule, *, revised: bool = False) -> str:
    """§7.4 `pending` (plus `review shown` when a review exists)."""
    room = _rule_room_name(ctx, rule, a)
    head = f"**{a.id}** pending" + (" (revised, approvals cleared)" if revised else "")
    text = (
        f"{head} · {summarize(ctx, a)} · needs one {rule.approvers} approver in {room}. "
        f"`@{ctx.executor} approve {a.id}` / `deny {a.id} <reason>` / `changes {a.id} <note>`"
    )
    review = ctx.view.latest_review(a.id)
    if review is not None:
        text += "\n" + review_text(ctx, a, review[0], review[1], review[2])
    return text


def review_text(ctx: Context, a: Artifact, r: Review, reviewer_id: str, reviewer_name: str) -> str:
    who = ctx.name_of(reviewer_id, reviewer_name)
    return (
        f"**{a.id}** last review: **{r.met} of {r.total}** criteria met at "
        f"`{short_sha(r.head_sha)}` by {who}."
    )


# ---------------------------------------------------------------------------------------------
# reviews (§6.3, §7.1)
# ---------------------------------------------------------------------------------------------


def _handle_review(ctx: Context, ev: MessageEvent) -> list[Effect]:
    if Kind.PR not in ctx.kinds:
        return [
            Post(
                ev.room_id,
                f"Review not recorded — reviews go to @{GITHUB_EXECUTOR}.",
                thread_id=_reply_thread(ev),
            ),
            _audit(ev, "rejected_review:wrong_executor"),
        ]
    reviewer = ctx.policy.proposer_id(REVIEWER)
    if not reviewer or ev.sender != reviewer:
        return [
            Post(
                ev.room_id,
                f"Review not recorded — only {REVIEWER} posts reviews.",
                thread_id=_reply_thread(ev),
            ),
            _audit(ev, "rejected_review:bad_sender"),
        ]
    review, reason = parse_review(ev.body)
    if review is None:
        return [
            Post(ev.room_id, f"Review not recorded — malformed: {reason}.", _reply_thread(ev)),
            _audit(ev, "rejected_review:malformed"),
        ]
    a = ctx.view.get(review.pr)
    if a is None or a.kind is not Kind.PR:
        return [
            Post(
                ev.room_id, f"Review not recorded — {review.pr} is unknown here.", _reply_thread(ev)
            ),
            _audit(ev, "rejected_review:unknown", review.pr),
        ]
    if a.room_id != ev.room_id:
        return [
            Post(
                ev.room_id,
                f"Review not recorded — {a.id} lives in {ctx.room_name(a.room_id)}.",
                _reply_thread(ev),
            ),
            _audit(ev, "rejected_review:wrong_room", a.id),
        ]
    stale = review.head_sha != a.facts().get("head_sha")
    text = review_text(ctx, a, review, ev.sender, ev.sender_name)
    if stale:
        text += f" The pending head is `{short_sha(a.facts().get('head_sha'))}`."
    return [
        RecordReview(a.id, review, ev.sender, ev.sender_name),
        _audit(
            ev,
            "review_recorded",
            a.id,
            head_sha=review.head_sha,
            met=review.met,
            total=review.total,
        ),
        Post(ev.room_id, text, thread_id=a.thread_id),
    ]


# ---------------------------------------------------------------------------------------------
# commands (§9) and the gates (§7.1 A1–A7)
# ---------------------------------------------------------------------------------------------


def _handle_command(ctx: Context, ev: MessageEvent) -> list[Effect]:
    command = cmd.parse_command(ev.body, ctx.executor_names)
    thread = _reply_thread(ev)
    match command:
        case None:
            if cmd.is_bare_mention(ev.body, ctx.executor_names):
                return [Post(ev.room_id, cmd.HELP_TEXT, thread_id=thread)]
            return [_audit(ev, "ignored")]  # §9: anything else is ignored silently
        case cmd.Help():
            return [Post(ev.room_id, cmd.HELP_TEXT, thread_id=thread)]
        case cmd.Whoami():
            groups = ", ".join(ctx.policy.groups_of(ev.sender)) or "none"
            role = ctx.policy.role_of(ev.sender)
            tail = f" · agent role {role}" if role else f" · groups: {groups}"
            who = ev.sender_name or "you"
            return [
                Post(ev.room_id, f"{who}: participant id `{ev.sender}`{tail}", thread_id=thread)
            ]
        case cmd.Status(artifact_id=None):
            return [Post(ev.room_id, _status_room(ctx, ev.room_id), thread_id=thread)]
        case cmd.Status(artifact_id=aid):
            a = ctx.view.get(aid)
            if a is None or a.kind not in ctx.kinds:
                return [Post(ev.room_id, f"**{aid}** is unknown here.", thread_id=thread)]
            return [Post(ev.room_id, _status_one(ctx, a), thread_id=thread)]
        case cmd.Board():
            if ctx.executor != SWITCH_EXECUTOR:
                return [
                    Post(
                        ev.room_id,
                        f"The board is kept by @{SWITCH_EXECUTOR} in "
                        f"{ctx.room_names.get('product', '#product')}.",
                        thread_id=thread,
                    )
                ]
            return _post_board(ctx, ev)
        case cmd.Approve(artifact_id=aid):
            return _approve(ctx, ev, aid)
        case cmd.Deny(artifact_id=aid, reason=reason):
            return _deny(ctx, ev, aid, reason)
        case cmd.Changes(artifact_id=aid, note=note):
            return _changes(ctx, ev, aid, note)
    return []  # pragma: no cover


def _refuse(ev: MessageEvent, code: str, text: str, aid: str | None, **d: Any) -> list[Effect]:
    return [Post(ev.room_id, text, thread_id=_reply_thread(ev)), _audit(ev, code, aid, **d)]


def _gate(
    ctx: Context, ev: MessageEvent, aid: str, verb: str, *, allow_changes_requested: bool = False
) -> tuple[Artifact | None, Rule | None, list[Effect]]:
    """A2–A5, shared by approve, deny and changes. Returns (artifact, rule, refusal effects)."""
    a = ctx.view.get(aid)
    if a is None or a.kind not in ctx.kinds:
        owner = EXECUTOR_OF_KIND.get((aid.split("-", 1)[0] or "").lower().replace("rel", "release"))
        hint = f" Try @{owner}." if owner and owner != ctx.executor else ""
        return (
            None,
            None,
            _refuse(ev, f"refused_{verb}:unknown", f"**{aid}** is unknown here.{hint}", aid),
        )
    rule = ctx.policy.rule(a.rule)
    if rule is None:
        return (
            None,
            None,
            _refuse(
                ev, f"refused_{verb}:no_rule", f"**{aid}** names a rule that no longer exists.", aid
            ),
        )
    name = ctx.name_of(ev.sender, ev.sender_name)
    # A3 — only humans authorize; an agent's command is refused whatever it says
    if ev.sender in ctx.agent_ids:
        return (
            None,
            None,
            _refuse(
                ev,
                f"refused_{verb}:agent",
                f"**{aid}** not applied — @{name} is an agent, not an approver.",
                aid,
            ),
        )
    # A2 — the group the rule names, by participant id
    if not ctx.policy.in_group(ev.sender, rule.approvers):
        return (
            None,
            None,
            _refuse(
                ev,
                f"refused_{verb}:not_approver",
                f"**{aid}** not applied — {name} is not in the {rule.approvers} group.",
                aid,
                group=rule.approvers,
            ),
        )
    # A5 — the room the rule names
    room_ok = (
        ev.room_id == a.room_id
        if rule.room == FEATURE_ROOM
        else ev.room_id == ctx.policy.room_id(rule.room)
    )
    if not room_ok:
        return (
            None,
            None,
            _refuse(
                ev,
                f"refused_{verb}:wrong_room",
                f"**{aid}** not applied — I take {a.kind.value} commands in "
                f"{_rule_room_name(ctx, rule, a)} only.",
                aid,
            ),
        )
    # A4 — pending, and not past its TTL
    if a.state in WAITING_STATES and a.expires_at <= ctx.now:
        return (
            None,
            None,
            [
                *_expire(ctx, a),
                *_refuse(
                    ev,
                    f"refused_{verb}:not_pending",
                    f"**{aid}** is expired, not pending.",
                    aid,
                    state="expired",
                ),
            ],
        )
    ok_states = (
        {State.PENDING, State.CHANGES_REQUESTED} if allow_changes_requested else {State.PENDING}
    )
    if a.state not in ok_states:
        return (
            None,
            None,
            _refuse(
                ev,
                f"refused_{verb}:not_pending",
                f"**{aid}** is {a.state.value}, not pending.",
                aid,
                state=a.state.value,
            ),
        )
    return a, rule, []


def _self_check(ctx: Context, ev: MessageEvent, a: Artifact) -> list[Effect]:
    """A3, second half: not the proposer, and not the PR author when the author is a person."""
    name = ctx.name_of(ev.sender, ev.sender_name)
    if ev.sender == a.proposer_id:
        return _refuse(
            ev, "refused_approval:self", f"**{a.id}** not applied — {name} proposed it.", a.id
        )
    if a.kind is Kind.PR:
        author = a.facts().get("author_login") or ""
        if author and ctx.users.id_for_login(author) == ev.sender:
            return _refuse(
                ev,
                "refused_approval:self",
                f"**{a.id}** not applied — {name} opened this pull request.",
                a.id,
                author_login=author,
            )
    return []


def _approve(ctx: Context, ev: MessageEvent, aid: str) -> list[Effect]:
    a, rule, refusal = _gate(ctx, ev, aid, "approval")  # A1 is the regex that got us here
    if a is None or rule is None:
        return refusal
    refusal = _self_check(ctx, ev, a)
    if refusal:
        return refusal
    name = ctx.name_of(ev.sender, ev.sender_name)
    # A6 — snapshot unchanged: re-read from the platform (or recomputed from policy for a spec)
    proposal = a.proposal()
    live = _live_facts(ctx, a, proposal, rule)
    if not live.ok:
        return _refuse(
            ev,
            "refused_approval:no_facts",
            f"**{aid}** not applied — could not re-read {proposal.target}: {live.error}.",
            aid,
        )
    snapshot = make_snapshot(a.id, a.proposer_id, proposal, live.data)
    if snapshot.hash != a.snapshot_sha:
        what = _what_changed(a.facts(), live.data)
        return [
            UpdateSnapshot(
                a.id,
                proposal_json=a.proposal_json,
                snapshot_json=snapshot.canonical(),
                snapshot_sha=snapshot.hash,
            ),
            _audit(
                ev, "snapshot_changed", aid, what=what, previous=a.snapshot_sha, now=snapshot.hash
            ),
            Post(
                ev.room_id,
                f"**{aid}** changed since it was proposed ({what}). "
                "Approvals cleared; it is pending again.",
                thread_id=a.thread_id,
            ),
            *_refuse(
                ev,
                "refused_approval:snapshot_changed",
                f"**{aid}** not applied — it changed since it was proposed; "
                "review the new snapshot and approve again.",
                aid,
            ),
        ]
    # A7 — recorded; approved → executing/opening written before any network call (§4.3 rule 4)
    return [
        RecordApproval(a.id, ev.sender, ev.room_id, ev.message_id, a.snapshot_sha, "approve"),
        Transition(
            a.id,
            State.APPROVED,
            {
                "approver_id": ev.sender,
                "approver_name": name,
                "approved_at": ctx.now,
                "approval_message_id": ev.message_id,
            },
        ),
        Transition(a.id, RUNNING_OF[a.kind], {"attempts": a.attempts + 1}),
        _audit(ev, "approved", aid, snapshot_sha=a.snapshot_sha, group=rule.approvers),
        Execute(a.id),
    ]


def _deny(ctx: Context, ev: MessageEvent, aid: str, reason: str | None) -> list[Effect]:
    a, rule, refusal = _gate(ctx, ev, aid, "deny", allow_changes_requested=True)
    if a is None or rule is None:
        return refusal
    name = ctx.name_of(ev.sender, ev.sender_name)
    suffix = f": {reason}" if reason else ""
    return [
        RecordApproval(a.id, ev.sender, ev.room_id, ev.message_id, a.snapshot_sha, "deny", reason),
        Transition(
            a.id,
            State.DENIED,
            {"approver_id": ev.sender, "approver_name": name, "note": reason},
        ),
        _audit(ev, "denied", aid, reason=reason),
        Post(
            ev.room_id,
            f"**{aid}** denied by {name}{suffix}. @{ctx.name_of(a.proposer_id)} nothing happens.",
            thread_id=a.thread_id,
        ),
        *_status(ctx, a, State.DENIED),
    ]


def _changes(ctx: Context, ev: MessageEvent, aid: str, note: str) -> list[Effect]:
    a, rule, refusal = _gate(ctx, ev, aid, "changes")
    if a is None or rule is None:
        return refusal
    name = ctx.name_of(ev.sender, ev.sender_name)
    return [
        RecordApproval(a.id, ev.sender, ev.room_id, ev.message_id, a.snapshot_sha, "changes", note),
        Transition(
            a.id,
            State.CHANGES_REQUESTED,
            {"approver_id": ev.sender, "approver_name": name, "note": note},
        ),
        _audit(ev, "changes_requested", aid, note=note),
        Post(
            ev.room_id,
            f"**{aid}** sent back by {name}: {note} "
            f"@{ctx.name_of(a.proposer_id)} revise and re-propose in this thread; the id stays.",
            thread_id=a.thread_id,
        ),
        *_status(ctx, a, State.CHANGES_REQUESTED),
    ]


def _expire(ctx: Context, a: Artifact) -> list[Effect]:
    rule = ctx.policy.rule(a.rule)
    ttl = rule.pending_ttl if rule else "its TTL"
    return [
        Transition(a.id, State.EXPIRED),
        Audit("expired", a.id, details={"after": ttl}),
        Post(
            a.room_id,
            f"**{a.id}** expired after {ttl} without a decision. "
            f"@{ctx.name_of(a.proposer_id)} re-propose if still wanted.",
            thread_id=a.thread_id,
        ),
        *_status(ctx, a, State.EXPIRED),
    ]


# ---------------------------------------------------------------------------------------------
# continuations after the adapter ran
# ---------------------------------------------------------------------------------------------


def after_execute(ctx: Context, a: Artifact, result: ActionResult, verified: bool) -> list[Effect]:
    if a.state is not RUNNING_OF[a.kind]:
        return [Audit("execute_result_ignored", a.id, details={"state": a.state.value})]
    name = a.approver_name or ctx.name_of(a.approver_id or "")
    if result.ok and verified:
        return [
            Transition(
                a.id,
                SUCCESS_OF[a.kind],
                {"result_json": json.dumps(result.data, sort_keys=True), "error": None},
            ),
            Audit(
                "applied",
                a.id,
                actor_id=a.approver_id,
                actor_name=a.approver_name,
                details={
                    "action": a.proposal().action,
                    "snapshot_sha": a.snapshot_sha,
                    **result.data,
                },
            ),
            Post(
                a.room_id,
                f"**{a.id}** approved by {name} · {action_phrase(a, result.data)} · "
                f"verified at **{ctx.now:%H:%M:%S} UTC**.",
                thread_id=a.thread_id,
            ),
            *_spec_opened(ctx, a, result.data),
            *_status(ctx, a, SUCCESS_OF[a.kind]),
        ]
    error = result.error or ("not visible on read-back" if result.ok else "unknown platform error")
    action = a.proposal().action.replace("_", " ")
    failed = FAILURE_OF[a.kind]
    if a.kind is Kind.SPEC or a.attempts < ctx.max_attempts:
        wait = int(ctx.retry_after.total_seconds() // 60)
        return [
            Transition(a.id, failed, {"error": error, "failed_at": ctx.now}),
            Audit("action_failed", a.id, details={"error": error, "attempt": a.attempts}),
            Post(
                a.room_id,
                f"**{a.id}** approved by {name} but {action} failed: {error}. "
                f"Retrying once in {wait} min.",
                thread_id=a.thread_id,
            ),
            *_status(ctx, a, failed),
        ]
    return [
        Transition(a.id, failed, {"error": error, "failed_at": ctx.now}),
        Transition(a.id, State.FAILED),
        Audit(
            "action_failed", a.id, details={"error": error, "attempt": a.attempts, "final": True}
        ),
        Post(
            a.room_id,
            f"**{a.id}** approved by {name} but {action} failed again: {error}. "
            f"Not retried; @{ctx.name_of(a.proposer_id)} a new proposal is required.",
            thread_id=a.thread_id,
        ),
        *_status(ctx, a, State.FAILED),
    ]


def _spec_opened(ctx: Context, a: Artifact, result: dict[str, Any]) -> list[Effect]:
    """§7.2: post the room's existence in #product with its members, and the spec into the new
    room as its first message (the brief every agent in it reads; Appendix C)."""
    if a.kind is not Kind.SPEC:
        return []
    plan = a.facts()
    people = ", ".join(ctx.name_of(p) for p in plan.get("humans", []))
    agents = ", ".join(ctx.name_of(p) for p in plan.get("agents", []))  # no mentions: not in here
    room = result.get("room_name") or a.target
    effects: list[Effect] = [
        Post(
            a.room_id,
            f"**{a.id}** feature room **#{room.lstrip('#')}** is open · people: {people} · "
            f"agents: {agents} · instructions: the spec. The merge is approved there, by "
            f"{ctx.name_of(a.proposal().body.engineer)}, with `approve PR-n` to {GITHUB_EXECUTOR}.",
            thread_id=a.thread_id,
        )
    ]
    new_room = result.get("room_id")
    if new_room:
        body: Any = a.proposal().body
        engineer = ctx.name_of(body.engineer)
        approver = ctx.name_of(a.approver_id or "")
        effects.append(
            Post(
                str(new_room),
                f"@{CODER} **{a.id}** instructions · approved by {approver} · engineer {engineer} "
                f"approves the merge with `@{GITHUB_EXECUTOR} approve PR-n`.\n"
                f"{spec_instructions(a.proposal())}",
            )
        )
    return effects


def after_verify(ctx: Context, a: Artifact, v: VerifyResult) -> list[Effect]:
    """Crash recovery (§4.3 rule 4): the state said executing; what does the platform say?"""
    if a.state is not RUNNING_OF[a.kind]:
        return [Audit("verify_result_ignored", a.id, details={"state": a.state.value})]
    if v.present is None:
        return [Audit("recovery_verify_error", a.id, details={"error": v.error or "unknown"})]
    if v.present:
        return after_execute(ctx, a, ActionResult(ok=True, data=v.data), True)
    # the action never landed: run it now (the write is idempotent on the platform side)
    return [Audit("recovery_execute", a.id), Execute(a.id)]


# ---------------------------------------------------------------------------------------------
# derived spec stages and the board (§7.3, §14) — sindarin-switch only
# ---------------------------------------------------------------------------------------------


def _spec_stages(ctx: Context) -> list[Effect]:
    """open → done when a merged PR names the spec; done → released when a published release
    lists it. Read from the other executor's state, never from its messages (§4.3 rule 6)."""
    effects: list[Effect] = []
    merged = {
        a.proposal().body.spec: a.id  # type: ignore[union-attr]
        for a in ctx.peer_artifacts
        if a.kind is Kind.PR and a.state is State.MERGED
    }
    released: dict[str, str] = {}
    for a in ctx.peer_artifacts:
        if a.kind is Kind.RELEASE and a.state is State.PUBLISHED:
            for s in a.proposal().body.specs:  # type: ignore[union-attr]
                released.setdefault(s, a.id)
    for spec in ctx.view.in_states(State.OPEN, kind=Kind.SPEC):
        if spec.id in merged:
            effects += [
                Transition(spec.id, State.DONE),
                Audit("spec_done", spec.id, details={"pr": merged[spec.id]}),
                Post(
                    spec.room_id, f"**{spec.id}** done · {merged[spec.id]} merged.", spec.thread_id
                ),
            ]
    for spec in ctx.view.in_states(State.DONE, kind=Kind.SPEC):
        if spec.id in released:
            effects += [
                Transition(spec.id, State.RELEASED),
                Audit("spec_released", spec.id, details={"release": released[spec.id]}),
                Post(
                    spec.room_id,
                    f"**{spec.id}** released in {released[spec.id]}.",
                    spec.thread_id,
                ),
            ]
    return effects


def _daily_board(ctx: Context) -> list[Effect]:
    today = ctx.now.strftime("%Y-%m-%d")
    if ctx.view.get_value("board_date") == today or not ctx.policy.room_id("product"):
        return []
    return [
        SetValue("board_date", today),
        Post(ctx.policy.room_id("product"), board_text(ctx)),
        Audit("board_posted", details={"date": today}),
    ]


def _post_board(ctx: Context, ev: MessageEvent) -> list[Effect]:
    today = ctx.now.strftime("%Y-%m-%d")
    room = ctx.policy.room_id("product") or ev.room_id
    return [
        SetValue("board_date", today),
        Post(room, board_text(ctx)),
        _audit(ev, "board_posted", date=today),
    ]


def _age(ctx: Context, a: Artifact) -> str:
    seconds = int((ctx.now - a.created_at).total_seconds())
    if seconds < 3600:
        return f"{max(seconds // 60, 0)}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _waiting_on(ctx: Context, a: Artifact) -> str:
    rule = ctx.policy.rule(a.rule)
    if a.state in WAITING_STATES and rule is not None:
        if a.state is State.CHANGES_REQUESTED:
            return f"{ctx.name_of(a.proposer_id)} (revise)"
        names = ", ".join(ctx.name_of(p) for p in ctx.policy.group_members(rule.approvers))
        return f"{rule.approvers}: {names or 'nobody configured'}"
    if a.state in RETRY_STATES:
        return "retry"
    if a.state in IN_FLIGHT_STATES:
        return "executor"
    if a.kind is Kind.SPEC and a.state is State.OPEN:
        return f"{CODER} and {ctx.name_of(a.proposal().body.engineer)}"  # type: ignore[union-attr]
    if a.kind is Kind.SPEC and a.state is State.DONE:
        return "release"
    return "-"


def board_text(ctx: Context) -> str:
    """§14: two sections, numbers from the executors' state, no commentary."""
    live = {State.OPEN, State.DONE} | WAITING_STATES | IN_FLIGHT_STATES | RETRY_STATES
    own = [a for a in ctx.view.in_states(*live) if a.kind in ctx.kinds]
    peers = [a for a in ctx.peer_artifacts if a.state in live]
    rows = sorted(own + peers, key=lambda a: a.created_at)
    lines = [f"**Board** · {ctx.now:%Y-%m-%d %H:%M} UTC", "**In flight**"]
    if not rows:
        lines.append("nothing in flight")
    for a in rows:
        owner = ctx.name_of(a.approver_id) if a.approver_id else ctx.name_of(a.proposer_id)
        lines.append(
            f"{a.id} · {a.state.value} · {owner} · {_age(ctx, a)} · "
            f"waiting on {_waiting_on(ctx, a)}"
        )
    lines.append("**Waiting on**")
    waiting = [a for a in rows if a.state is State.PENDING]
    if not waiting:
        lines.append("no pending approvals")
    for a in waiting:
        rule = ctx.policy.rule(a.rule)
        room = _rule_room_name(ctx, rule, a) if rule else ctx.room_name(a.room_id)
        lines.append(f"{a.id} · {room} · {_waiting_on(ctx, a)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------------------------


def _status_room(ctx: Context, room_id: str) -> str:
    live = WAITING_STATES | IN_FLIGHT_STATES | RETRY_STATES | {State.OPEN, State.DONE}
    rows = [a for a in ctx.view.in_states(*live) if a.kind in ctx.kinds and a.room_id == room_id]
    if not rows:
        return "No open artifacts in this room."
    out = []
    for a in rows:
        extra = ""
        if a.state is State.PENDING:
            extra = f"awaiting approval until {fmt_dt(a.expires_at)}"
        elif a.state is State.CHANGES_REQUESTED:
            extra = f"changes requested: {a.note}"
        elif a.state in RETRY_STATES:
            extra = f"failed: {a.error} · retrying"
        else:
            extra = "in progress"
        out.append(f"**{a.id}** · {a.state.value} · {summarize(ctx, a)} · {extra}")
    return "\n".join(out)


def _status_one(ctx: Context, a: Artifact) -> str:
    lines = [
        f"**{a.id}** · {a.state.value} · {summarize(ctx, a)} · rule {a.rule}",
        f"proposed by {ctx.name_of(a.proposer_id, a.proposer_name)} at {fmt_dt(a.created_at)} · "
        f"snapshot sha256 `{a.snapshot_sha}` · revision {a.seq}",
    ]
    if a.approver_name and a.approved_at:
        lines.append(f"approved by {a.approver_name} at {fmt_dt(a.approved_at)}")
    if a.state in WAITING_STATES:
        lines.append(f"expires {fmt_dt(a.expires_at)}")
    if a.note:
        lines.append(f"note: {a.note}")
    if a.error:
        lines.append(f"error: {a.error}")
    review = ctx.view.latest_review(a.id)
    if review is not None:
        lines.append(review_text(ctx, a, *review))
    if a.result_json:
        lines.append(action_phrase(a, a.result()))
    return "\n".join(lines)
