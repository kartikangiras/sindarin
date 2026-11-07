"""Pure pipelines: events in, Effects out (design v5 §5). No I/O in this module, ever.

Inputs are a MessageEvent (parsed), a read-only View of the store, the policy and users files,
the facts the runner read from the platform, and a clock. The only text interpreted here is the
regex table in commands.py (§9) and the JSON inside ```sindarin-proposal
fences (§8). Nothing else is input. Natural language is never interpreted.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from pydantic import ValidationError

from sindarin.adapters.base import ActionResult, Facts, VerifyResult
from sindarin.executor import commands as cmd
from sindarin.names import (
    ANALYST,
    BOOKKEEPER,
    EXECUTOR_OF_KIND,
    LEDGER_EXECUTOR,
    PLANNER,
    SWITCH_EXECUTOR,
    TREASURY,
)
from sindarin.policy import (
    PERIOD_ROOM,
    Policy,
    Rule,
    check_facts,
    check_proposer,
    check_target,
)
from sindarin.schema import (
    AWAITING_STATES,
    FAILURE_OF,
    HEAD_OF,
    IN_FLIGHT_STATES,
    PLATFORM_OF,
    PROPOSAL_FENCE,
    RETRY_STATES,
    ROOM_KINDS,
    RUNNING_OF,
    STANDALONE_KINDS,
    SUCCESS_OF,
    WAITING_STATES,
    Artifact,
    Kind,
    Proposal,
    State,
    StatusFence,
    extract_fences,
    fmt_dt,
    has_fence,
    make_snapshot,
    money,
    percent,
    sha256_hex,
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


@dataclass(frozen=True)
class ConfirmPaid:
    """Runner: the bank's confirmation for a sent run → adapter.confirm → core.after_confirm()."""

    artifact_id: str
    reference: str
    room_id: str
    thread_id: str | None
    actor_id: str
    actor_name: str


@dataclass(frozen=True)
class PollPayment:
    """Runner: ask the rail where a sent run stands → core.after_confirm() once it is paid."""

    artifact_id: str


@dataclass(frozen=True)
class PollInvoice:
    """Runner: ask the biller whether a won deal's invoice is paid → core.after_collect()."""

    artifact_id: str


@dataclass(frozen=True)
class CrmSync:
    """Runner: the deal into the CRM at its stage (open when proposed, won, lost)."""

    artifact_id: str
    stage: str


@dataclass(frozen=True)
class SendContract:
    """Runner: the won deal's order form out for signature."""

    artifact_id: str


@dataclass(frozen=True)
class PollSignature:
    """Runner: whether the order form came back signed."""

    artifact_id: str


@dataclass(frozen=True)
class BuildPack:
    """Runner (sindarin-switch): the month's board pack into Drive and Slides, links back."""

    period: str
    room_id: str
    thread_id: str | None
    actor_id: str
    actor_name: str
    auto: bool = False


@dataclass(frozen=True)
class RunReport:
    """Runner: sindarin-switch renders a report, an export or a chart from its read-only ledger
    handle and posts it, a file through Switch attachments when it can."""

    what: str  # report | export | chart
    period: str
    subject: str  # the table or the chart kind, "" for a report
    room_id: str
    thread_id: str | None
    actor_id: str
    actor_name: str


@dataclass(frozen=True)
class BookSale:
    """Runner: the ledger adapter books a late sale a person typed, then posts the outcome."""

    invoice: str
    customer: str
    amount_cents: int
    room_id: str
    thread_id: str | None
    actor_id: str
    actor_name: str


@dataclass(frozen=True)
class SetPrice:
    """Runner: the ledger adapter edits a list price a person typed, then posts the outcome."""

    product: str
    amount_cents: int
    room_id: str
    thread_id: str | None
    actor_id: str
    actor_name: str


@dataclass(frozen=True)
class RunAudit:
    """Runner: the audit trail from the store(s): one artifact, or a month across both executors
    when sindarin-switch answers in the finance room."""

    target: str  # artifact id or period
    with_peer: bool
    room_id: str
    thread_id: str | None
    actor_id: str
    actor_name: str


@dataclass(frozen=True)
class InspectFile:
    """Runner: fetch the file a person linked, describe it (or list one tab's entries), post."""

    url: str
    tab: str
    room_id: str
    thread_id: str | None
    actor_id: str
    actor_name: str


@dataclass(frozen=True)
class LoadBooks:
    """Runner: fetch the file a person linked and, if the books can take it, load it."""

    url: str
    room_id: str
    thread_id: str | None
    actor_id: str
    actor_name: str


Effect = (
    Post
    | Audit
    | RecordMessage
    | CreateArtifact
    | Transition
    | UpdateSnapshot
    | RecordApproval
    | Execute
    | Verify
    | SetValue
    | BookSale
    | SetPrice
    | InspectFile
    | LoadBooks
    | RunAudit
    | RunReport
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
    def get_value(self, key: str) -> str | None: ...


@dataclass(frozen=True)
class Context:
    executor: str  # handle: sindarin-switch | sindarin-ledger
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
    peer_artifacts: tuple[Artifact, ...] = ()  # the other executor's state (board, stages)

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
    def ledger_executor_id(self) -> str:
        """sindarin-ledger's participant id: from policy, else the one configured peer."""
        pid = self.policy.executor_id(LEDGER_EXECUTOR)
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


def summarize(ctx: Context, a: Artifact) -> str:
    """One line a person reads in the pending template: ids and money, never digests."""
    body: Any = a.proposal().body
    facts = a.facts()
    if a.kind is Kind.GOALS:
        return (
            f"{body.title} · revenue target {money(body.revenue_target_cents, body.currency)} · "
            f"margin {percent(body.margin_target_bps)} · cost cap "
            f"{money(body.cost_cap_cents, body.currency)}"
        )
    if a.kind is Kind.BUDGET:
        n = len(body.lines)
        cap = facts.get("cap_cents")
        under = f" under the cap {money(int(cap), body.currency)}" if cap is not None else ""
        total = money(body.total_cents, body.currency)
        return f"{n} line{'s' if n != 1 else ''} · {total}{under} · {body.goals}"
    if a.kind is Kind.ENTRIES:
        n = len(body.lines)
        total = money(body.total_cents)
        return f"period {body.period} · {n} line{'s' if n != 1 else ''} · {total} · {body.budget}"
    if a.kind is Kind.CLOSE:
        n = len(body.payables)
        pay = (
            f"{n} payable{'s' if n != 1 else ''} {money(body.total_payables_cents, body.currency)}"
            if n
            else "no payables"
        )
        e = len(body.entries)
        return f"{body.title} · {e} adjusting entr{'y' if e == 1 else 'ies'} · {pay}"
    if a.kind is Kind.PAY:
        total = int(facts.get("total_cents", body.total_cents))
        n = len(body.run)
        return f"{n} payable{'s' if n != 1 else ''} · {money(total, body.currency)} · {body.plan}"
    if a.kind is Kind.DEAL:
        return (
            f"{body.deal_id} · {body.account} · {body.product} at "
            f"{money(body.price_cents, body.currency)} (list "
            f"{money(body.list_cents, body.currency)}, {percent(body.discount_bps)} off) · "
            f"{body.period}"
        )
    e = len(body.entries)
    tail = f" · after {body.pay_run}" if body.pay_run else ""
    return f"period {body.period} · {e} entr{'y' if e == 1 else 'ies'} · {body.plan}{tail}"


def action_phrase(a: Artifact, result: dict[str, Any]) -> str:
    body: Any = a.proposal().body
    if a.kind is Kind.DEAL:
        phrase = f"won · sale {money(body.price_cents, body.currency)} posted to {body.period}"
        if result.get("invoice_id"):
            phrase += (
                f" · invoice **{result['invoice_id']}** sent, due in "
                f"{int(result.get('due_days', 30))} days"
            )
        elif result.get("invoice_error"):
            phrase += f" · invoice not sent ({result['invoice_error']})"
        return phrase
    if a.kind is Kind.PAY:
        total = int(result.get("total_cents", body.total_cents))
        n = len(body.run)
        payables = f"{n} payable{'s' if n != 1 else ''}"
        if int(result.get("paid", 0)) >= n or "sent" not in result:
            tail = f" · reference **{result['reference']}**" if result.get("reference") else ""
            via = ""
            if result.get("rail_confirms") and result.get("rail"):
                via = f" via {result['rail']}"
            if result.get("balance_after_cents") is not None:
                after = money(int(result["balance_after_cents"]), body.currency)
                tail += f" · operating balance {after}"
            return f"paid {money(total, body.currency)}{via} · {payables}{tail}"
        rail = str(result.get("rail") or "the bank")
        doc = f" · file **{result['document']}**" if result.get("document") else ""
        return f"sent to {rail} · {payables} · {money(total, body.currency)}{doc}"
    if a.kind is Kind.LOCK:
        n = int(result.get("entries_posted", len(body.entries)))
        return f"{n} entr{'y' if n == 1 else 'ies'} posted · period {body.period} locked"
    if a.kind is Kind.BUDGET:
        n = int(result.get("lines", len(body.lines)))
        return (
            f"recorded {money(body.total_cents, body.currency)} · {n} line{'s' if n != 1 else ''}"
        )
    if a.kind is Kind.ENTRIES:
        n = int(result.get("lines_posted", len(body.lines)))
        return f"{n} budget line{'s' if n != 1 else ''} posted · period {body.period} planned"
    room = result.get("room_name") or a.target
    return f"room **#{room.lstrip('#')}** opened"


def _room_plan(ctx: Context, proposal: Proposal, rule: Rule) -> dict[str, Any]:
    """The membership a room-opening approval binds to (design v5 §3): the approvers, the
    group the rule also invites, the two agents of the chain, sindarin-ledger and
    sindarin-switch. Computed from policy, never from chat text."""
    humans = list(ctx.policy.group_members(rule.approvers))
    if rule.also_invite:
        humans += [p for p in ctx.policy.group_members(rule.also_invite) if p not in humans]
    roles = (PLANNER, BOOKKEEPER) if proposal.kind is Kind.GOALS else (BOOKKEEPER, TREASURY)
    agents = [
        pid
        for pid in (
            *(ctx.policy.proposer_id(r) for r in roles),
            ctx.ledger_executor_id,
            ctx.self_id,  # so the brief can be posted into the new room
        )
        if pid
    ]
    return {
        "slug": proposal.target,
        "humans": humans,
        "agents": agents,
        "instructions_sha": sha256_hex(plan_instructions(proposal)),
    }


_goals_plan = _room_plan  # the planning chain's name for it


def plan_instructions(proposal: Proposal) -> str:
    """The room instructions sindarin-switch sets on the room it opens: the goals of a plan
    room, the month's plan of a period room."""
    body: Any = proposal.body
    cur = body.currency
    lines = [f"# {body.title}", "", body.summary, ""]
    if proposal.kind is Kind.CLOSE:
        if body.report:
            lines += ["## The month", *[f"- {r}" for r in body.report], ""]
        lines.append("## Adjusting entries to post")
        if body.entries:
            lines += [
                f"{i}. {e.memo} · {money(e.amount_cents, cur)}"
                for i, e in enumerate(body.entries, 1)
            ]
        else:
            lines.append("none")
        lines += ["", "## Payables to pay"]
        if body.payables:
            lines += [f"- {inv}" for inv in body.payables]
            lines.append(f"Total {money(body.total_payables_cents, cur)}")
        else:
            lines.append("none")
        lines += [
            "",
            f"Payment runs are approved in #payments with `@{LEDGER_EXECUTOR} approve PAY-n`; "
            f"the lock of {body.period} is approved here with `@{LEDGER_EXECUTOR} approve "
            "LOCK-n`. Agents propose; only the named groups approve; the executor acts.",
        ]
        return "\n".join(lines)
    if body.report:
        lines += [f"## The basis: {body.basis}", *[f"- {r}" for r in body.report], ""]
    lines += [
        f"## Goals for {body.period}",
        f"- Revenue target {money(body.revenue_target_cents, cur)}",
        f"- Margin target {percent(body.margin_target_bps)}",
        f"- Cost cap {money(body.cost_cap_cents, cur)}: the budget's lines sum under it",
        "",
        f"The budget is approved in #planning with `@{LEDGER_EXECUTOR} approve BUDGET-n`; its "
        f"register entries into the books of {body.period} are approved here with "
        f"`@{LEDGER_EXECUTOR} approve ENTRIES-n`. Agents propose; only the named groups approve; "
        "the executor acts.",
    ]
    return "\n".join(lines)


def _goals_of(ctx: Context, proposal: Proposal) -> dict[str, Any]:
    """The approved goals a budget answers, as the executor recorded them: the cap and the
    targets (never from the fence). Empty when no such goals are open."""
    body: Any = proposal.body
    goals = _plan_named(ctx, body.goals)
    if goals is None or goals.state not in (State.OPEN, State.DONE):
        return {}
    g: Any = goals.proposal().body
    return {
        "goals": goals.id,
        "cap_cents": int(g.cost_cap_cents),
        "revenue_target_cents": int(g.revenue_target_cents),
        "margin_target_bps": int(g.margin_target_bps),
    }


def _live_facts(ctx: Context, a: Artifact, proposal: Proposal, rule: Rule) -> Facts:
    if a.kind in ROOM_KINDS:
        return Facts(ok=True, data=_room_plan(ctx, proposal, rule))
    f = ctx.facts.get(proposal.target)
    if f is None:
        return Facts(ok=False, error="no fresh read of the target")
    if f.ok and a.kind is Kind.BUDGET:
        return Facts(ok=True, data={**f.data, **_goals_of(ctx, proposal)})
    return f


_MONEY_LABELS = {
    "revenue_cents": "revenue",
    "costs_cents": "costs",
    "cap_cents": "cost cap",
    "total_cents": "total",
    "revenue_target_cents": "revenue target",
    "list_cents": "list price",
}
_PLAIN_LABELS = {
    "sales_rows": "sales rows",
    "period_status": "period",
    "basis_status": "basis",
    "open_payables": "open payables",
    "deal_stage": "deal",
}


def _what_changed(old: dict[str, Any], new: dict[str, Any], currency: str = "USD") -> str:
    """What moved between the snapshot and the ledger now, in the words a person reads."""
    parts: list[str] = []
    if "payables" in old or "payables" in new:
        before = {r["invoice"]: r for r in old.get("payables", [])}
        after = {r["invoice"]: r for r in new.get("payables", [])}
        for inv in sorted(set(before) | set(after)):
            b, n = before.get(inv), after.get(inv)
            if b == n:
                continue
            if b is None:
                parts.append(f"{inv} appeared")
            elif n is None:
                parts.append(f"{inv} gone")
            elif b["amount_cents"] != n["amount_cents"]:
                was, now = money(b["amount_cents"], currency), money(n["amount_cents"], currency)
                parts.append(f"{inv} {was} → {now}")
            elif b["status"] != n["status"]:
                parts.append(f"{inv} {b['status']} → {n['status']}")
            else:
                parts.append(f"{inv} changed")
    if "budget_lines" in old or "budget_lines" in new:
        before = {r["department"]: r for r in old.get("budget_lines", [])}
        after = {r["department"]: r for r in new.get("budget_lines", [])}
        for dep in sorted(set(before) | set(after)):
            b, n = before.get(dep), after.get(dep)
            if b == n:
                continue
            if b is None:
                parts.append(f"{dep} appeared")
            elif n is None:
                parts.append(f"{dep} gone")
            else:
                was, now = money(b["amount_cents"], currency), money(n["amount_cents"], currency)
                parts.append(f"{dep} {was} → {now}")
    basis = new.get("basis") or old.get("basis")
    for k in sorted(set(old) | set(new)):
        if k in (
            "budget_lines",
            "payables",
            "digest",
            "accounts",
            "budget_id",
            "goals",
            "margin_target_bps",
        ):
            continue
        if k == "total_cents" and ("payables" in old or "payables" in new):
            continue  # the rows above already say what moved
        if old.get(k) == new.get(k):
            continue
        if k in _MONEY_LABELS:
            label = _MONEY_LABELS[k]
            if k in ("revenue_cents", "costs_cents") and basis:
                label = f"{basis} {label}"
            parts.append(
                f"{label} {money(int(old.get(k) or 0), currency)} → "
                f"{money(int(new.get(k) or 0), currency)}"
            )
        else:
            parts.append(f"{_PLAIN_LABELS.get(k, k)} {old.get(k)} → {new.get(k)}")
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
        if (
            proposal is not None
            and proposal.kind in ctx.kinds
            and PLATFORM_OF[proposal.kind] != "switch"
        ):
            return [Lookup(proposal)]
        return []
    command = cmd.parse_command(ev.body, ctx.executor_names)
    if isinstance(command, cmd.Approve):
        a = ctx.view.get(command.artifact_id)
        if (
            a is not None
            and a.kind in ctx.kinds
            and PLATFORM_OF[a.kind] != "switch"
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
    else:
        effects.extend(_handle_command(ctx, ev))
    return effects


def handle_sweep(ctx: Context) -> list[Effect]:
    """Expiry, retries, crash recovery, the derived stages, the daily board."""
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
    # a run on a rail that reports its own status: ask it, once a minute, until it is paid
    for a in ctx.view.in_states(State.SENT):
        if a.kind not in ctx.kinds or not a.result().get("rail_confirms"):
            continue
        last = ctx.view.get_value(f"poll:{a.id}")
        if last and (ctx.now - datetime.fromisoformat(last)) < timedelta(seconds=55):
            continue
        effects.append(SetValue(f"poll:{a.id}", ctx.now.isoformat()))
        effects.append(PollPayment(a.id))
    # a won deal with an invoice out: ask the biller, once a minute, until the customer pays
    for a in ctx.view.in_states(State.WON):
        if a.kind not in ctx.kinds or not a.result().get("invoice_id"):
            continue
        if ctx.view.get_value(f"collected:{a.id}") is not None:
            continue
        last = ctx.view.get_value(f"poll:{a.id}")
        if last and (ctx.now - datetime.fromisoformat(last)) < timedelta(seconds=55):
            continue
        effects.append(SetValue(f"poll:{a.id}", ctx.now.isoformat()))
        effects.append(PollInvoice(a.id))
    # an order form out for signature: ask once a minute until it is signed or declined
    for a in ctx.view.in_states(State.WON):
        if a.kind not in ctx.kinds or ctx.view.get_value(f"esign:{a.id}") is None:
            continue
        if ctx.view.get_value(f"signed:{a.id}") is not None:
            continue
        last = ctx.view.get_value(f"pollsig:{a.id}")
        if last and (ctx.now - datetime.fromisoformat(last)) < timedelta(seconds=55):
            continue
        effects.append(SetValue(f"pollsig:{a.id}", ctx.now.isoformat()))
        effects.append(PollSignature(a.id))
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
        effects.extend(_switch_stages(ctx))
        effects.extend(_daily_board(ctx))
    else:
        effects.extend(_ledger_stages(ctx))
    return effects


# ---------------------------------------------------------------------------------------------
# P — proposals (P1–P5)
# ---------------------------------------------------------------------------------------------


def _reject(ctx: Context, ev: MessageEvent, code: str, text: str, **details: Any) -> list[Effect]:
    return [
        Post(ev.room_id, f"Proposal not recorded — {text}", thread_id=_reply_thread(ev)),
        _audit(ev, f"rejected_proposal:{code}", **details),
    ]


def _proposal_room_ok(ctx: Context, rule: Rule, room_id: str) -> bool:
    if rule.room == PERIOD_ROOM:
        return room_id not in {v for v in ctx.policy.rooms.values() if v}
    return bool(ctx.policy.room_id(rule.room)) and room_id == ctx.policy.room_id(rule.room)


def _rule_room_name(ctx: Context, rule: Rule, a: Artifact | None = None) -> str:
    if rule.room == PERIOD_ROOM:
        return ctx.room_name(a.room_id) if a else "the period room"
    room_id = ctx.policy.room_id(rule.room)
    if room_id and (room_id in ctx.room_names or ctx.policy.room_key(room_id)):
        return ctx.room_name(room_id)
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
    thread = _reply_thread(ev)
    existing = next(
        (
            a
            for a in ctx.view.by_thread(thread, State.CHANGES_REQUESTED, State.PENDING)
            if a.kind is proposal.kind
        ),
        None,
    )
    # order (design v5 §5): the budget follows its goals, the entries follow the recorded
    # budget; one of each per period
    reason = _order_check(ctx, proposal, thread)
    if reason is not None:
        return _reject(ctx, ev, "order", f"{reason}.", kind=proposal.kind.value)
    # P4 — the snapshot comes from the ledger, never from the fence
    if proposal.kind in ROOM_KINDS:
        facts = Facts(ok=True, data=_room_plan(ctx, proposal, rule))
    else:
        facts = ctx.facts.get(proposal.target) or Facts(ok=False, error="target not read")
        if not facts.ok:
            return _reject(
                ctx,
                ev,
                "no_facts",
                f"could not read the ledger for {proposal.target}: {facts.error}.",
            )
        if proposal.snapshot_hint and facts.data.get("digest") != proposal.snapshot_hint:
            return _reject(
                ctx,
                ev,
                "stale",
                "the ledger changed since this was prepared; propose again from the current rows.",
            )
        if proposal.kind is Kind.BUDGET:
            facts = Facts(ok=True, data={**facts.data, **_goals_of(ctx, proposal)})
        reason = check_facts(ctx.policy, proposal, facts.data)
        if reason is not None:
            return _reject(ctx, ev, "policy", f"{reason}.", reason=reason)
    # P5 — id, pending, template. Re-proposals keep their id.
    if existing is not None:
        return _reproposal(ctx, ev, existing, proposal, facts, rule)
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
        *([CrmSync(aid, "open")] if proposal.kind is Kind.DEAL else []),
        Post(ev.room_id, pending_text(ctx, artifact, rule), thread_id=thread),
        *_status(ctx, artifact),
    ]


def _plan_named(ctx: Context, head_id: str) -> Artifact | None:
    """The approved head of a chain (the goals, or the month's plan) an artifact names, from
    the other executor's state (read-only)."""
    pool = (
        ctx.peer_artifacts
        if not (ROOM_KINDS & ctx.kinds)
        else tuple(ctx.view.in_states(State.OPEN, State.DONE))
    )
    return next((a for a in pool if a.kind in ROOM_KINDS and a.id == head_id), None)


def _head_id(body: Any) -> str:
    return str(getattr(body, "goals", None) or getattr(body, "plan", None) or "")


def _order_check(ctx: Context, proposal: Proposal, thread: str) -> str | None:
    body: Any = proposal.body
    if proposal.kind in ROOM_KINDS:
        # one head per period and chain: pending, opening, open or done, another one is refused
        taken = (*WAITING_STATES, *IN_FLIGHT_STATES, State.OPEN_FAILED, State.OPEN, State.DONE)
        for a in ctx.view.in_states(*taken, kind=proposal.kind):
            if a.period == body.period and a.thread_id != thread:
                return f"**{a.id}** is already {a.state.value} for {body.period}"
        return None
    if proposal.kind in STANDALONE_KINDS:
        # a deal stands alone: one live artifact per deal id
        held = (*WAITING_STATES, *IN_FLIGHT_STATES, *RETRY_STATES, State.WON)
        for a in ctx.view.in_states(*held, kind=proposal.kind):
            if a.proposal().body.deal_id == body.deal_id and a.thread_id != thread:
                return f"**{a.id}** is already {a.state.value} for {body.deal_id}"
        return None
    head_id = _head_id(body)
    head = _plan_named(ctx, head_id)
    want = HEAD_OF[proposal.kind]
    if head is None or head.kind is not want or head.state not in (State.OPEN, State.DONE):
        noun = "approved goals" if want is Kind.GOALS else "an approved plan"
        verb = "are" if want is Kind.GOALS else "is"
        return f"{head_id} {verb} not {noun} yet; a {proposal.kind.value} follows the {want.value}"
    head_body: Any = head.proposal().body
    if head_body.period != body.period:
        return f"{head_id} is for {head_body.period}, not {body.period}"
    live = (*WAITING_STATES, *IN_FLIGHT_STATES, *RETRY_STATES)

    def taken_by(kind: Kind, *states: State) -> str | None:
        for a in ctx.view.in_states(*live, *states, kind=kind):
            if _head_id(a.proposal().body) == head_id and a.thread_id != thread:
                return f"**{a.id}** is already {a.state.value} for {head_id}"
        return None

    if proposal.kind is Kind.BUDGET:
        return taken_by(Kind.BUDGET, State.RECORDED)
    if proposal.kind is Kind.ENTRIES:
        recorded = [
            a
            for a in ctx.view.in_states(State.RECORDED, kind=Kind.BUDGET)
            if a.proposal().body.goals == head_id
        ]
        if not recorded:
            return f"no budget is recorded for {head_id} yet; the entries follow the budget"
        if body.budget not in {a.id for a in recorded}:
            return f"{body.budget} is not the recorded budget of {head_id}"
        return taken_by(Kind.ENTRIES)
    if proposal.kind is Kind.PAY:
        extra = [x.invoice for x in body.run if x.invoice not in head_body.payables]
        if extra:
            return f"{', '.join(extra)} not in the approved plan {head_id}"
        return taken_by(Kind.PAY)
    # LOCK: the plan's payables must be paid first, by a run of this plan
    if head_body.payables:
        paid = [
            a
            for a in ctx.view.in_states(State.PAID, kind=Kind.PAY)
            if a.proposal().body.plan == head_id
        ]
        if not paid:
            sent = [
                a
                for a in ctx.view.in_states(State.SENT, kind=Kind.PAY)
                if a.proposal().body.plan == head_id
            ]
            if sent:
                return (
                    f"{sent[0].id} is sent, not paid; the lock follows the bank's confirmation "
                    f"(`paid {sent[0].id} <reference>`)"
                )
            return f"{head_id} names payables; the lock follows their payment run being paid"
        if body.pay_run and body.pay_run not in {a.id for a in paid}:
            return f"{body.pay_run} is not a paid run of {head_id}"
    return taken_by(Kind.LOCK)


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
    """The `pending` template: one id, money, the group, the room, the three commands."""
    room = _rule_room_name(ctx, rule, a)
    head = f"**{a.id}** pending" + (" (revised, approvals cleared)" if revised else "")
    return (
        f"{head} · {summarize(ctx, a)} · needs {_approver_phrase(ctx, rule, a)} in {room}. "
        f"`@{ctx.executor} approve {a.id}` / `deny {a.id} <reason>` / `changes {a.id} <note>`"
    )


_LIMITED_KINDS = (Kind.BUDGET, Kind.PAY, Kind.DEAL)  # a limit picks the group on the total read


def _approver_phrase(ctx: Context, rule: Rule, a: Artifact) -> str:
    total = a.facts().get("total_cents") if a.kind in _LIMITED_KINDS else None
    groups = rule.approver_groups(int(total) if total is not None else None)
    if len(groups) == 1:
        return f"one {groups[0]} approver"
    what = "discount under" if a.kind is Kind.DEAL else "under"
    return (
        f"one {groups[0]} or {groups[1]} approver "
        f"({what} {money(rule.limit_cents or 0, rule.currency)})"
    )


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
        case cmd.Brief():
            return _brief(ctx, ev)
        case cmd.Paid(artifact_id=aid, reference=reference):
            return _paid(ctx, ev, aid, reference)
        case cmd.Sale(invoice=invoice, amount_cents=cents, customer=customer):
            return _sale(ctx, ev, invoice, cents, customer)
        case cmd.Price(product=product, amount_cents=cents):
            return _price(ctx, ev, product, cents)
        case cmd.Read(url=url, tab=tab):
            return _read_file(ctx, ev, url, tab)
        case cmd.Audit(target=target):
            return _audit_trail(ctx, ev, target)
        case cmd.Approvers(kind=kind_word):
            return _approvers(ctx, ev, kind_word)
        case cmd.Load(url=url):
            return _load(ctx, ev, url)
        case cmd.Report(period=period):
            return _report(ctx, ev, "report", period, "")
        case cmd.Export(period=period, what=what):
            return _report(ctx, ev, "export", period, what)
        case cmd.Chart(period=period, what=what):
            return _report(ctx, ev, "chart", period, what)
        case cmd.Pack(period=period):
            return _pack(ctx, ev, period)
        case cmd.Board():
            if ctx.executor != SWITCH_EXECUTOR:
                return [
                    Post(
                        ev.room_id,
                        f"The board is kept by {SWITCH_EXECUTOR} in "
                        f"{ctx.room_name(ctx.policy.room_id('finance'))}.",
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


def _sale(ctx: Context, ev: MessageEvent, invoice: str, cents: int, customer: str) -> list[Effect]:
    """A person lands a late sale in the books: not a gate, an event on the record, done by the
    ledger executor under the person's id so the next approval meets the changed ledger."""
    thread = _reply_thread(ev)
    if ctx.executor != LEDGER_EXECUTOR:
        return [
            Post(
                ev.room_id,
                f"The books are kept by {LEDGER_EXECUTOR}: type `sale "
                f"{invoice} {cents / 100:.2f} {customer}` to it in "
                f"{ctx.room_name(ctx.policy.room_id('planning'))}.",
                thread_id=thread,
            )
        ]
    name = ctx.name_of(ev.sender, ev.sender_name)
    if ev.sender in ctx.agent_ids:
        return _refuse(
            ev,
            "refused_sale:agent",
            f"**{invoice}** not booked — @{name} is an agent; a sale lands from a person.",
            None,
        )
    return [
        BookSale(invoice, customer, cents, ev.room_id, thread, ev.sender, name),
        _audit(ev, "sale_requested", invoice=invoice, amount_cents=cents, customer=customer),
    ]


def _report(ctx: Context, ev: MessageEvent, what: str, period: str, subject: str) -> list[Effect]:
    """Reports, exports and charts are sindarin-switch's: it reads the ledger read-only and can
    attach files through Switch. The ledger executor points there."""
    thread = _reply_thread(ev)
    if ctx.executor != SWITCH_EXECUTOR:
        words = f"{what} {period}" + (f" {subject}" if subject else "")
        return [
            Post(
                ev.room_id,
                f"Reports are kept by {SWITCH_EXECUTOR}: type `{words}` to it.",
                thread_id=thread,
            )
        ]
    name = ctx.name_of(ev.sender, ev.sender_name)
    return [
        RunReport(what, period, subject, ev.room_id, thread, ev.sender, name),
        _audit(ev, f"{what}_requested", period=period, subject=subject),
    ]


def _pack(ctx: Context, ev: MessageEvent, period: str) -> list[Effect]:
    """The board pack is sindarin-switch's: it holds the read-only ledger and the Drive key."""
    thread = _reply_thread(ev)
    if ctx.executor != SWITCH_EXECUTOR:
        return [
            Post(
                ev.room_id,
                f"The board pack is built by {SWITCH_EXECUTOR}: type `pack {period}` to it.",
                thread_id=thread,
            )
        ]
    name = ctx.name_of(ev.sender, ev.sender_name)
    return [
        BuildPack(period, ev.room_id, thread, ev.sender, name),
        _audit(ev, "pack_requested", period=period),
    ]


def sale_booked_text(e: BookSale, data: dict[str, Any]) -> str:
    period = data.get("period", "")
    revenue = int(data.get("revenue_cents", 0))
    return (
        f"**{e.invoice}** {e.customer} {money(e.amount_cents)} landed in {period} · "
        f"{period} revenue is now {money(revenue)}."
    )


def _price(ctx: Context, ev: MessageEvent, product: str, cents: int) -> list[Effect]:
    """A person edits a list price: not a gate, an event on the record, done by the ledger
    executor under the person's id so a pending deal meets the changed price book."""
    thread = _reply_thread(ev)
    if ctx.executor != LEDGER_EXECUTOR:
        return [
            Post(
                ev.room_id,
                f"The price book is kept by {LEDGER_EXECUTOR}: type `price "
                f"{product} {cents / 100:.2f}` to it in "
                f"{ctx.room_name(ctx.policy.room_id('sales'))}.",
                thread_id=thread,
            )
        ]
    name = ctx.name_of(ev.sender, ev.sender_name)
    if ev.sender in ctx.agent_ids:
        return _refuse(
            ev,
            "refused_price:agent",
            f"**{product}** not repriced — @{name} is an agent; a list price moves by a person.",
            None,
        )
    return [
        SetPrice(product, cents, ev.room_id, thread, ev.sender, name),
        _audit(ev, "price_requested", product=product, amount_cents=cents),
    ]


def _audit_trail(ctx: Context, ev: MessageEvent, target: str) -> list[Effect]:
    """`audit GOALS-1` is answered by the executor that holds the artifact; `audit 2026-10` by
    sindarin-switch in the finance room with both executors' events, elsewhere by the executor
    present with its own. One answer per room, never two."""
    thread = _reply_thread(ev)
    in_finance = bool(ev.room_id) and ev.room_id == ctx.policy.room_id("finance")
    is_period = bool(re.fullmatch(r"\d{4}-\d{2}", target))
    if is_period:
        if ctx.executor == SWITCH_EXECUTOR and not in_finance:
            return []  # the ledger executor is in this room and answers for its own events
        return [
            RunAudit(target, ctx.executor == SWITCH_EXECUTOR, ev.room_id, thread, ev.sender, ""),
            _audit(ev, "audit_requested", target=target),
        ]
    own = ctx.view.get(target) is not None
    if own:
        return [
            RunAudit(target, False, ev.room_id, thread, ev.sender, ""),
            _audit(ev, "audit_requested", target=target),
        ]
    if ctx.executor == SWITCH_EXECUTOR and in_finance:
        if any(a.id == target for a in ctx.peer_artifacts):
            return [
                RunAudit(target, True, ev.room_id, thread, ev.sender, ""),
                _audit(ev, "audit_requested", target=target),
            ]
        return [Post(ev.room_id, f"**{target}** is unknown here.", thread_id=thread)]
    if ctx.executor == LEDGER_EXECUTOR:
        if not any(a.id == target for a in ctx.peer_artifacts):
            return [Post(ev.room_id, f"**{target}** is unknown here.", thread_id=thread)]
        shared = any(
            a.kind in ROOM_KINDS and a.result().get("room_id") == ev.room_id
            for a in ctx.peer_artifacts
        )
        if shared:
            return []  # sindarin-switch opened this room, sits in it and answers
        return [
            Post(
                ev.room_id,
                f"**{target}** is kept by {SWITCH_EXECUTOR}: ask in "
                f"{ctx.room_name(ctx.policy.room_id('finance'))}.",
                thread_id=thread,
            )
        ]
    return []  # the owner is in this room and answers


def _approvers(ctx: Context, ev: MessageEvent, kind_word: str | None) -> list[Effect]:
    """`who approves [<kind>]`: each executor answers for the kinds it owns, so a room with both
    gets one answer per kind. Names are display only; the ids in the policy decide."""
    thread = _reply_thread(ev)
    wanted = [k for k in Kind if k.value == kind_word] if kind_word else list(Kind)
    lines = []
    for kind in wanted:
        if kind not in ctx.kinds:
            continue
        for rule in ctx.policy.rules_for(kind):
            groups = [rule.approvers] + (
                [rule.approvers_under_limit] if rule.approvers_under_limit else []
            )
            who = rule.approvers
            if rule.approvers_under_limit and rule.limit_cents is not None:
                noun = "a discount" if kind is Kind.DEAL else "amounts"
                who = (
                    f"{rule.approvers}; {rule.approvers_under_limit} alone for {noun} up to "
                    f"{money(rule.limit_cents, rule.currency)}"
                )
            room = (
                ctx.room_name(ctx.policy.room_id(rule.room))
                if rule.room != PERIOD_ROOM
                else ("the plan room" if kind is Kind.ENTRIES else "the period room")
            )
            names = " · ".join(
                f"{g}: {', '.join(ctx.name_of(p) for p in ctx.policy.group_members(g)) or 'nobody'}"
                for g in groups
            )
            lines.append(f"**{kind.value}** · {who} · in {room} · {names}")
    if not lines:
        return []
    return [Post(ev.room_id, "\n".join(lines), thread_id=thread)]


def _read_file(ctx: Context, ev: MessageEvent, url: str, tab: str = "") -> list[Effect]:
    """Any file a person links: sindarin-switch fetches and describes it, read-only."""
    thread = _reply_thread(ev)
    if ctx.executor != SWITCH_EXECUTOR:
        return [
            Post(
                ev.room_id,
                f"Files are read by {SWITCH_EXECUTOR}: type `read <link>` to it.",
                thread_id=thread,
            )
        ]
    name = ctx.name_of(ev.sender, ev.sender_name)
    return [
        InspectFile(url, tab, ev.room_id, thread, ev.sender, name),
        _audit(ev, "file_requested", url=url[:200], tab=tab),
    ]


def _load(ctx: Context, ev: MessageEvent, url: str) -> list[Effect]:
    """The team's file into the books: a write by a person in an approver group, under their
    id; the ledger keeps its locked months and paid payables whatever the file says."""
    thread = _reply_thread(ev)
    if ctx.executor != LEDGER_EXECUTOR:
        return [
            Post(
                ev.room_id,
                f"The books are loaded by {LEDGER_EXECUTOR}: type `load <link>` to it in "
                f"{ctx.room_name(ctx.policy.room_id('planning'))}.",
                thread_id=thread,
            )
        ]
    name = ctx.name_of(ev.sender, ev.sender_name)
    if ev.sender in ctx.agent_ids:
        return _refuse(
            ev,
            "refused_load:agent",
            f"**load** refused — @{name} is an agent; the books are loaded by a person.",
            None,
        )
    if not ctx.policy.groups_of(ev.sender):
        return _refuse(
            ev,
            "refused_load:not_approver",
            f"**load** refused — {name} is in no approver group; the books are loaded by the "
            "cfo, a controller or the board.",
            None,
        )
    return [
        LoadBooks(url, ev.room_id, thread, ev.sender, name),
        _audit(ev, "load_requested", url=url[:200]),
    ]


def price_set_text(e: SetPrice, data: dict[str, Any]) -> str:
    was = int(data.get("was_cents", 0))
    return f"**{e.product}** list price is now {money(e.amount_cents)} (was {money(was)})."


def _refuse(ev: MessageEvent, code: str, text: str, aid: str | None, **d: Any) -> list[Effect]:
    return [Post(ev.room_id, text, thread_id=_reply_thread(ev)), _audit(ev, code, aid, **d)]


def _gate(
    ctx: Context, ev: MessageEvent, aid: str, verb: str, *, allow_changes_requested: bool = False
) -> tuple[Artifact | None, Rule | None, list[Effect]]:
    """A2–A5, shared by approve, deny and changes. Returns (artifact, rule, refusal effects)."""
    a = ctx.view.get(aid)
    if a is None or a.kind not in ctx.kinds:
        owner = EXECUTOR_OF_KIND.get((aid.split("-", 1)[0] or "").lower())
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
    # A2 — the group the rule names, by participant id; a budget or pay rule may admit a second
    # group below its limit, judged on the total the executor read, never on the fence
    total = a.facts().get("total_cents") if a.kind in _LIMITED_KINDS else None
    allowed = rule.approver_groups(int(total) if total is not None else None)
    if not any(ctx.policy.in_group(ev.sender, g) for g in allowed):
        text = f"**{aid}** not applied — {name} is not in the {rule.approvers} group."
        if (
            rule.limit_cents is not None
            and rule.approvers_under_limit
            and total is not None
            and ctx.policy.in_group(ev.sender, rule.approvers_under_limit)
        ):
            amount = money(int(total), rule.currency)
            if a.kind is Kind.DEAL:
                amount = f"a {amount} discount"
            text = (
                f"**{aid}** not applied — {amount} needs the "
                f"{rule.approvers} group; {rule.approvers_under_limit} may approve up to "
                f"{money(rule.limit_cents, rule.currency)}."
            )
        return (
            None,
            None,
            _refuse(ev, f"refused_{verb}:not_approver", text, aid, group=rule.approvers),
        )
    # A5 — the room the rule names
    room_ok = (
        ev.room_id == a.room_id
        if rule.room == PERIOD_ROOM
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
    """A3, second half: the one who proposed never approves."""
    if ev.sender == a.proposer_id:
        name = ctx.name_of(ev.sender, ev.sender_name)
        return _refuse(
            ev, "refused_approval:self", f"**{a.id}** not applied — {name} proposed it.", a.id
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
    # A6 — snapshot unchanged: re-read from the platform (or recomputed from policy for goals)
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
        what = _what_changed(a.facts(), live.data, getattr(proposal.body, "currency", "USD"))
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
                f"**{aid}** changed since it was proposed: {what}. "
                f"Approvals cleared; @{ctx.name_of(a.proposer_id)} please propose again.",
                thread_id=a.thread_id,
            ),
            *_refuse(
                ev,
                "refused_approval:snapshot_changed",
                f"**{aid}** not applied — the ledger changed since the proposal ({what}).",
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
        *([CrmSync(a.id, "lost")] if a.kind is Kind.DEAL else []),
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
        landed = SUCCESS_OF[a.kind]
        if a.kind is Kind.PAY and int(result.data.get("paid", 0)) < len(a.proposal().body.run):
            landed = State.SENT
        return [
            Transition(
                a.id,
                landed,
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
            *_sent_next_step(ctx, a, result.data, landed),
            *_deal_connectors(a, landed),
            *_room_opened(ctx, a, result.data),
            *_status(ctx, a, landed),
        ]
    error = result.error or ("not visible on read-back" if result.ok else "unknown platform error")
    action = a.proposal().action.replace("_", " ")
    failed = FAILURE_OF[a.kind]
    if a.kind in ROOM_KINDS or a.attempts < ctx.max_attempts:
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


def _sent_next_step(ctx: Context, a: Artifact, data: dict[str, Any], landed: State) -> list[Effect]:
    """A run handed to the bank file: the batch in the thread and the line a person types once
    the bank has paid. A polling rail needs nothing from anyone."""
    if a.kind is Kind.DEAL and data.get("invoice_url"):
        return [
            Post(
                a.room_id,
                f"**{a.id}** invoice for {data.get('account', 'the customer')}: "
                f"{data['invoice_url']} · I check it until it is paid, then the receipt is "
                "booked.",
                thread_id=a.thread_id,
            )
        ]
    if landed is not State.SENT or a.kind is not Kind.PAY:
        return []
    if data.get("rail_confirms"):
        return [
            Post(
                a.room_id,
                f"**{a.id}** is with {data.get('rail', 'the rail')}; I check it until every "
                "payout is paid, then the period can lock.",
                thread_id=a.thread_id,
            )
        ]
    body = ""
    if data.get("document_csv"):
        body = f"\n```csv\n{str(data['document_csv']).strip()[:2500]}\n```"
    return [
        Post(
            a.room_id,
            f"**{a.id}** payment file **{data.get('document', a.id + '.csv')}** for the bank "
            f"portal.{body}\nOnce the bank has paid, confirm with "
            f"`@{LEDGER_EXECUTOR} paid {a.id} <bank reference>`; the period locks after that.",
            thread_id=a.thread_id,
        )
    ]


def _deal_connectors(a: Artifact, landed: State) -> list[Effect]:
    """A won deal reaches the CRM and goes out for signature; the runner skips what is not
    configured."""
    if a.kind is not Kind.DEAL or landed is not State.WON:
        return []
    return [CrmSync(a.id, "won"), SendContract(a.id)]


def after_collect(ctx: Context, a: Artifact, data: dict[str, Any]) -> list[Effect]:
    """The customer paid the deal's invoice: the receipt is booked and the room is told."""
    receipt = data.get("receipt") or {}
    body: Any = a.proposal().body
    amount = money(int(receipt.get("amount_cents", body.price_cents)), body.currency)
    return [
        SetValue(f"collected:{a.id}", ctx.now.isoformat()),
        Audit("receipt_booked", a.id, details={k: v for k, v in data.items() if k != "receipt"}),
        Post(
            a.room_id,
            f"**{a.id}** paid by {body.account} · {amount} received on invoice "
            f"**{data.get('invoice_id', '')}** · receipt booked to "
            f"{receipt.get('period', body.period)} (cash up, receivables down) at "
            f"**{ctx.now:%H:%M:%S} UTC**.",
            thread_id=a.thread_id,
        ),
    ]


def after_confirm(
    ctx: Context, a: Artifact, data: dict[str, Any], *, actor_name: str = ""
) -> list[Effect]:
    """The rail or the bank says the run is paid: the artifact lands in PAID and the lock
    stage line follows on the next sweep. A run still partly out stays SENT."""
    if a.state is not State.SENT:
        return [Audit("confirm_ignored", a.id, details={"state": a.state.value})]
    result = {**a.result(), **data}
    n = len(a.proposal().body.run)
    if int(data.get("paid", 0)) < n:
        return [
            Transition(a.id, State.SENT, {"result_json": json.dumps(result, sort_keys=True)}),
            Audit("payment_progress", a.id, details={k: v for k, v in data.items()}),
        ]
    by = f" by {actor_name}" if actor_name else f" by {data.get('rail', 'the rail')}"
    return [
        Transition(
            a.id, State.PAID, {"result_json": json.dumps(result, sort_keys=True), "error": None}
        ),
        Audit("paid", a.id, actor_name=actor_name or None, details=dict(data)),
        Post(
            a.room_id,
            f"**{a.id}** paid{by} · {action_phrase(a, result)} · booked and verified at "
            f"**{ctx.now:%H:%M:%S} UTC**.",
            thread_id=a.thread_id,
        ),
        *_status(ctx, a, State.PAID),
    ]


def _paid(ctx: Context, ev: MessageEvent, aid: str, reference: str) -> list[Effect]:
    """`paid PAY-n <reference>`: a person in the run's approver groups reports the bank's
    confirmation. Agents are refused; a run that was never sent, or is already paid, is named."""
    thread = _reply_thread(ev)
    a = ctx.view.get(aid)
    if a is None or a.kind is not Kind.PAY or a.kind not in ctx.kinds:
        return _refuse(ev, "refused_paid:unknown", f"**{aid}** is unknown here.", aid)
    if ev.sender in ctx.agent_ids:
        return _refuse(
            ev,
            "refused_paid:agent",
            f"**{aid}** not confirmed — agents and executors do not confirm payments.",
            aid,
        )
    rule = ctx.policy.rule(a.rule)
    groups = [
        g
        for g in ((rule.approvers if rule else ""), (rule.approvers_under_limit if rule else ""))
        if g
    ]
    if not any(ev.sender in ctx.policy.group_members(g) for g in groups):
        name = ctx.name_of(ev.sender, ev.sender_name)
        return _refuse(
            ev,
            "refused_paid:not_in_group",
            f"**{aid}** not confirmed — {name} is not in the {' or '.join(groups)} group.",
            aid,
        )
    if rule is not None and not _proposal_room_ok(ctx, rule, ev.room_id):
        return _refuse(
            ev,
            "refused_paid:wrong_room",
            f"**{aid}** not confirmed — I take paid commands in "
            f"{_rule_room_name(ctx, rule, a)} only.",
            aid,
        )
    if a.state is State.PAID:
        return _refuse(ev, "refused_paid:already", f"**{aid}** is already paid.", aid)
    if a.state is not State.SENT:
        return _refuse(
            ev,
            "refused_paid:not_sent",
            f"**{aid}** not confirmed — it is {a.state.value}, not sent to the bank.",
            aid,
        )
    if a.result().get("rail_confirms"):
        return _refuse(
            ev,
            "refused_paid:rail_confirms",
            f"**{aid}** is confirmed by {a.result().get('rail', 'the rail')} itself; I am "
            "checking it.",
            aid,
        )
    return [
        _audit(ev, "paid_reported", aid, reference=reference),
        ConfirmPaid(
            aid, reference, ev.room_id, thread, ev.sender, ctx.name_of(ev.sender, ev.sender_name)
        ),
    ]


def _room_opened(ctx: Context, a: Artifact, result: dict[str, Any]) -> list[Effect]:
    """Post the room's existence in #acme-finance with its members, and the brief into the new
    room as its first message (the one every agent in it reads)."""
    if a.kind not in ROOM_KINDS:
        return []
    plan = a.facts()
    body: Any = a.proposal().body
    people = ", ".join(ctx.name_of(p) for p in plan.get("humans", []))
    agents = ", ".join(ctx.name_of(p) for p in plan.get("agents", []))  # no mentions: not in here
    room = result.get("room_name") or a.target
    if a.kind is Kind.GOALS:
        budget_rule = next(iter(ctx.policy.rules_for(Kind.BUDGET)), None)
        entries_rule = next(iter(ctx.policy.rules_for(Kind.ENTRIES)), None)
        by = budget_rule.approvers if budget_rule else "cfo"
        if budget_rule and budget_rule.approvers_under_limit:
            by = (
                f"{budget_rule.approvers_under_limit} (the {budget_rule.approvers} above the limit)"
            )
        tail = (
            f"instructions: the goals. The budget is approved in "
            f"{ctx.room_name(ctx.policy.room_id('planning'))} by {by}; its register entries "
            f"into the books of {body.period} are approved in the plan room by "
            f"{entries_rule.approvers if entries_rule else 'controllers'}, both to "
            f"@{LEDGER_EXECUTOR}."
        )
        kind_word, mention = "plan room", f"@{PLANNER} @{BOOKKEEPER}"
    else:
        pay_rule = next(iter(ctx.policy.rules_for(Kind.PAY)), None)
        lock_rule = next(iter(ctx.policy.rules_for(Kind.LOCK)), None)
        tail = (
            f"instructions: the plan. Payment runs are approved in "
            f"{ctx.room_name(ctx.policy.room_id('payments'))} by "
            f"{pay_rule.approvers if pay_rule else 'cfo'}; the lock of {body.period} is approved "
            f"in the period room by {lock_rule.approvers if lock_rule else 'controllers'}, both "
            f"to @{LEDGER_EXECUTOR}."
        )
        kind_word, mention = "period room", f"@{BOOKKEEPER} @{TREASURY}"
    effects: list[Effect] = [
        Post(
            a.room_id,
            f"**{a.id}** {kind_word} **#{room.lstrip('#')}** is open · people: {people} · "
            f"agents: {agents} · {tail}",
            thread_id=a.thread_id,
        )
    ]
    new_room = result.get("room_id")
    if new_room:
        effects.append(Post(str(new_room), brief_text(ctx, a, mention)))
    return effects


def brief_text(ctx: Context, a: Artifact, mention: str) -> str:
    """The goals as the plan room's brief: posted when the room opens, and again to whoever
    asks `brief` (an agent the bridge had not synced yet, a person, a restarted agent)."""
    approver = ctx.name_of(a.approver_id or "")
    return (
        f"{mention} **{a.id}** instructions · approved by {approver} · period {a.period}.\n"
        f"{plan_instructions(a.proposal())}"
    ).strip()


def plan_for_room(ctx: Context, room_id: str) -> Artifact | None:
    """The goals or plan whose room this is, from the executor's own record of what it opened."""
    for a in ctx.view.in_states(State.OPENING, State.OPEN, State.OPEN_FAILED, State.DONE):
        if a.kind in ROOM_KINDS and a.result().get("room_id") == room_id:
            return a
    return None


def _brief(ctx: Context, ev: MessageEvent) -> list[Effect]:
    thread = _reply_thread(ev)
    if ctx.executor != SWITCH_EXECUTOR:
        return [
            Post(
                ev.room_id,
                f"The goals are kept by {SWITCH_EXECUTOR}: ask it with `brief`.",
                thread_id=thread,
            )
        ]
    plan = plan_for_room(ctx, ev.room_id)
    if plan is None:
        return [
            Post(
                ev.room_id,
                "Nothing opened this room; `brief` works in a plan room or a period room.",
                thread_id=thread,
            )
        ]
    mention = f"@{ev.sender_name}" if ev.sender_name else ""
    return [Post(ev.room_id, brief_text(ctx, plan, mention))]


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
# derived stages and the board — the two executors' sweeps
# ---------------------------------------------------------------------------------------------


def _switch_stages(ctx: Context) -> list[Effect]:
    """open → done when the chain's last step is applied: the register entries for the goals,
    the lock for the month's plan. Read from the other executor's state, never from its
    messages. The stage line wakes the analyst."""
    effects: list[Effect] = []
    finished: dict[str, Artifact] = {}
    for a in ctx.peer_artifacts:
        if (a.kind is Kind.ENTRIES and a.state is State.PLANNED) or (
            a.kind is Kind.LOCK and a.state is State.LOCKED
        ):
            finished.setdefault(_head_id(a.proposal().body), a)
    for head in ctx.view.in_states(State.OPEN):
        if head.kind not in ROOM_KINDS:
            continue
        last = finished.get(head.id)
        if last is None:
            continue
        outcome = "planned" if head.kind is Kind.GOALS else "locked"
        effects += [
            Transition(head.id, State.DONE),
            Audit(f"{head.kind.value}_done", head.id, details={"last": last.id}),
            Post(
                head.room_id,
                f"**{head.id}** done · {last.id} applied · period {head.period} is {outcome}. "
                f"@{ANALYST} tell the business.",
                head.thread_id,
            ),
        ]
        finance_room = ctx.policy.room_id("finance")
        if (
            head.kind is Kind.CLOSE
            and finance_room
            and ctx.view.get_value(f"pack:{head.period}") is None
        ):
            # the locked month's board pack, in the thread the business asked in
            effects += [
                SetValue(f"pack:{head.period}", ctx.now.isoformat()),
                BuildPack(head.period, finance_room, head.ask_root_id or None, "", "", auto=True),
            ]
    finance = ctx.policy.room_id("finance")
    for deal in ctx.peer_artifacts:
        # a won deal is told once to the finance room: the books moved by a sale
        if deal.kind is not Kind.DEAL or deal.state is not State.WON or not finance:
            continue
        if ctx.view.get_value(f"stage:won:{deal.id}") is not None:
            continue
        dbody: Any = deal.proposal().body
        revenue = deal.result().get("revenue_cents")
        now = (
            f" · {dbody.period} revenue is now {money(int(revenue), dbody.currency)}"
            if revenue is not None
            else ""
        )
        effects += [
            SetValue(f"stage:won:{deal.id}", ctx.now.isoformat()),
            Post(
                finance,
                f"**{deal.id}** won · {dbody.deal_id} {dbody.account} · {dbody.product} at "
                f"{money(dbody.price_cents, dbody.currency)} landed in {dbody.period}{now}.",
            ),
            Audit("stage_line", deal.id, details={"stage": "won"}),
        ]
    for head in ctx.view.in_states(State.DONE):
        # a locked month gets its board pack once, where the business asked for the close
        if head.kind is not Kind.CLOSE or not finance:
            continue
        if ctx.view.get_value(f"pack:{head.period}") is not None:
            continue
        effects += [
            SetValue(f"pack:{head.period}", ctx.now.isoformat()),
            BuildPack(head.period, finance, head.ask_root_id or None, "", "", auto=True),
        ]
    return effects


_goals_stages = _switch_stages


def _ledger_stages(ctx: Context) -> list[Effect]:
    """Hand the work on (design v5 §5): once the goals are approved, call the planner in
    #planning, and once the budget is recorded, the bookkeeper in the plan room; once a plan
    is approved, call the treasury agent in #payments, and once its run is paid (or it had no
    payables), the bookkeeper in the period room. Each line is posted once and recorded in the
    store; it is the only way a machine wakes an agent."""
    effects: list[Effect] = []
    planning = ctx.policy.room_id("planning")
    payments = ctx.policy.room_id("payments")
    recorded = [a for a in ctx.view.in_states(State.RECORDED, kind=Kind.BUDGET)]
    paid_runs = [a for a in ctx.view.in_states(State.PAID, kind=Kind.PAY)]
    for head in ctx.peer_artifacts:
        if head.kind not in ROOM_KINDS or head.state not in (State.OPEN, State.DONE):
            continue
        body: Any = head.proposal().body
        room = str(head.result().get("room_id") or "")
        if head.kind is Kind.GOALS:
            if planning and ctx.view.get_value(f"stage:budget:{head.id}") is None:
                effects += [
                    SetValue(f"stage:budget:{head.id}", ctx.now.isoformat()),
                    Post(
                        planning,
                        f"**{head.id}** approved · revenue target "
                        f"{money(body.revenue_target_cents, body.currency)} · cost cap "
                        f"{money(body.cost_cap_cents, body.currency)} for {body.period}. "
                        f"@{PLANNER} propose the budget.",
                    ),
                    Audit("stage_line", head.id, details={"stage": "budget"}),
                ]
            mine = [a for a in recorded if a.proposal().body.goals == head.id]  # type: ignore[union-attr]
            if (
                mine
                and room
                and head.state is State.OPEN
                and ctx.view.get_value(f"stage:entries:{head.id}") is None
            ):
                b: Any = mine[0].proposal().body
                n = len(b.lines)
                effects += [
                    SetValue(f"stage:entries:{head.id}", ctx.now.isoformat()),
                    Post(
                        room,
                        f"**{mine[0].id}** recorded · {n} line{'s' if n != 1 else ''}, "
                        f"{money(b.total_cents, b.currency)} under the cap · period "
                        f"{body.period} is ready for its entries. @{BOOKKEEPER} propose the "
                        "budget register entries.",
                    ),
                    Audit("stage_line", head.id, details={"stage": "entries"}),
                ]
            continue
        # the month-end chain
        if body.payables and payments and ctx.view.get_value(f"stage:pay:{head.id}") is None:
            n = len(body.payables)
            effects += [
                SetValue(f"stage:pay:{head.id}", ctx.now.isoformat()),
                Post(
                    payments,
                    f"**{head.id}** approved · {n} payable{'s' if n != 1 else ''}, "
                    f"{money(body.total_payables_cents, body.currency)} for {body.period}. "
                    f"@{TREASURY} propose the payment run.",
                ),
                Audit("stage_line", head.id, details={"stage": "pay"}),
            ]
        paid = [a for a in paid_runs if a.proposal().body.plan == head.id]  # type: ignore[union-attr]
        ready = not body.payables or bool(paid)
        if (
            ready
            and room
            and head.state is State.OPEN
            and ctx.view.get_value(f"stage:lock:{head.id}") is None
        ):
            lead = f"**{paid[0].id}** paid · " if paid else ""
            effects += [
                SetValue(f"stage:lock:{head.id}", ctx.now.isoformat()),
                Post(
                    room,
                    f"{lead}period {body.period} is ready to lock. @{BOOKKEEPER} propose the lock.",
                ),
                Audit("stage_line", head.id, details={"stage": "lock"}),
            ]
    return effects


def _daily_board(ctx: Context) -> list[Effect]:
    today = ctx.now.strftime("%Y-%m-%d")
    if ctx.view.get_value("board_date") == today or not ctx.policy.room_id("finance"):
        return []
    return [
        SetValue("board_date", today),
        Post(ctx.policy.room_id("finance"), board_text(ctx)),
        Audit("board_posted", details={"date": today}),
    ]


def _post_board(ctx: Context, ev: MessageEvent) -> list[Effect]:
    today = ctx.now.strftime("%Y-%m-%d")
    room = ctx.policy.room_id("finance") or ev.room_id
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
    if a.state is State.SENT:
        return (
            "the bank's confirmation"
            if not a.result().get("rail_confirms")
            else str(a.result().get("rail", "the rail"))
        )
    if a.kind is Kind.GOALS and a.state is State.OPEN:
        return f"{PLANNER} and {BOOKKEEPER}"
    if a.kind is Kind.CLOSE and a.state is State.OPEN:
        return f"{TREASURY} and {BOOKKEEPER}"
    return "-"


def board_text(ctx: Context) -> str:
    """§14: two sections, numbers from the executors' state, no commentary."""
    live = (
        {State.OPEN, State.DONE}
        | WAITING_STATES
        | IN_FLIGHT_STATES
        | RETRY_STATES
        | AWAITING_STATES
    )
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
    live = (
        WAITING_STATES
        | IN_FLIGHT_STATES
        | RETRY_STATES
        | AWAITING_STATES
        | {State.OPEN, State.DONE}
    )
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
        elif a.state is State.SENT:
            extra = f"sent · waiting on {_waiting_on(ctx, a)}"
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
    if a.result_json:
        lines.append(action_phrase(a, a.result()))
    return "\n".join(lines)
