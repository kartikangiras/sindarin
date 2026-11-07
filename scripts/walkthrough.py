#!/usr/bin/env python3
"""Play the ten-minute demo as one person, the way the landing page describes it: the `user`
account that the bootstrap put in every approver group types every line, and this script watches
the rooms for the executors' and agents' replies. Plan first, then close, then a deal, then a chart.

    uv run python scripts/walkthrough.py

Everything else is yours to type: `read` and `load` your own file, `changes`, `deny`, reports,
exports, charts, `board`, `status`, `brief`. Reads the stack env and the root .env like the
bootstrap does; needs the stack and the seven processes running.
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

PLAN_PERIOD = "2026-11"
BASIS = "2026-10"


class Solo:
    def __init__(self, target: MattermostTarget, wait: float) -> None:
        self.wait = wait
        pw = target.user_password or "Sindarin-demo-1"
        try:
            self.me = MattermostClient.login(target.url, target.user, pw)
        except MattermostError as exc:
            raise SystemExit(f"cannot sign in as {target.user}: {exc}") from None
        self.team = self.me.team_by_name(target.team_name)["id"]
        self.start = int(time.time() * 1000)
        self.seen: set[str] = set()
        self.timeouts = 0
        self.t0 = time.time()

    def channel(self, name: str, tries: int = 40) -> str:
        for _ in range(tries):
            ch = self.me.channel_by_name(self.team, name)
            if ch:
                return ch["id"]
            time.sleep(1.5)
        raise SystemExit(f"channel #{name} never appeared")

    def say(self, channel: str, text: str, root: str | None = None) -> dict:
        post = self.me.create_post(self.channel(channel), text, root)
        print(f"\n>>> user in #{channel}{' (thread)' if root else ''}: {text}")
        self.seen.add(post["id"])
        return post

    def watch(self, channel: str, pattern: str, wait: float | None = None) -> dict | None:
        """Print new replies in the room until one matches `pattern` (or the wait runs out)."""
        deadline = time.time() + (wait or self.wait)
        rx = re.compile(pattern)
        cid = self.channel(channel)
        while time.time() < deadline:
            posts = self.me._req("GET", f"/channels/{cid}/posts?since={self.start}")["posts"]
            for pid, p in sorted(posts.items(), key=lambda kv: kv[1]["create_at"]):
                if pid in self.seen or p["user_id"] == self.me.user["id"]:
                    continue
                self.seen.add(pid)
                first = p["message"].strip().splitlines()[0] if p["message"].strip() else ""
                print(f"    [{self._who(p)}] {first[:170]}")
                if rx.search(p["message"]):
                    return p
            time.sleep(2)
        self.timeouts += 1
        print(f"    !! timed out waiting for /{pattern}/ in #{channel}")
        return None

    def _who(self, p: dict) -> str:
        uid = p.get("user_id", "")
        names = self.__dict__.setdefault("_names", {})
        if uid not in names:
            try:
                names[uid] = self.me._req("GET", f"/users/{uid}").get("username", uid[:8])
            except MattermostError:
                names[uid] = uid[:8]
        return names[uid]

    @staticmethod
    def root_of(p: dict | None) -> str | None:
        return (p.get("root_id") or p.get("id")) if p else None

    def pending_id(self, p: dict | None, prefix: str) -> str:
        m = re.search(rf"\*\*({prefix}-\d+)\*\* pending", p["message"]) if p else None
        return m.group(1) if m else f"{prefix}-1"

    def elapsed(self) -> str:
        return f"{int(time.time() - self.t0) // 60}m{int(time.time() - self.t0) % 60:02d}s"


# -- the chains ---------------------------------------------------------------------------------


def plan_chain(s: Solo) -> None:
    s.say("acme-finance", "@analyst @planner plan November")
    p = s.watch("acme-finance", r"\*\*GOALS-\d+\*\* pending", wait=s.wait * 2)
    goals = s.pending_id(p, "GOALS")
    s.say("acme-finance", f"@sindarin-switch approve {goals}", root=s.root_of(p))
    s.watch("acme-finance", rf"plan room \*\*#plan-{PLAN_PERIOD}\*\* is open")
    p = s.watch("planning", r"\*\*BUDGET-\d+\*\* pending", wait=s.wait * 2)
    budget = s.pending_id(p, "BUDGET")
    root = s.root_of(p)
    s.say("planning", "@sindarin-ledger sale S-2026-1055 3980.00 Contoso Ltd", root=root)
    s.watch("planning", r"landed in 2026-10")
    s.say("planning", f"@sindarin-ledger approve {budget}", root=root)
    s.watch("planning", r"the ledger changed since the proposal")
    s.watch("planning", r"\*\*BUDGET-\d+\*\* pending \(revised", wait=s.wait * 2)
    s.say("planning", f"@sindarin-ledger approve {budget}", root=root)
    s.watch("planning", r"approved by .* · recorded", wait=s.wait * 2)
    p = s.watch(f"plan-{PLAN_PERIOD}", r"\*\*ENTRIES-\d+\*\* pending", wait=s.wait * 2)
    entries = s.pending_id(p, "ENTRIES")
    s.say(f"plan-{PLAN_PERIOD}", f"@sindarin-ledger approve {entries}", root=s.root_of(p))
    s.watch(f"plan-{PLAN_PERIOD}", r"approved by .* planned", wait=s.wait * 2)
    s.watch("acme-finance", r"is \*{0,2}planned", wait=s.wait * 2)


def close_chain(s: Solo) -> None:
    s.say("acme-finance", "@analyst @bookkeeper close October")
    p = s.watch("acme-finance", r"\*\*CLOSE-\d+\*\* pending", wait=s.wait * 2)
    close = s.pending_id(p, "CLOSE")
    s.say("acme-finance", f"@sindarin-switch approve {close}", root=s.root_of(p))
    s.watch("acme-finance", rf"period room \*\*#close-{BASIS}\*\* is open")
    p = s.watch("payments", r"\*\*PAY-\d+\*\* pending", wait=s.wait * 2)
    pay = s.pending_id(p, "PAY")
    s.say("payments", f"@sindarin-ledger approve {pay}", root=s.root_of(p))
    s.watch("payments", r"approved by .* · paid", wait=s.wait * 2)
    p = s.watch(f"close-{BASIS}", r"\*\*LOCK-\d+\*\* pending", wait=s.wait * 2)
    lock = s.pending_id(p, "LOCK")
    s.say(f"close-{BASIS}", f"@sindarin-ledger approve {lock}", root=s.root_of(p))
    s.watch(f"close-{BASIS}", r"approved by .* locked", wait=s.wait * 2)
    s.watch("acme-finance", r"is \*{0,2}locked", wait=s.wait * 2)


def deal_chain(s: Solo) -> None:
    s.say("sales", "@sales deal Contoso Ltd on Team at 10,200.00")
    p = s.watch("sales", r"\*\*DEAL-\d+\*\* pending", wait=s.wait * 2)
    deal = s.pending_id(p, "DEAL")
    root = s.root_of(p)
    s.say("sales", "@sindarin-ledger price Team 12500.00", root=root)
    s.watch("sales", r"list price is now")
    s.say("sales", f"@sindarin-ledger approve {deal}", root=root)
    s.watch("sales", r"the ledger changed since the proposal")
    s.watch("sales", r"\*\*DEAL-\d+\*\* pending \(revised", wait=s.wait * 2)
    s.say("sales", f"@sindarin-ledger approve {deal}", root=root)
    s.watch("sales", r"approved by .* · won", wait=s.wait * 2)
    s.watch("acme-finance", rf"\*\*{deal}\*\* won")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--stack-env", type=Path, default=Path.home() / ".sindarin" / "switch-stack" / ".env"
    )
    ap.add_argument("--wait", type=float, default=90.0, help="seconds per stage")
    a = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)
    stack = parse_env_text(a.stack_env.read_text())
    target = MattermostTarget.resolve(REPO, stack)
    print(f"mattermost: {target.url} team {target.team_name} as {target.user}")
    s = Solo(target, a.wait)
    plan_chain(s)
    close_chain(s)
    deal_chain(s)
    s.say("acme-finance", f"@sindarin-switch chart {PLAN_PERIOD} deals")
    s.watch("acme-finance", r"deals by account")
    print(f"\nwalkthrough done in {s.elapsed()} · timeouts: {s.timeouts}")
    return 1 if s.timeouts else 0


if __name__ == "__main__":
    sys.exit(main())
