"""In-memory adapters for tests and `github_mode: simulated` (DDD §2, §15.3). They record every
call, can be told to fail, and never touch a network."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from sindarin.adapters.base import ActionResult, AdapterError, Facts, VerifyResult
from sindarin.schema import Artifact, Kind, Proposal

FAKE_TOKEN = "ghp_FAKE_TOKEN_NEVER_LOGGED_0123456789"


def fake_sha(seed: str) -> str:
    return hashlib.sha1(seed.encode()).hexdigest()  # noqa: S324 - a test fixture, not security


@dataclass
class FakePullRequest:
    number: int
    head_sha: str
    base: str = "main"
    state: str = "open"
    merged: bool = False
    draft: bool = False
    author_login: str = "coder-bot"
    title: str = ""
    merge_commit_sha: str | None = None
    branch: str = ""
    body: str = ""
    heads: list[str] = field(default_factory=list)  # every head the branch has had
    merged_seq: int = 0


@dataclass
class FakeGitHub:
    """Enough of GitHub for the merge and release gates."""

    platform: str = "github"
    kinds: frozenset[Kind] = frozenset({Kind.PR, Kind.RELEASE})
    repo: str = "acme/sindarin-demo-app"
    pulls: dict[int, FakePullRequest] = field(default_factory=dict)
    commits: set[str] = field(default_factory=set)
    tags: dict[str, str] = field(default_factory=dict)
    default_branch: str = "main"
    default_branch_head: str = field(default_factory=lambda: fake_sha("main-0"))
    calls: list[tuple[str, str]] = field(default_factory=list)
    fail_execute_times: int = 0
    fail_verify_times: int = 0
    token: str = FAKE_TOKEN  # exists only so tests can assert it never leaks
    next_number: int = 41
    path: str | None = None  # `github_mode: simulated`: a JSON file the agents' stand-ins share

    def __post_init__(self) -> None:
        self.commits.add(self.default_branch_head)
        self._load()

    # -- persistence (simulated mode shares one repository between processes) ----------------

    def _load(self) -> None:
        if not self.path or not Path(self.path).exists():
            return
        data = json.loads(Path(self.path).read_text(encoding="utf-8"))
        self.repo = data.get("repo", self.repo)
        self.pulls = {int(k): FakePullRequest(**v) for k, v in data.get("pulls", {}).items()}
        self.commits = set(data.get("commits", []))
        self.tags = dict(data.get("tags", {}))
        self.default_branch_head = data.get("default_branch_head", self.default_branch_head)
        self.next_number = int(data.get("next_number", self.next_number))

    def _save(self) -> None:
        if not self.path:
            return
        data = {
            "repo": self.repo,
            "pulls": {str(k): asdict(v) for k, v in self.pulls.items()},
            "commits": sorted(self.commits),
            "tags": self.tags,
            "default_branch_head": self.default_branch_head,
            "next_number": self.next_number,
        }
        Path(self.path).write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")

    # -- what a coder does (the agents' simulated GitHub) ----------------------------------------

    def open_pull(
        self, *, branch: str, title: str, body: str = "", author_login: str = "coder-bot"
    ) -> FakePullRequest:
        self._load()
        number = self.next_number
        self.next_number += 1
        pr = self.add_pull(number, title=title, branch=branch, body=body, author_login=author_login)
        self._save()
        return pr

    def merged_pulls(self) -> list[FakePullRequest]:
        self._load()
        return sorted((p for p in self.pulls.values() if p.merged), key=lambda p: p.merged_seq)

    def pull_files(self, number: int) -> list[dict[str, str]]:
        """A canned diff for the demo feature (DDD §18.1), per head."""
        self._load()
        pr = self.pulls.get(number)
        if pr is None:
            return []
        first = len(pr.heads) <= 1
        files = [
            {
                "filename": "app/orders.py",
                "patch": "+def export_csv(orders, limit=10000): ..."
                + ("" if first else "\n+    header = ','.join(COLUMNS)"),
            },
            {
                "filename": "tests/test_export.py",
                "patch": "+def test_content_type(): ...\n+def test_header_row_when_empty(): ..."
                "\n+def test_size_limit(): ...\n+def test_rows_follow_header(): ...",
            },
        ]
        return files

    # -- fixture helpers --------------------------------------------------------------------

    def add_pull(self, number: int, **over: Any) -> FakePullRequest:
        head = over.pop("head_sha", fake_sha(f"pr-{number}-1"))
        pr = FakePullRequest(number=number, head_sha=head, **over)
        if not pr.branch:
            pr.branch = f"feat/{number}"
        pr.heads = [head]
        self.pulls[number] = pr
        self.commits.add(head)
        self.next_number = max(self.next_number, number + 1)
        self._save()
        return pr

    def push(self, number: int, seed: str | None = None) -> str:
        """A new commit lands on the PR branch: the head moves."""
        self._load()
        pr = self.pulls[number]
        pr.head_sha = fake_sha(seed or f"pr-{number}-{len(pr.heads) + 1}")
        pr.heads.append(pr.head_sha)
        self.commits.add(pr.head_sha)
        self._save()
        return pr.head_sha

    def count(self, op: str) -> int:
        return sum(1 for c in self.calls if c[0] == op)

    # -- Adapter -------------------------------------------------------------------------------

    def _split(self, proposal: Proposal) -> tuple[str, str]:
        if proposal.kind is Kind.PR:
            repo, n = proposal.target.split("#", 1)
            return repo, n
        return tuple(proposal.target.split("@", 1))  # type: ignore[return-value]

    def read_facts(self, proposal: Proposal) -> Facts:
        self.calls.append(("read_facts", proposal.target))
        self._load()
        repo, rest = self._split(proposal)
        if repo != self.repo:
            return Facts(ok=False, error=f"repository {repo!r} not found")
        if proposal.kind is Kind.PR:
            pr = self.pulls.get(int(rest))
            if pr is None:
                return Facts(ok=False, error=f"pull request #{rest} not found")
            return Facts(
                ok=True,
                data={
                    "head_sha": pr.head_sha,
                    "base": pr.base,
                    "state": pr.state,
                    "merged": pr.merged,
                    "draft": pr.draft,
                    "author_login": pr.author_login,
                },
            )
        body: Any = proposal.body
        return Facts(
            ok=True,
            data={
                "commit_exists": body.target_sha in self.commits,
                "tag_exists": rest in self.tags,
                "default_branch_head": self.default_branch_head,
            },
        )

    def execute(self, a: Artifact) -> ActionResult:
        p = a.proposal()
        self.calls.append(("execute", p.target))
        if self.fail_execute_times > 0:
            self.fail_execute_times -= 1
            raise AdapterError("GitHub 502: bad gateway")
        self._load()
        facts = a.facts()
        if a.kind is Kind.PR:
            pr = self.pulls[p.body.number]  # type: ignore[union-attr]
            if pr.merged:
                return ActionResult(ok=True, data={"merge_commit_sha": pr.merge_commit_sha})
            if pr.head_sha != facts.get("head_sha"):
                return ActionResult(ok=False, error="GitHub 409: head sha does not match")
            pr.merged, pr.state = True, "closed"
            pr.merge_commit_sha = fake_sha(f"merge-{pr.number}-{pr.head_sha}")
            pr.merged_seq = 1 + max((q.merged_seq for q in self.pulls.values()), default=0)
            self.commits.add(pr.merge_commit_sha)
            self.default_branch_head = pr.merge_commit_sha
            self._save()
            return ActionResult(ok=True, data={"merge_commit_sha": pr.merge_commit_sha})
        body: Any = p.body
        if body.tag in self.tags:
            return ActionResult(ok=False, error="GitHub 422: tag already exists")
        self.tags[body.tag] = body.target_sha
        self._save()
        return ActionResult(
            ok=True,
            data={
                "html_url": f"https://github.com/{self.repo}/releases/tag/{body.tag}",
                "release_id": len(self.tags),
            },
        )

    def verify(self, a: Artifact) -> VerifyResult:
        p = a.proposal()
        self.calls.append(("verify", p.target))
        if self.fail_verify_times > 0:
            self.fail_verify_times -= 1
            return VerifyResult(present=None, error="GitHub 503")
        self._load()
        if a.kind is Kind.PR:
            pr = self.pulls.get(p.body.number)  # type: ignore[union-attr]
            if pr is None:
                return VerifyResult(present=False)
            return VerifyResult(present=pr.merged, data={"merge_commit_sha": pr.merge_commit_sha})
        tag = p.body.tag  # type: ignore[union-attr]
        if tag in self.tags:
            url = f"https://github.com/{self.repo}/releases/tag/{tag}"
            return VerifyResult(present=True, data={"html_url": url})
        return VerifyResult(present=False)


@dataclass
class FakeSwitchRooms:
    """Enough of the Gateway rooms route for the spec gate."""

    platform: str = "switch"
    kinds: frozenset[Kind] = frozenset({Kind.SPEC})
    rooms: dict[str, dict[str, Any]] = field(default_factory=dict)  # slug → room
    calls: list[tuple[str, str]] = field(default_factory=list)
    fail_execute_times: int = 0
    fail_verify_times: int = 0

    def read_facts(self, proposal: Proposal) -> Facts:
        self.calls.append(("read_facts", proposal.target))
        return Facts(ok=True, data={})

    def execute(self, a: Artifact) -> ActionResult:
        plan = a.facts()
        slug = plan.get("slug") or a.target
        self.calls.append(("execute", slug))
        if self.fail_execute_times > 0:
            self.fail_execute_times -= 1
            raise AdapterError("Gateway 503: unavailable")
        room = self.rooms.get(slug)
        if room is None:
            room = {
                "room_id": f"!room-{slug}:test",
                "name": slug,
                "humans": list(plan.get("humans", [])),
                "agents": list(plan.get("agents", [])),
                "instructions_sha": plan.get("instructions_sha", ""),
            }
            self.rooms[slug] = room
        return ActionResult(
            ok=True,
            data={
                "room_id": room["room_id"],
                "room_name": slug,
                "members": len(room["humans"]) + len(room["agents"]),
            },
        )

    def verify(self, a: Artifact) -> VerifyResult:
        plan = a.facts()
        slug = plan.get("slug") or a.target
        self.calls.append(("verify", slug))
        if self.fail_verify_times > 0:
            self.fail_verify_times -= 1
            return VerifyResult(present=None, error="Gateway 503")
        room = self.rooms.get(slug)
        if room is None:
            return VerifyResult(present=False)
        ok = set(room["humans"]) >= set(plan.get("humans", [])) and set(room["agents"]) >= set(
            plan.get("agents", [])
        )
        return VerifyResult(present=ok, data={"room_id": room["room_id"], "room_name": slug})

    def count(self, op: str) -> int:
        return sum(1 for c in self.calls if c[0] == op)
