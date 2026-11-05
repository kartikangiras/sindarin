"""What the agents read and write on GitHub (DDD §4.2, §6): the coder opens pull requests with
its scoped token; the reviewer and release agents read. In `github_mode: simulated` the same
calls go to the shared in-memory repository file the simulated executor uses."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from sindarin.adapters.fake import FakeGitHub

API_VERSION = "2022-11-28"


@dataclass(frozen=True)
class PullInfo:
    number: int
    head_sha: str
    title: str
    branch: str
    base: str
    state: str
    merged: bool
    author_login: str
    body: str = ""
    merge_commit_sha: str | None = None


class AgentGitHub(Protocol):
    repo: str

    def open_pull(self, *, branch: str, title: str, body: str) -> PullInfo: ...
    def pull(self, number: int) -> PullInfo | None: ...
    def pull_files(self, number: int) -> list[dict[str, Any]]: ...
    def push_fix(self, number: int, message: str) -> str: ...
    def main_head(self) -> str: ...
    def merged_pulls(self) -> list[PullInfo]: ...
    def latest_tag(self) -> str | None: ...


class SimGitHub:
    """The agents' view of the simulated repository (one JSON file, shared with the executor)."""

    def __init__(self, path: str | Path, repo: str) -> None:
        self.fake = FakeGitHub(repo=repo, path=str(path))
        self.repo = repo

    @staticmethod
    def _info(p) -> PullInfo:
        return PullInfo(
            number=p.number,
            head_sha=p.head_sha,
            title=p.title,
            branch=p.branch,
            base=p.base,
            state=p.state,
            merged=p.merged,
            author_login=p.author_login,
            body=p.body,
            merge_commit_sha=p.merge_commit_sha,
        )

    def open_pull(self, *, branch: str, title: str, body: str) -> PullInfo:
        return self._info(self.fake.open_pull(branch=branch, title=title, body=body))

    def pull(self, number: int) -> PullInfo | None:
        self.fake._load()
        p = self.fake.pulls.get(number)
        return self._info(p) if p else None

    def pull_files(self, number: int) -> list[dict[str, Any]]:
        return self.fake.pull_files(number)

    def push_fix(self, number: int, message: str) -> str:
        return self.fake.push(number)

    def main_head(self) -> str:
        self.fake._load()
        return self.fake.default_branch_head

    def merged_pulls(self) -> list[PullInfo]:
        return [self._info(p) for p in self.fake.merged_pulls()]

    def latest_tag(self) -> str | None:
        self.fake._load()
        return sorted(self.fake.tags)[-1] if self.fake.tags else None


class RealGitHub:
    """api.github.com with whatever token the role holds. The coder's token can push and open
    pull requests on one repository and nothing else; the others hold no token and read the
    public repository anonymously (DDD §4.2)."""

    def __init__(self, token: str, repo: str, *, base_url: str = "https://api.github.com") -> None:
        self.repo = repo
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "sindarin-agent",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._http = httpx.Client(base_url=base_url, timeout=20.0, headers=headers)

    def _get(self, path: str, **params: Any) -> Any:
        r = self._http.get(path, params=params or None)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    @staticmethod
    def _info(d: dict[str, Any]) -> PullInfo:
        return PullInfo(
            number=int(d["number"]),
            head_sha=(d.get("head") or {}).get("sha", ""),
            title=d.get("title", ""),
            branch=(d.get("head") or {}).get("ref", ""),
            base=(d.get("base") or {}).get("ref", ""),
            state=d.get("state", ""),
            merged=bool(d.get("merged") or d.get("merged_at")),
            author_login=(d.get("user") or {}).get("login", ""),
            body=d.get("body") or "",
            merge_commit_sha=d.get("merge_commit_sha"),
        )

    def open_pull(self, *, branch: str, title: str, body: str) -> PullInfo:
        base = (self._get(f"/repos/{self.repo}") or {}).get("default_branch", "main")
        r = self._http.post(
            f"/repos/{self.repo}/pulls",
            json={"title": title, "head": branch, "base": base, "body": body},
        )
        r.raise_for_status()
        return self._info(r.json())

    def pull(self, number: int) -> PullInfo | None:
        d = self._get(f"/repos/{self.repo}/pulls/{number}")
        return self._info(d) if d else None

    def pull_files(self, number: int) -> list[dict[str, Any]]:
        return list(self._get(f"/repos/{self.repo}/pulls/{number}/files", per_page=100) or [])

    def push_fix(self, number: int, message: str) -> str:
        raise NotImplementedError("the real coder pushes with git in its working clone")

    def main_head(self) -> str:
        base = (self._get(f"/repos/{self.repo}") or {}).get("default_branch", "main")
        d = self._get(f"/repos/{self.repo}/commits/{base}") or {}
        return str(d.get("sha", ""))

    def merged_pulls(self) -> list[PullInfo]:
        items = (
            self._get(
                f"/repos/{self.repo}/pulls",
                state="closed",
                sort="updated",
                direction="desc",
                per_page=50,
            )
            or []
        )
        return [self._info(d) for d in items if d.get("merged_at")]

    def latest_tag(self) -> str | None:
        items = self._get(f"/repos/{self.repo}/tags", per_page=1) or []
        return items[0]["name"] if items else None
