"""Harness for the adversarial tests (design v5 §2, §8): both executors on a real ledger file and
a fake Gateway, event builders in the real frame shape, a frozen clock, and a report writer.
Everything runs against executor.core through two Effectors; no Switch, no network."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from sindarin.adapters.fake import FakeSwitchRooms
from sindarin.adapters.ledger import LedgerAdapter
from sindarin.agents.finance import plan_goals, report_lines, standard_budget_lines
from sindarin.executor import core
from sindarin.executor.runner import Effector, RunnerConfig
from sindarin.ledger import Ledger
from sindarin.names import LEDGER_EXECUTOR, SWITCH_EXECUTOR
from sindarin.policy import load_policy
from sindarin.propose import build_budget, build_entries, build_goals, render
from sindarin.schema import Artifact, Proposal, canonical_json, is_status_fence
from sindarin.store import Store
from sindarin.switch.events import MessageEvent, message_envelope, parse_frame

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
REPORTS = HERE / "reports"
DATA = HERE.parent / "data" / "acme"
POLICY = load_policy(FIXTURES / "policy.yaml")

BASIS = "2026-10"  # the month in the books
PERIOD = "2026-11"  # the month being planned
FINANCE = "!room-acme-finance:test"
PLANNING = "!room-planning:test"
PERIOD_ROOM = f"!room-plan-{PERIOD}:test"  # what FakeSwitchRooms names the room it opens
ASK = "m-ask-1"  # the business's ask: the thread the goals live in

ISAAC = "@switch-user-isaac:test"
LEONARDO = "@switch-user-leonardo:test"
JOHN = "@switch-user-john:test"
VINCENT = "@switch-user-vincent:test"
ANALYST_AGENT = "@switch-agent-analyst:test"
PLANNER_AGENT = "@switch-agent-planner:test"
BOOKKEEPER_AGENT = "@switch-agent-bookkeeper:test"
SWITCH_EXEC = "@switch-agent-sindarin-switch:test"
LEDGER_EXEC = "@switch-agent-sindarin-ledger:test"
NAMES = {
    ISAAC: "Isaac",
    LEONARDO: "Leonardo",
    JOHN: "John",
    VINCENT: "Vincent",
    ANALYST_AGENT: "analyst",
    PLANNER_AGENT: "planner",
    BOOKKEEPER_AGENT: "bookkeeper",
    SWITCH_EXEC: SWITCH_EXECUTOR,
    LEDGER_EXEC: LEDGER_EXECUTOR,
}
ROOM_NAMES = {
    FINANCE: "#acme-finance",
    PLANNING: "#planning",
    PERIOD_ROOM: f"#plan-{PERIOD}",
}
# which executor is a member of which room (Switch delivers events to members only)
MEMBERS = {
    FINANCE: (SWITCH_EXECUTOR,),
    PLANNING: (LEDGER_EXECUTOR,),
    PERIOD_ROOM: (LEDGER_EXECUTOR, SWITCH_EXECUTOR),
}

T0 = datetime(2026, 11, 5, 12, 0, 0, tzinfo=UTC)


class FrozenClock:
    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> datetime:
        self.now += timedelta(**kw)
        return self.now


def room_name(room: str) -> str:
    return ROOM_NAMES.get(room, room)


# -- fence builders (the agents' tools, with the test ledger) ----------------------------------


def goals_proposal(
    books: Ledger,
    *,
    period: str = PERIOD,
    basis: str = BASIS,
    title: str = "Plan November 2026",
    summary: str = "November grows on October.",
    ask: str = ASK,
    justification: str = "the business asked for the plan",
    cost_cap_cents: int | None = None,
    revenue_target_cents: int | None = None,
    rule: str | None = None,
) -> Proposal:
    s = books.summary(basis)
    goals = plan_goals(books, period, basis)
    if cost_cap_cents is not None:
        goals["cost_cap_cents"] = cost_cap_cents
    if revenue_target_cents is not None:
        goals["revenue_target_cents"] = revenue_target_cents
    return build_goals(
        POLICY,
        period=period,
        basis=basis,
        title=title,
        summary=summary,
        report=report_lines(s),
        ask=ask,
        justification=justification,
        rule=rule,
        **goals,
    )


def budget_proposal(
    books: Ledger,
    *,
    period: str = PERIOD,
    goals: str = "GOALS-1",
    cap_cents: int | None = None,
    lines: list[dict[str, Any]] | None = None,
    justification: str = "the departments' shares of the approved cap",
    digest: bool = True,
    rule: str | None = None,
) -> Proposal:
    if lines is None:
        cap = (
            cap_cents
            if cap_cents is not None
            else plan_goals(books, period, BASIS)["cost_cap_cents"]
        )
        lines = standard_budget_lines(cap)
    return build_budget(
        POLICY,
        period=period,
        goals=goals,
        lines=lines,
        digest=books.actuals_digest(BASIS) if digest else "",
        justification=justification,
        rule=rule,
    )


def entries_proposal(
    books: Ledger,
    *,
    period: str = PERIOD,
    goals: str = "GOALS-1",
    budget: str = "BUDGET-1",
    memo: str = "Budget November 2026",
    justification: str = "the budget is recorded; lines from the ledger",
    lines: list[dict[str, Any]] | None = None,
    digest: bool = True,
    rule: str | None = None,
) -> Proposal:
    rows = books.budget(period)
    if lines is None:
        lines = [
            {
                "department": r["department"],
                "account": r["account"],
                "amount_cents": int(r["amount_cents"]),
            }
            for r in rows
        ] or standard_budget_lines(plan_goals(books, period, BASIS)["cost_cap_cents"])
    return build_entries(
        POLICY,
        period=period,
        goals=goals,
        budget=budget,
        lines=lines,
        memo=memo,
        digest=Ledger.budget_digest(rows) if digest else "",
        justification=justification,
        rule=rule,
    )


def proposal_text(p: Proposal) -> str:
    return render(p)


# -- the harness --------------------------------------------------------------------------------


@dataclass
class Harness:
    switch: Effector
    ledger: Effector
    books: Ledger
    rooms: FakeSwitchRooms
    clock: FrozenClock
    posts: list[core.Post] = field(default_factory=list)
    log: list[tuple[str, list[core.Effect]]] = field(default_factory=list)
    seq: int = 100
    tmp: Path | None = None

    @property
    def executors(self) -> dict[str, Effector]:
        return {SWITCH_EXECUTOR: self.switch, LEDGER_EXECUTOR: self.ledger}

    # -- event builders (real frame shape) --------------------------------------------------

    def _event(
        self,
        room: str,
        sender: str,
        name: str,
        body: str,
        *,
        mid: str,
        thread: str | None,
        addressed: bool,
    ) -> MessageEvent:
        self.seq += 1
        frame = message_envelope(
            room_id=room,
            sequence=self.seq,
            sender=sender,
            sender_name=name,
            message_id=mid,
            body=body,
            addressed=addressed,
            thread_id=thread,
            timestamp=int(self.clock.now.timestamp() * 1000),
            channel_type="channel_public" if room == FINANCE else "channel_private",
        )
        ev = parse_frame(frame)
        assert isinstance(ev, MessageEvent)
        return ev

    def say(
        self,
        room: str,
        sender: str,
        body: str,
        *,
        mid: str,
        thread: str | None = None,
        addressed: bool = True,
        name: str | None = None,
        label: str | None = None,
        to: tuple[str, ...] | None = None,
    ) -> list[core.Effect]:
        """Deliver one message to every executor that is a member of the room (or to `to`)."""
        name = name if name is not None else NAMES.get(sender, sender)
        ev = self._event(room, sender, name, body, mid=mid, thread=thread, addressed=addressed)
        effects: list[core.Effect] = []
        for handle in to or MEMBERS.get(room, ()):
            effects += self.executors[handle].handle_message(ev)
        self.log.append(
            (label or f"{name} in {room_name(room)}: {body.splitlines()[0][:90]}", effects)
        )
        return effects

    def command(
        self,
        mid: str,
        body: str,
        *,
        sender: str,
        room: str,
        executor: str = LEDGER_EXECUTOR,
        thread: str | None = None,
    ) -> list[core.Effect]:
        return self.say(room, sender, f"@{executor} {body}", mid=mid, thread=thread, to=(executor,))

    # -- the three gates --------------------------------------------------------------------

    def propose_goals(self, mid: str = "m-goals-1", **kw: Any) -> list[core.Effect]:
        text = proposal_text(goals_proposal(self.books, **kw))
        return self.say(FINANCE, PLANNER_AGENT, text, mid=mid, thread=kw.get("ask", ASK))

    def approve_goals(
        self, mid: str = "m-approve-goals", *, sender: str = JOHN, aid: str = "GOALS-1"
    ) -> list[core.Effect]:
        return self.command(
            mid, f"approve {aid}", sender=sender, room=FINANCE, executor=SWITCH_EXECUTOR, thread=ASK
        )

    def propose_budget(
        self, mid: str = "m-budget-1", *, room: str = PLANNING, thread: str | None = None, **kw: Any
    ) -> list[core.Effect]:
        return self.say(
            room,
            PLANNER_AGENT,
            proposal_text(budget_proposal(self.books, **kw)),
            mid=mid,
            thread=thread,
        )

    def approve_budget(
        self,
        mid: str = "m-approve-budget",
        *,
        sender: str = ISAAC,
        aid: str = "BUDGET-1",
        room: str = PLANNING,
        thread: str | None = "m-budget-1",
    ) -> list[core.Effect]:
        return self.command(mid, f"approve {aid}", sender=sender, room=room, thread=thread)

    def propose_entries(
        self, mid: str = "m-entries-1", *, room: str = PERIOD_ROOM, **kw: Any
    ) -> list[core.Effect]:
        return self.say(
            room, BOOKKEEPER_AGENT, proposal_text(entries_proposal(self.books, **kw)), mid=mid
        )

    def approve_entries(
        self,
        mid: str = "m-approve-entries",
        *,
        sender: str = LEONARDO,
        aid: str = "ENTRIES-1",
        room: str = PERIOD_ROOM,
        thread: str | None = "m-entries-1",
    ) -> list[core.Effect]:
        return self.command(mid, f"approve {aid}", sender=sender, room=room, thread=thread)

    def sweep(self, label: str = "sweep") -> list[core.Effect]:
        effects = self.switch.sweep() + self.ledger.sweep()
        self.log.append((label, effects))
        return effects

    def run_goals_gate(self) -> Artifact:
        self.propose_goals()
        self.approve_goals()
        self.sweep("sweep: stage lines")
        a = self.goals()
        assert a is not None and a.state.value == "open", a
        return a

    def run_budget_gate(self, *, sender: str = ISAAC) -> Artifact:
        self.propose_budget()
        self.approve_budget(sender=sender)
        self.sweep("sweep: stage lines")
        a = self.budget()
        assert a is not None, "no BUDGET-1"
        return a

    def run_entries_gate(self) -> Artifact:
        self.propose_entries()
        self.approve_entries()
        self.sweep("sweep: goals done")
        a = self.entries()
        assert a is not None, "no ENTRIES-1"
        return a

    # -- reads --------------------------------------------------------------------------------

    def audit_events(self, executor: str = LEDGER_EXECUTOR) -> list[str]:
        return [e.event for e in self.executors[executor].store.audit_all()]

    def all_audit(self) -> list[tuple[str, Any]]:
        rows = []
        for handle, ef in self.executors.items():
            rows += [(handle, e) for e in ef.store.audit_all()]
        return sorted(rows, key=lambda r: (r[1].ts, r[1].id or 0))

    def posts_to(self, room: str, *, fences: bool = False) -> list[core.Post]:
        return [
            p for p in self.posts if p.room_id == room and (fences or not is_status_fence(p.text))
        ]

    def goals(self, aid: str = "GOALS-1") -> Artifact | None:
        return self.switch.store.get(aid)

    def budget(self, aid: str = "BUDGET-1") -> Artifact | None:
        return self.ledger.store.get(aid)

    def entries(self, aid: str = "ENTRIES-1") -> Artifact | None:
        return self.ledger.store.get(aid)

    def recorded(self) -> list[str]:
        return [r["department"] for r in self.books.budget(PERIOD)]

    def planned(self) -> list[str]:
        return [r["department"] for r in self.books.planned(PERIOD)]

    def assert_no_leaks(self) -> None:
        for p in self.posts:
            if "participant id" not in p.text:
                assert "@switch-user-" not in p.text, f"participant id in a post: {p.text[:80]}"


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    clock = FrozenClock()
    posts: list[core.Post] = []

    def poster(room_id: str, text: str, thread_id: str | None) -> None:
        posts.append(core.Post(room_id, text, thread_id))

    books = Ledger(tmp_path / "books.db")
    books.import_dir(DATA / BASIS)
    rooms = FakeSwitchRooms()
    switch_db, ledger_db = tmp_path / "switch.db", tmp_path / "ledger.db"
    switch_store, ledger_store = Store(switch_db), Store(ledger_db)
    common = dict(
        policy_path=FIXTURES / "policy.yaml",
        users_path=FIXTURES / "users.yaml",
        room_names=dict(ROOM_NAMES),
        post_audit_lines=False,
    )
    switch = Effector(
        switch_store,
        {"switch": rooms},
        poster,
        RunnerConfig(
            executor=SWITCH_EXECUTOR,
            self_id=SWITCH_EXEC,
            peer_ids=frozenset({LEDGER_EXEC}),
            peer_state_db=ledger_db,
            **common,
        ),
        clock=clock,
    )
    ledger = Effector(
        ledger_store,
        {"ledger": LedgerAdapter(books)},
        poster,
        RunnerConfig(
            executor=LEDGER_EXECUTOR,
            self_id=LEDGER_EXEC,
            peer_ids=frozenset({SWITCH_EXEC}),
            peer_state_db=switch_db,
            **common,
        ),
        clock=clock,
    )
    h = Harness(
        switch=switch,
        ledger=ledger,
        books=books,
        rooms=rooms,
        clock=clock,
        posts=posts,
        tmp=tmp_path,
    )
    yield h
    switch_store.close()
    ledger_store.close()
    books.close()


@pytest.fixture
def temp_policy(tmp_path: Path, harness: Harness):
    """Copy the fixture policy to tmp so a test can edit it; both executors read the copy."""

    def _write(mutate) -> Path:
        data = yaml.safe_load((FIXTURES / "policy.yaml").read_text())
        mutate(data)
        path = tmp_path / "policy.yaml"
        path.write_text(yaml.safe_dump(data))
        shutil.copy(FIXTURES / "users.yaml", tmp_path / "users.yaml")
        for ef in (harness.switch, harness.ledger):
            ef.config.policy_path = path
            ef.config.users_path = tmp_path / "users.yaml"
        return path

    return _write


# -- reports -----------------------------------------------------------------------------------


def _fmt_effect(e: core.Effect) -> str:
    match e:
        case core.Post(room_id=r, text=t, thread_id=th):
            where = room_name(r) + (" (in thread)" if th else "")
            return f"post → {where}: “{t.replace(chr(10), ' ⏎ ')}”"
        case core.Audit(event=ev, artifact_id=aid):
            return f"audit `{ev}`" + (f" {aid}" if aid else "")
        case core.RecordMessage(message=m):
            return f"record message {m.message_id} from {m.sender_name}"
        case core.CreateArtifact(artifact=a):
            return f"create {a.id} pending ({a.kind.value} {a.target}, rule {a.rule})"
        case core.Transition(artifact_id=aid, state=s):
            return f"transition {aid} → {s.value}"
        case core.UpdateSnapshot(artifact_id=aid, snapshot_sha=sha):
            return f"new snapshot for {aid} ({sha[:12]}…)"
        case core.RecordApproval(artifact_id=aid, decision=d):
            return f"record {d} on {aid}"
        case core.Execute(artifact_id=aid):
            return f"adapter.execute {aid}"
        case core.Verify(artifact_id=aid):
            return f"adapter.verify {aid}"
        case core.SetValue(key=k):
            return f"set {k}"
    return repr(e)


def write_report(
    name: str, title: str, scenario: str, harness: Harness, verdict: str, must_observe: list[str]
) -> Path:
    REPORTS.mkdir(exist_ok=True)
    lines = [
        f"# {title}",
        "",
        f"_Generated by `uv run pytest validation/{name}.py` against `executor.core` through both "
        f"executors on a real ledger file and `FakeSwitchRooms`, a frozen clock starting "
        f"{T0:%Y-%m-%d %H:%M:%S} UTC, and synthetic events in the real frame shape._",
        "",
        "## Scenario",
        "",
        scenario.strip(),
        "",
        "## Must observe",
        "",
    ]
    lines += [f"- {m}" for m in must_observe]
    lines += ["", "## Events fed and effects observed", ""]
    for label, effects in harness.log:
        lines.append(f"**{label}**")
        lines.append("")
        if not effects:
            lines.append("- (no effects: duplicate, ignored, or no executor in the room)")
        for e in effects:
            lines.append(f"- {_fmt_effect(e)}")
        lines.append("")
    lines += ["## The ledger afterwards", ""]
    t = harness.books.targets(PERIOD)
    if t:
        lines.append(
            f"- targets {PERIOD}: revenue {t['revenue_cents'] / 100:,.2f}, cap "
            f"{t['cost_cap_cents'] / 100:,.2f} ({t['source']})"
        )
    for r in harness.books.budget(PERIOD):
        lines.append(
            f"- budget {r['department']} {r['account']} {r['amount_cents'] / 100:,.2f} ({r['source']})"
        )
    for r in harness.books.planned(PERIOD):
        lines.append(
            f"- planned {r['department']} {r['account']} {r['amount_cents'] / 100:,.2f} ({r['source']})"
        )
    lines.append(f"- period {PERIOD}: {harness.books.period_status(PERIOD)}")
    lines += ["", "## Gateway calls", ""]
    lines += [f"- switch `{op}` {t}" for op, t in harness.rooms.calls] or ["- none"]
    lines += [
        "",
        "## Audit rows",
        "",
        "| executor | ts | event | artifact | actor |",
        "|---|---|---|---|---|",
    ]
    for handle, e in harness.all_audit():
        lines.append(
            f"| {handle} | {e.ts:%H:%M:%S} | `{e.event}` | {e.artifact_id or ''} | {e.actor_name or ''} |"
        )
    lines += ["", "## Verdict", "", verdict.strip(), ""]
    path = REPORTS / f"{name}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


__all__ = ["canonical_json"]
