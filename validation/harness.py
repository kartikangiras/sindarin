"""Harness for the adversarial tests (DDD §15.3): both executors on fake adapters, event builders
in the real frame shape, a frozen clock, and a report writer. Everything runs against
executor.core through two Effectors; no Switch, no GitHub."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from sindarin.adapters.fake import FAKE_TOKEN, FakeGitHub, FakeSwitchRooms, fake_sha
from sindarin.executor import core
from sindarin.executor.runner import Effector, RunnerConfig
from sindarin.names import GITHUB_EXECUTOR, SWITCH_EXECUTOR
from sindarin.schema import (
    PROPOSAL_SCHEMA,
    REVIEW_SCHEMA,
    Artifact,
    Proposal,
    Review,
    canonical_json,
    is_status_fence,
)
from sindarin.store import Store
from sindarin.switch.events import MessageEvent, message_envelope, parse_frame

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
REPORTS = HERE / "reports"

PRODUCT = "!room-product:test"
RELEASES = "!room-releases:test"
CUSTOMER = "!room-acme-shared:test"
FEATURE = (
    "!room-feat-csv-export:test"  # what FakeSwitchRooms names the room for slug feat-csv-export
)
SLUG = "feat-csv-export"
REPO = "acme/sindarin-demo-app"

ISAAC = "@switch-user-isaac:test"
LEONARDO = "@switch-user-leonardo:test"
JOHN = "@switch-user-john:test"
VINCENT = "@switch-user-vincent:test"
PRODUCT_AGENT = "@switch-agent-product:test"
CODER_AGENT = "@switch-agent-coder:test"
REVIEWER_AGENT = "@switch-agent-reviewer:test"
RELEASE_AGENT = "@switch-agent-release:test"
SWITCH_EXEC = "@switch-agent-sindarin-switch:test"
GITHUB_EXEC = "@switch-agent-sindarin-github:test"
NAMES = {
    ISAAC: "Isaac",
    LEONARDO: "Leonardo",
    JOHN: "John",
    VINCENT: "Vincent",
    PRODUCT_AGENT: "product",
    CODER_AGENT: "coder",
    REVIEWER_AGENT: "reviewer",
    RELEASE_AGENT: "release",
    SWITCH_EXEC: SWITCH_EXECUTOR,
    GITHUB_EXEC: GITHUB_EXECUTOR,
}
ROOM_NAMES = {
    PRODUCT: "#product",
    RELEASES: "#releases",
    CUSTOMER: "#acme-shared",
    FEATURE: "#feat-csv-export",
}
# which executor is a member of which room (Switch delivers events to members only)
MEMBERS = {
    PRODUCT: (SWITCH_EXECUTOR,),
    RELEASES: (GITHUB_EXECUTOR,),
    FEATURE: (GITHUB_EXECUTOR,),
    CUSTOMER: (),
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


# -- fence builders ----------------------------------------------------------------------------


def spec_proposal(
    *,
    title: str = "CSV export of orders",
    engineer: str = ISAAC,
    repo: str = REPO,
    slug: str = SLUG,
    criteria: list[str] | None = None,
    ask: str = "m-ask-1",
    justification: str = "Acme asked for a CSV export of orders",
) -> Proposal:
    return Proposal(
        schema=PROPOSAL_SCHEMA,
        kind="spec",
        platform="switch",
        action="open_feature_room",
        target=slug,
        body={
            "title": title,
            "press_release": "Acme can now download their orders as a CSV file from the API.",
            "faq": [
                "Who is it for? Acme's finance team.",
                "What does it cost? Nothing; it is part of the API.",
                "When? Target date below.",
            ],
            "criteria": criteria
            or [
                "GET /orders.csv returns text/csv",
                "a header row is present even when there are no orders",
                "exports above 10,000 rows return 413 with a message",
                "a test covers each of the above",
            ],
            "scope_in": ["CSV export endpoint"],
            "scope_out": ["scheduled exports", "XLSX"],
            "target_date": date(2026, 11, 7).isoformat(),
            "engineer": engineer,
            "repo": repo,
        },
        ask=ask,
        rule="spec",
        justification=justification,
    )


def pr_proposal(
    *,
    number: int = 41,
    title: str = "CSV export of orders",
    branch: str = "feat/SPEC-1",
    spec: str = "SPEC-1",
    repo: str = REPO,
    head_sha: str = "",
    rule: str = "merge",
) -> Proposal:
    return Proposal(
        schema=PROPOSAL_SCHEMA,
        kind="pr",
        platform="github",
        action="merge_pr",
        target=f"{repo}#{number}",
        snapshot_hint=head_sha,
        body={"number": number, "title": title, "branch": branch, "spec": spec},
        ask="m-ask-1",
        rule=rule,
        justification="all criteria met, reviewer 4 of 4",
    )


def release_proposal(
    *,
    tag: str = "v1.4.0",
    target_sha: str,
    specs: list[str] | None = None,
    repo: str = REPO,
    notes: str = "SPEC-1 CSV export of orders, asked by Acme (m-ask-1).",
) -> Proposal:
    return Proposal(
        schema=PROPOSAL_SCHEMA,
        kind="release",
        platform="github",
        action="create_release",
        target=f"{repo}@{tag}",
        snapshot_hint=target_sha,
        body={
            "tag": tag,
            "target_sha": target_sha,
            "notes": notes,
            "specs": specs or ["SPEC-1"],
            "asks": ["m-ask-1"],
        },
        ask="m-ask-1",
        rule="release",
        justification="release manager asked for the Friday release",
    )


def review_fence(
    *,
    pr: str = "PR-41",
    head_sha: str,
    met: list[bool] | None = None,
    evidence: str = "tests/test_export.py",
) -> str:
    met = met if met is not None else [True, True, True, True]
    r = Review(
        schema=REVIEW_SCHEMA,
        pr=pr,
        head_sha=head_sha,
        results=[
            {"criterion": i + 1, "met": m, "evidence": f"{evidence}::test_{i + 1}"}
            for i, m in enumerate(met)
        ],
        summary=f"{sum(met)} of {len(met)} met",
    )
    return f"@{GITHUB_EXECUTOR} review\n{r.render()}"


def proposal_text(p: Proposal) -> str:
    executor = SWITCH_EXECUTOR if p.kind == "spec" else GITHUB_EXECUTOR
    return f"@{executor} proposal\n{p.render()}"


# -- the harness -------------------------------------------------------------------------------


@dataclass
class Harness:
    switch: Effector
    github: Effector
    gh: FakeGitHub
    rooms: FakeSwitchRooms
    clock: FrozenClock
    posts: list[core.Post] = field(default_factory=list)
    log: list[tuple[str, list[core.Effect]]] = field(default_factory=list)
    seq: int = 100
    tmp: Path | None = None

    @property
    def executors(self) -> dict[str, Effector]:
        return {SWITCH_EXECUTOR: self.switch, GITHUB_EXECUTOR: self.github}

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
            channel_type="channel_public" if room in (PRODUCT, RELEASES) else "channel_private",
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
        """Deliver one message to every executor that is a member of the room."""
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
        executor: str = GITHUB_EXECUTOR,
        thread: str | None = None,
    ) -> list[core.Effect]:
        return self.say(room, sender, f"@{executor} {body}", mid=mid, thread=thread)

    def propose_spec(self, mid: str = "m-spec-1", **kw: Any) -> list[core.Effect]:
        return self.say(PRODUCT, PRODUCT_AGENT, proposal_text(spec_proposal(**kw)), mid=mid)

    def approve_spec(
        self, mid: str = "m-ok-spec", spec: str = "SPEC-1", sender: str = LEONARDO
    ) -> list[core.Effect]:
        return self.command(
            mid,
            f"approve {spec}",
            sender=sender,
            room=PRODUCT,
            executor=SWITCH_EXECUTOR,
            thread="m-spec-1",
        )

    def open_pull(self, number: int = 41, **over: Any):
        return self.gh.add_pull(
            number, **{"title": "CSV export of orders", "author_login": "coder-bot", **over}
        )

    def propose_pr(
        self,
        mid: str = "m-pr-1",
        *,
        number: int = 41,
        room: str = FEATURE,
        sender: str = CODER_AGENT,
        **kw: Any,
    ):
        pr = self.gh.pulls.get(number)
        head = pr.head_sha if pr else ""
        return self.say(
            room,
            sender,
            proposal_text(pr_proposal(number=number, head_sha=kw.pop("head_sha", head), **kw)),
            mid=mid,
        )

    def review(
        self,
        mid: str = "m-rev-1",
        *,
        met: list[bool] | None = None,
        number: int = 41,
        sender: str = REVIEWER_AGENT,
        room: str = FEATURE,
    ):
        head = self.gh.pulls[number].head_sha
        return self.say(
            room,
            sender,
            review_fence(pr=f"PR-{number}", head_sha=head, met=met),
            mid=mid,
            thread="m-pr-1",
        )

    def approve_pr(
        self, mid: str = "m-ok-pr", pr: str = "PR-41", *, sender: str = ISAAC, room: str = FEATURE
    ):
        return self.command(mid, f"approve {pr}", sender=sender, room=room, thread="m-pr-1")

    def propose_release(
        self,
        mid: str = "m-rel-1",
        *,
        target_sha: str | None = None,
        sender: str = RELEASE_AGENT,
        room: str = RELEASES,
        **kw: Any,
    ):
        sha = target_sha or self.gh.default_branch_head
        return self.say(
            room, sender, proposal_text(release_proposal(target_sha=sha, **kw)), mid=mid
        )

    def approve_release(
        self, mid: str = "m-ok-rel", rel: str = "REL-1", *, sender: str = JOHN, room: str = RELEASES
    ):
        return self.command(mid, f"approve {rel}", sender=sender, room=room, thread="m-rel-1")

    def sweep(self, label: str = "sweep") -> list[core.Effect]:
        effects = self.switch.sweep() + self.github.sweep()
        self.log.append((f"{label} at {self.clock.now:%H:%M:%S}", effects))
        return effects

    # -- the full happy path, for tests that start later in the chain ------------------------

    def run_spec_gate(self) -> Artifact:
        self.propose_spec()
        self.approve_spec()
        a = self.switch.store.get("SPEC-1")
        assert a is not None and a.state.value == "open", a
        return a

    def run_merge_gate(self, *, review: bool = True) -> Artifact:
        self.open_pull(41)
        self.propose_pr()
        if review:
            self.review()
        self.approve_pr()
        a = self.github.store.get("PR-41")
        assert a is not None and a.state.value == "merged", a
        return a

    # -- queries -------------------------------------------------------------------------------

    def audit_events(self, executor: str = GITHUB_EXECUTOR) -> list[str]:
        return [e.event for e in self.executors[executor].store.audit_all()]

    def all_audit(self) -> list[tuple[str, Any]]:
        out = []
        for handle, ef in self.executors.items():
            out += [(handle, e) for e in ef.store.audit_all()]
        return sorted(out, key=lambda x: (x[1].ts, x[1].id or 0))

    def posts_to(self, room: str, *, fences: bool = False) -> list[core.Post]:
        return [
            p for p in self.posts if p.room_id == room and (fences or not is_status_fence(p.text))
        ]

    def spec(self, aid: str = "SPEC-1") -> Artifact | None:
        return self.switch.store.get(aid)

    def pr(self, aid: str = "PR-41") -> Artifact | None:
        return self.github.store.get(aid)

    def rel(self, aid: str = "REL-1") -> Artifact | None:
        return self.github.store.get(aid)

    def assert_no_leaks(self) -> None:
        """No token anywhere; no participant id in a room post beyond whoami."""
        for _, e in self.all_audit():
            text = canonical_json(e.model_dump(mode="json"))
            assert FAKE_TOKEN not in text and "GITHUB_TOKEN" not in text, "token leaked into audit"
        for p in self.posts:
            assert FAKE_TOKEN not in p.text and "ghp_" not in p.text, "token leaked into a post"
            if "participant id" not in p.text:
                assert "@switch-user-" not in p.text, f"participant id in a post: {p.text[:80]}"


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    clock = FrozenClock()
    posts: list[core.Post] = []

    def poster(room_id: str, text: str, thread_id: str | None) -> None:
        posts.append(core.Post(room_id, text, thread_id))

    gh = FakeGitHub(repo=REPO)
    rooms = FakeSwitchRooms()
    github_db = tmp_path / "github.db"
    github_store = Store(github_db)
    switch_store = Store(":memory:")
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
            peer_ids=frozenset({GITHUB_EXEC}),
            peer_state_db=github_db,
            **common,
        ),
        clock=clock,
    )
    github = Effector(
        github_store,
        {"github": gh},
        poster,
        RunnerConfig(
            executor=GITHUB_EXECUTOR,
            self_id=GITHUB_EXEC,
            peer_ids=frozenset({SWITCH_EXEC}),
            **common,
        ),
        clock=clock,
    )
    h = Harness(
        switch=switch, github=github, gh=gh, rooms=rooms, clock=clock, posts=posts, tmp=tmp_path
    )
    yield h
    switch_store.close()
    github_store.close()


@pytest.fixture
def temp_policy(tmp_path: Path, harness: Harness):
    """Copy the fixture policy to tmp so a test can edit it; both executors read the copy."""

    def _write(mutate) -> Path:
        data = yaml.safe_load((FIXTURES / "policy.yaml").read_text())
        mutate(data)
        path = tmp_path / "policy.yaml"
        path.write_text(yaml.safe_dump(data))
        shutil.copy(FIXTURES / "users.yaml", tmp_path / "users.yaml")
        for ef in (harness.switch, harness.github):
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
        case core.RecordReview(artifact_id=aid, review=r):
            return f"record review {aid}: {r.met} of {r.total}"
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
        f"_Generated by `uv run pytest validation/{name}.py` against `executor.core` through both executors on `FakeGitHub` and `FakeSwitchRooms`, a frozen clock starting {T0:%Y-%m-%d %H:%M:%S} UTC, and synthetic events in the real frame shape (DDD §12.1, §15.3)._",
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
    lines += ["## Adapter calls", ""]
    calls = [f"- github `{op}` {t}" for op, t in harness.gh.calls] + [
        f"- switch `{op}` {t}" for op, t in harness.rooms.calls
    ]
    lines += calls or ["- none"]
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


__all__ = ["fake_sha"]
