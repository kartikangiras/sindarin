"""GitHub adapter against a mock transport (DDD §7.1): facts, merge, release, read-back, smoke.
No network, and no token in any message."""

from __future__ import annotations

import json

import httpx
import pytest
from validation.harness import REPO, pr_proposal, release_proposal

from sindarin.adapters.base import AdapterError
from sindarin.adapters.github import GitHubAdapter
from sindarin.schema import Artifact, Kind, State, make_snapshot

TOKEN = "github_pat_TEST_TOKEN_VALUE"
HEAD = "a" * 40
MAIN = "b" * 40


class Mock:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.pr = {
            "number": 41,
            "head": {"sha": HEAD, "ref": "feat/SPEC-1"},
            "base": {"ref": "main"},
            "state": "open",
            "merged": False,
            "draft": False,
            "user": {"login": "coder-bot"},
            "merge_commit_sha": None,
        }
        self.tags: dict[str, str] = {}
        self.protected = True
        self.merge_status = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        path, method = request.url.path, request.method
        self.calls.append((method, path))
        body = json.loads(request.content) if request.content else {}
        if path == f"/repos/{REPO}":
            return httpx.Response(
                200, json={"default_branch": "main", "permissions": {"push": True, "admin": False}}
            )
        if path == f"/repos/{REPO}/pulls/41" and method == "GET":
            return httpx.Response(200, json=self.pr)
        if path == f"/repos/{REPO}/pulls/41/merge":
            if self.merge_status != 200:
                return httpx.Response(
                    self.merge_status,
                    json={
                        "message": "Head branch was modified"
                        if self.merge_status == 409
                        else "Pull Request is not mergeable"
                    },
                )
            if body.get("sha") != self.pr["head"]["sha"]:
                return httpx.Response(409, json={"message": "Head branch was modified"})
            self.pr.update(merged=True, state="closed", merge_commit_sha="c" * 40)
            return httpx.Response(200, json={"sha": "c" * 40, "merged": True})
        if path.startswith(f"/repos/{REPO}/pulls/"):
            return httpx.Response(404, json={"message": "Not Found"})
        if path == f"/repos/{REPO}/commits/main":
            return httpx.Response(200, json={"sha": MAIN})
        if path.startswith(f"/repos/{REPO}/commits/"):
            sha = path.rsplit("/", 1)[1]
            return httpx.Response(200 if sha in (MAIN, HEAD) else 422, json={"sha": sha})
        if path.startswith(f"/repos/{REPO}/git/ref/tags/"):
            tag = path.rsplit("/", 1)[1]
            return httpx.Response(200 if tag in self.tags else 404, json={})
        if path == f"/repos/{REPO}/releases" and method == "POST":
            if body["tag_name"] in self.tags:
                return httpx.Response(422, json={"message": "Validation Failed"})
            self.tags[body["tag_name"]] = body["target_commitish"]
            return httpx.Response(
                201,
                json={
                    "html_url": f"https://github.com/{REPO}/releases/tag/{body['tag_name']}",
                    "id": 7,
                },
            )
        if path.startswith(f"/repos/{REPO}/releases/tags/"):
            tag = path.rsplit("/", 1)[1]
            if tag in self.tags:
                return httpx.Response(
                    200, json={"html_url": f"https://github.com/{REPO}/releases/tag/{tag}", "id": 7}
                )
            return httpx.Response(404, json={"message": "Not Found"})
        if path == f"/repos/{REPO}/branches/main/protection":
            return httpx.Response(200 if self.protected else 404, json={})
        return httpx.Response(500, json={"message": "unexpected"})


@pytest.fixture
def gh():
    m = Mock()
    adapter = GitHubAdapter(TOKEN)
    adapter._http = httpx.Client(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(m.handler),
        headers=adapter._http.headers,
    )
    return adapter, m


def _artifact(proposal, facts) -> Artifact:
    from datetime import UTC, datetime

    now = datetime(2026, 11, 5, tzinfo=UTC)
    aid = "PR-41" if proposal.kind is Kind.PR else "REL-1"
    snap = make_snapshot(aid, "@a:t", proposal, facts)
    return Artifact(
        id=aid,
        kind=proposal.kind,
        state=State.EXECUTING,
        rule="x",
        room_id="!r",
        thread_id="t",
        proposal_message_id="m",
        proposer_id="@a:t",
        proposer_name="a",
        proposal_json=proposal.model_dump_json(by_alias=True),
        snapshot_json=snap.canonical(),
        snapshot_sha=snap.hash,
        created_at=now,
        expires_at=now,
        attempts=1,
    )


def test_pr_facts_merge_and_verify(gh):
    adapter, m = gh
    facts = adapter.read_facts(pr_proposal())
    assert facts.ok and facts.data == {
        "head_sha": HEAD,
        "base": "main",
        "state": "open",
        "merged": False,
        "draft": False,
        "author_login": "coder-bot",
    }
    assert not adapter.read_facts(pr_proposal(number=99)).ok
    a = _artifact(pr_proposal(), facts.data)
    assert adapter.verify(a).present is False
    result = adapter.execute(a)
    assert result.ok and result.data == {"merge_commit_sha": "c" * 40}
    sent = [c for c in m.calls if c == ("PUT", f"/repos/{REPO}/pulls/41/merge")]
    assert len(sent) == 1
    v = adapter.verify(a)
    assert v.present is True and v.data["merge_commit_sha"] == "c" * 40


def test_merge_refusals_name_no_token(gh):
    adapter, m = gh
    a = _artifact(pr_proposal(), {"head_sha": "0" * 40, "base": "main"})
    r = adapter.execute(a)  # the stored head differs: GitHub's sha guard refuses
    assert not r.ok and "409" in r.error and "head moved" in r.error and TOKEN not in r.error
    m.merge_status = 405
    r = adapter.execute(_artifact(pr_proposal(), {"head_sha": HEAD}))
    assert not r.ok and "not mergeable" in r.error


def test_release_facts_publish_and_verify(gh):
    adapter, m = gh
    p = release_proposal(target_sha=MAIN)
    facts = adapter.read_facts(p)
    assert facts.ok and facts.data == {
        "commit_exists": True,
        "tag_exists": False,
        "default_branch_head": MAIN,
    }
    assert adapter.read_facts(release_proposal(target_sha="f" * 40)).data["commit_exists"] is False
    a = _artifact(p, facts.data)
    assert adapter.verify(a).present is False
    r = adapter.execute(a)
    assert r.ok and r.data["html_url"].endswith("/releases/tag/v1.4.0")
    assert adapter.read_facts(p).data["tag_exists"] is True
    assert not adapter.execute(a).ok  # second create → 422
    assert adapter.verify(a).present is True


def test_smoke(gh):
    adapter, m = gh
    rows = adapter.smoke(REPO, "main")
    assert [(n, ok) for n, ok, _ in rows] == [
        (f"repo {REPO}", True),
        ("token can push", True),
        ("main protected", True),
    ]
    m.protected = False
    assert adapter.smoke(REPO, "main")[2][1] is False


def test_unreachable_is_an_adapter_error_without_headers():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    adapter = GitHubAdapter(TOKEN)
    adapter._http = httpx.Client(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(boom),
        headers=adapter._http.headers,
    )
    facts = adapter.read_facts(pr_proposal())
    assert not facts.ok and "ConnectError" in facts.error and TOKEN not in facts.error
    v = adapter.verify(_artifact(pr_proposal(), {"head_sha": HEAD}))
    assert v.present is None and TOKEN not in (v.error or "")
    with pytest.raises(AdapterError):
        GitHubAdapter("")
