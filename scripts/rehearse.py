#!/usr/bin/env python3
"""Play the demo shot list (DDD §18.2) against the live stack through the Mattermost API, as the
four people, and print every reply the agents and executors post. A rehearsal, not a test: it
tells you what the rooms will show on camera.

    uv run python scripts/rehearse.py [--stack-env ~/.sindarin/switch-stack/.env] [--demo-pass …]
                                      [--from ask|pr|release] [--wait 25]
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from sindarin.bootstrap import MattermostTarget  # noqa: E402
from sindarin.executor.config import parse_env_text  # noqa: E402
from sindarin.switch.mattermost import MattermostClient, MattermostError  # noqa: E402


class Stage:
    def __init__(self, url: str, team: str, demo_pass: str, wait: float) -> None:
        self.url = url
        self.demo_pass = demo_pass
        self.wait = wait
        self.people: dict[str, MattermostClient] = {}
        self.admin: MattermostClient | None = None
        self.team = team
        self.team_id = ""
        self.seen: set[str] = set()
        self.started_ms = int(time.time() * 1000)  # posts older than the run are never shown

    def login(self, admin_user: str, admin_password: str) -> None:
        self.admin = MattermostClient.login(self.url, admin_user, admin_password)
        self.team_id = self.admin.team_by_name(self.team)["id"]
        for who in ("isaac", "leonardo", "john", "vincent"):
            self.people[who] = MattermostClient.login(
                self.url, f"{who}@sindarin.local", self.demo_pass
            )

    def channel(self, name: str, tries: int = 30) -> dict:
        assert self.admin is not None
        for _ in range(tries):
            ch = self.admin.channel_by_name(self.team_id, name)
            if ch:
                return ch
            time.sleep(1)
        raise SystemExit(f"channel {name} never appeared")

    def say(self, who: str, channel: str, text: str, root_id: str | None = None) -> dict:
        ch = self.channel(channel)
        if who not in ("isaac", "leonardo", "john", "vincent"):
            raise ValueError(who)
        try:
            post = self.people[who].create_post(ch["id"], text, root_id)
        except MattermostError:
            assert self.admin is not None
            self.admin.add_to_channel(ch["id"], self.people[who].user["id"])  # type: ignore[index]
            post = self.people[who].create_post(ch["id"], text, root_id)
        print(f"\n>>> {who} in #{channel}{' (thread)' if root_id else ''}: {text}")
        self.seen.add(post["id"])
        return post

    def watch(
        self, channel: str, *, until: str | None = None, wait: float | None = None
    ) -> list[dict]:
        """Print new posts in a channel for up to `wait` seconds; stop early when `until` matches."""
        assert self.admin is not None
        ch = self.channel(channel)
        deadline = time.time() + (wait or self.wait)
        found: list[dict] = []
        while time.time() < deadline:
            for post in reversed(self.admin.posts(ch["id"], 60)):
                if post["id"] in self.seen or int(post.get("create_at", 0)) < self.started_ms:
                    continue
                self.seen.add(post["id"])
                user = self.admin._req("GET", f"/users/{post['user_id']}")
                name = user.get("username", "?")
                msg = post.get("message", "")
                first = msg.splitlines()[0] if msg else ""
                print(
                    f"    [{name}]{' ↳' if post.get('root_id') else ''} {first[:160]}"
                    + (" …" if len(msg.splitlines()) > 1 else "")
                )
                found.append(post)
                if until and re.search(until, msg):
                    return found
            time.sleep(1.5)
        if until:
            print(f"    !! timed out waiting for /{until}/ in #{channel}")
        return found


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--stack-env", type=Path, default=Path.home() / ".sindarin" / "switch-stack" / ".env"
    )
    p.add_argument("--demo-pass", default="Sindarin-demo-1")
    p.add_argument(
        "--mattermost-env",
        type=Path,
        default=None,
        help="a Mattermost server of your own (default: executor/mattermost.env if present)",
    )
    p.add_argument("--wait", type=float, default=30.0)
    p.add_argument(
        "--agent-refusal",
        action="store_true",
        help="the coder stand-in runs with SINDARIN_DEMO_AGENT_REFUSAL=1",
    )
    p.add_argument(
        "--from",
        dest="start",
        choices=("ask", "pr", "release"),
        default="ask",
        help="resume at a later gate when the earlier ones already happened",
    )
    a = p.parse_args()
    sys.stdout.reconfigure(line_buffering=True)
    stack = parse_env_text(a.stack_env.read_text())
    target = MattermostTarget.resolve(REPO, stack, mattermost_env=a.mattermost_env)
    print(f"mattermost: {target.url} team {target.team_name}")
    stage = Stage(target.url, target.team_name, a.demo_pass, a.wait)
    stage.login(target.admin_user, target.admin_password)

    feat = "feat-csv-export"
    if a.start == "ask":
        run_ask_and_spec(stage)
    if a.start in ("ask", "pr"):
        run_build_and_merge(stage, feat, a)
    run_release(stage)
    return 0


def run_ask_and_spec(stage: Stage) -> None:
    # 0–8 s · the ask
    ask = stage.say(
        "isaac", "acme-shared", "@product we need a CSV export of our orders for the finance team"
    )
    stage.watch("acme-shared", until=r"\?")
    stage.say(
        "isaac", "acme-shared", "all order fields; a 10,000-row limit is fine", root_id=ask["id"]
    )
    stage.watch("acme-shared", until=r"(?i)spec|draft")
    posts = stage.watch("product", until=r"\*\*SPEC-\d+\*\* pending")
    spec_root = next((q["id"] for q in posts if "sindarin-proposal" in q.get("message", "")), None)
    spec = "SPEC-1"
    for q in posts:
        m = re.search(r"\*\*(SPEC-\d+)\*\* pending", q.get("message", ""))
        if m:
            spec = m.group(1)

    # 8–18 s · the spec gate
    stage.say("leonardo", "product", f"@sindarin-switch approve {spec}", root_id=spec_root)
    stage.watch("product", until=r"feature room \*\*#feat-csv-export\*\* is open")


def run_build_and_merge(stage: Stage, feat: str, a: argparse.Namespace) -> None:
    # 18–32 s · build and review
    posts = stage.watch(feat, until=r"\*\*PR-\d+\*\* pending", wait=a.wait * 2)
    posts += stage.watch(feat, until=r"\*\*(\d+) of \1\*\*|All criteria met", wait=a.wait * 2)
    if a.agent_refusal:
        posts += stage.watch(feat, until=r"is an agent, not an approver")
    pr_id = None
    for q in posts:
        m = re.search(r"\*\*(PR-\d+)\*\* pending", q.get("message", ""))
        if m:
            pr_id = m.group(1)
    if pr_id is None:
        raise SystemExit(f"no **PR-n** pending post appeared in #{feat}; nothing to approve")
    pr_root = None
    assert stage.admin is not None
    for q in stage.admin.posts(stage.channel(feat)["id"], 60):
        if "sindarin-proposal" in q.get("message", "") and not q.get("root_id"):
            pr_root = q["id"]

    # 32–44 s · two refusals, then the merge
    stage.say("vincent", feat, f"@sindarin-github approve {pr_id}", root_id=pr_root)
    stage.watch(feat, until=r"is not in the engineering group")
    stage.say("isaac", feat, f"@sindarin-github approve {pr_id}", root_id=pr_root)
    stage.watch(feat, until=r"approved by Isaac · merged", wait=a.wait * 2)


def run_release(stage: Stage) -> None:
    # 44–54 s · the release gate
    stage.say("john", "releases", "@release draft the release")
    posts = stage.watch("releases", until=r"\*\*REL-\d+\*\* pending")
    rel_id = None
    rel_root = None
    for q in posts:
        m = re.search(r"\*\*(REL-\d+)\*\* pending", q.get("message", ""))
        if m:
            rel_id = m.group(1)
        if "sindarin-proposal" in q.get("message", "") and not q.get("root_id"):
            rel_root = q["id"]
    if rel_id is None:
        raise SystemExit("no **REL-n** pending post appeared in #releases; nothing to approve")
    stage.say("john", "releases", f"@sindarin-github approve {rel_id}", root_id=rel_root)
    stage.watch("releases", until=r"approved by John · published")

    # 54–62 s · the customer is told
    stage.watch("acme-shared", until=r"is live")

    # 62–70 s · the board
    stage.say("leonardo", "product", "@sindarin-switch board")
    stage.watch("product", until=r"\*\*Board\*\*")
    print("\nrehearsal done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
