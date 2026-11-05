"""GitHub adapter (DDD §7.1): read a pull request or a release target, merge, publish, read back.
The only code in the GitHub executor that touches GitHub. Holds the one token that can merge."""

from __future__ import annotations

from typing import Any

import httpx

from sindarin.adapters.base import ActionResult, AdapterError, Facts, VerifyResult
from sindarin.schema import Artifact, Kind, Proposal

API_VERSION = "2022-11-28"


class GitHubAdapter:
    platform = "github"
    kinds = frozenset({Kind.PR, Kind.RELEASE})

    def __init__(
        self,
        token: str,
        *,
        base_url: str = "https://api.github.com",
        timeout: float = 20.0,
        merge_method: str = "squash",
    ) -> None:
        if not token:
            raise AdapterError("GITHUB_TOKEN is not set")
        self.merge_method = merge_method
        self._http = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "Authorization": f"Bearer {token}",
                "User-Agent": "sindarin-github",
            },
        )

    def close(self) -> None:
        self._http.close()

    # -- http -----------------------------------------------------------------------------------

    def _call(self, method: str, path: str, *, json: Any = None) -> httpx.Response:
        try:
            return self._http.request(method, path, json=json)
        except httpx.HTTPError as exc:  # never include the request headers in the message
            raise AdapterError(
                f"GitHub unreachable: {exc.__class__.__name__} on {method} {path}"
            ) from None

    @staticmethod
    def _detail(r: httpx.Response) -> str:
        try:
            msg = r.json().get("message")
        except ValueError:
            msg = None
        return f"GitHub {r.status_code}" + (f": {msg}" if msg else "")

    @staticmethod
    def _split(proposal: Proposal) -> tuple[str, str]:
        if proposal.kind is Kind.PR:
            repo, n = proposal.target.split("#", 1)
            return repo, n
        repo, tag = proposal.target.split("@", 1)
        return repo, tag

    # -- facts (P4, A6) -------------------------------------------------------------------------

    def read_facts(self, proposal: Proposal) -> Facts:
        try:
            if proposal.kind is Kind.PR:
                return self._pr_facts(proposal)
            if proposal.kind is Kind.RELEASE:
                return self._release_facts(proposal)
        except AdapterError as exc:
            return Facts(ok=False, error=str(exc))
        return Facts(ok=False, error=f"unsupported kind {proposal.kind.value}")

    def _pr_facts(self, proposal: Proposal) -> Facts:
        repo, n = self._split(proposal)
        r = self._call("GET", f"/repos/{repo}/pulls/{n}")
        if r.status_code == 404:
            return Facts(ok=False, error=f"pull request {proposal.target} not found")
        if r.status_code != 200:
            return Facts(ok=False, error=f"{self._detail(r)} on pull request lookup")
        d = r.json()
        return Facts(
            ok=True,
            data={
                "head_sha": (d.get("head") or {}).get("sha"),
                "base": (d.get("base") or {}).get("ref"),
                "state": d.get("state"),
                "merged": bool(d.get("merged")),
                "draft": bool(d.get("draft")),
                "author_login": (d.get("user") or {}).get("login"),
            },
        )

    def _release_facts(self, proposal: Proposal) -> Facts:
        repo, tag = self._split(proposal)
        body: Any = proposal.body
        r = self._call("GET", f"/repos/{repo}")
        if r.status_code != 200:
            return Facts(ok=False, error=f"{self._detail(r)} on repository lookup")
        default_branch = r.json().get("default_branch") or "main"
        c = self._call("GET", f"/repos/{repo}/commits/{body.target_sha}")
        if c.status_code not in (200, 404, 422):
            return Facts(ok=False, error=f"{self._detail(c)} on commit lookup")
        t = self._call("GET", f"/repos/{repo}/git/ref/tags/{tag}")
        if t.status_code not in (200, 404):
            return Facts(ok=False, error=f"{self._detail(t)} on tag lookup")
        h = self._call("GET", f"/repos/{repo}/commits/{default_branch}")
        if h.status_code != 200:
            return Facts(ok=False, error=f"{self._detail(h)} on branch head lookup")
        return Facts(
            ok=True,
            data={
                "commit_exists": c.status_code == 200,
                "tag_exists": t.status_code == 200,
                "default_branch_head": h.json().get("sha"),
            },
        )

    # -- the one write --------------------------------------------------------------------------

    def execute(self, a: Artifact) -> ActionResult:
        p = a.proposal()
        if a.kind is Kind.PR:
            return self._merge(p, a.facts())
        if a.kind is Kind.RELEASE:
            return self._release(p)
        raise AdapterError(f"unsupported kind {a.kind.value}")

    def _merge(self, p: Proposal, facts: dict[str, Any]) -> ActionResult:
        repo, n = self._split(p)
        body: Any = p.body
        # `sha` makes GitHub refuse the merge if the head moved after the snapshot (409)
        r = self._call(
            "PUT",
            f"/repos/{repo}/pulls/{n}/merge",
            json={
                "merge_method": self.merge_method,
                "sha": facts.get("head_sha"),
                "commit_title": f"{body.title} (#{n}) [{body.spec}]",
            },
        )
        if r.status_code == 200:
            d = r.json()
            return ActionResult(ok=True, data={"merge_commit_sha": d.get("sha")})
        if r.status_code == 405:
            return ActionResult(ok=False, error=f"{self._detail(r)} (not mergeable)")
        if r.status_code == 409:
            return ActionResult(ok=False, error=f"{self._detail(r)} (head moved)")
        return ActionResult(ok=False, error=f"{self._detail(r)} on merge")

    def _release(self, p: Proposal) -> ActionResult:
        repo, tag = self._split(p)
        body: Any = p.body
        r = self._call(
            "POST",
            f"/repos/{repo}/releases",
            json={
                "tag_name": tag,
                "target_commitish": body.target_sha,
                "name": tag,
                "body": body.notes,
                "draft": False,
                "prerelease": False,
            },
        )
        if r.status_code == 201:
            d = r.json()
            return ActionResult(
                ok=True, data={"html_url": d.get("html_url"), "release_id": d.get("id")}
            )
        return ActionResult(ok=False, error=f"{self._detail(r)} on create release")

    # -- read back ------------------------------------------------------------------------------

    def verify(self, a: Artifact) -> VerifyResult:
        p = a.proposal()
        try:
            if a.kind is Kind.PR:
                repo, n = self._split(p)
                r = self._call("GET", f"/repos/{repo}/pulls/{n}")
                if r.status_code != 200:
                    return VerifyResult(
                        present=None, error=f"{self._detail(r)} on pull request lookup"
                    )
                d = r.json()
                return VerifyResult(
                    present=bool(d.get("merged")),
                    data={"merge_commit_sha": d.get("merge_commit_sha")},
                )
            repo, tag = self._split(p)
            r = self._call("GET", f"/repos/{repo}/releases/tags/{tag}")
            if r.status_code == 404:
                return VerifyResult(present=False)
            if r.status_code != 200:
                return VerifyResult(present=None, error=f"{self._detail(r)} on release lookup")
            d = r.json()
            return VerifyResult(
                present=True, data={"html_url": d.get("html_url"), "release_id": d.get("id")}
            )
        except AdapterError as exc:
            return VerifyResult(present=None, error=str(exc))

    # -- read-only smoke (`sindarin adapter smoke`) ------------------------------------------------

    def smoke(self, repo: str, base: str = "main") -> list[tuple[str, bool, str]]:
        """The repository is visible, the token can push, and `base` is protected so only this
        token's identity merges (DDD §4.1 layer 2). No writes."""
        out: list[tuple[str, bool, str]] = []
        r = self._call("GET", f"/repos/{repo}")
        if r.status_code != 200:
            return [(f"repo {repo}", False, f"{self._detail(r)} on repository lookup")]
        perms = r.json().get("permissions") or {}
        out.append((f"repo {repo}", True, f"ok (default branch {r.json().get('default_branch')})"))
        out.append(
            (
                "token can push",
                bool(perms.get("push")),
                "ok" if perms.get("push") else "token lacks contents write",
            )
        )
        b = self._call("GET", f"/repos/{repo}/branches/{base}/protection")
        if b.status_code == 200:
            out.append((f"{base} protected", True, "ok"))
        elif b.status_code == 404:
            out.append(
                (f"{base} protected", False, "no branch protection: anyone with push merges")
            )
        else:
            out.append((f"{base} protected", False, f"{self._detail(b)} on protection lookup"))
        return out
