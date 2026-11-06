#!/usr/bin/env python3
"""Play the demo (design v5 §3) against the live stack through the Mattermost API, as the
management team, and print every reply the agents and executors post. A rehearsal, not a test:
it tells you what the rooms will show on camera. Needs `sindarin bootstrap --people cast` (or both).

    uv run python scripts/rehearse.py [--stack-env ~/.sindarin/switch-stack/.env] [--demo-pass …]
                                      [--from ask|budget|entries] [--wait 60] [--no-sale]

Plays in the bundled Mattermost, or in a server of your own when the root .env sets MATTERMOST_URL.
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

PERIOD = "2026-11"
BASIS = "2026-10"
PERIOD_ROOM = f"plan-{PERIOD}"
PEOPLE = ("isaac", "leonardo", "john", "vincent")


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
        for who in PEOPLE:
            try:
                self.people[who] = MattermostClient.login(
                    self.url, f"{who}@sindarin.local", self.demo_pass
                )
            except MattermostError as exc:
                raise SystemExit(
                    f"cannot sign in as {who}: {exc}. The rehearsal plays the management team; "
                    "run `uv run sindarin bootstrap --people cast` (or both) first."
                ) from None

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
        if who not in PEOPLE:
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

    def proposal_root(self, channel: str) -> str | None:
        """The thread an executor's `pending` reply lives in: the newest proposal post."""
        assert self.admin is not None
        for q in self.admin.posts(self.channel(channel)["id"], 60):
            if "sindarin-proposal" in q.get("message", ""):
                return q.get("root_id") or q["id"]
        return None


def pending_id(posts: list[dict], prefix: str, fallback: str) -> str:
    for q in posts:
        m = re.search(rf"\*\*({prefix}-\d+)\*\* pending", q.get("message", ""))
        if m:
            return m.group(1)
    return fallback


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--stack-env", type=Path, default=Path.home() / ".sindarin" / "switch-stack" / ".env"
    )
    p.add_argument("--demo-pass", default="Sindarin-demo-1")
    p.add_argument("--wait", type=float, default=60.0, help="seconds per stage")
    p.add_argument(
        "--no-sale",
        action="store_true",
        help="skip the 'the books moved between proposal and approval' moment",
    )
    p.add_argument(
        "--from",
        dest="start",
        choices=("ask", "budget", "entries"),
        default="ask",
        help="resume at a later gate when the earlier ones already happened",
    )
    p.add_argument(
        "--chain",
        choices=("plan", "close", "sales"),
        default="plan",
        help="which chain to play: the planning cycle (plan), the month-end close (close) or a "
        "deal through the discount band (sales)",
    )
    a = p.parse_args()
    sys.stdout.reconfigure(line_buffering=True)
    stack = parse_env_text(a.stack_env.read_text())
    target = MattermostTarget.resolve(REPO, stack)
    print(f"mattermost: {target.url} team {target.team_name}")
    stage = Stage(target.url, target.team_name, a.demo_pass, a.wait)
    stage.login(target.admin_user, target.admin_password)

    if a.chain == "close":
        run_close_chain(stage, amend=not a.no_sale)
        return 0
    if a.chain == "sales":
        run_sales_chain(stage, reprice=not a.no_sale)
        return 0
    if a.start == "ask":
        run_ask_and_goals(stage)
    if a.start in ("ask", "budget"):
        run_budget(stage, sale=not a.no_sale)
    run_entries_and_note(stage)
    return 0


def run_sales_chain(stage: Stage, *, reprice: bool) -> None:
    """The sales chain: a deal at 15% off needs the cfo (the controller is refused with the
    discount and the limit); a list price edited in chat under the pending deal refuses the
    stale approval; the re-proposal is won and the sale lands in the open month."""
    ask = stage.say("vincent", "sales", "@sales deal Contoso Ltd on Team at 10,200.00")
    posts = stage.watch("sales", until=r"\*\*DEAL-\d+\*\* pending", wait=stage.wait * 2)
    deal = pending_id(posts, "DEAL", "DEAL-1")
    root = stage.proposal_root("sales") or ask["id"]
    stage.say("leonardo", "sales", f"@sindarin-ledger approve {deal}", root_id=root)
    stage.watch("sales", until=r"discount needs the cfo group")
    if reprice:
        stage.say("john", "sales", "@sindarin-ledger price Team 12500.00", root_id=root)
        stage.watch("sales", until=r"list price is now 12,500.00 USD")
        stage.say("john", "sales", f"@sindarin-ledger approve {deal}", root_id=root)
        stage.watch("sales", until=r"the ledger changed since the proposal")
        stage.watch("sales", until=r"\*\*DEAL-\d+\*\* pending \(revised", wait=stage.wait * 2)
    stage.say("john", "sales", f"@sindarin-ledger approve {deal}", root_id=root)
    stage.watch("sales", until=r"approved by John · won", wait=stage.wait * 2)
    posts = stage.watch("acme-finance", until=rf"\*\*{deal}\*\* won", wait=stage.wait)
    landed = re.search(r"landed in (\d{4}-\d{2})", " ".join(p.get("message", "") for p in posts))
    period = landed.group(1) if landed else BASIS
    stage.say("john", "acme-finance", f"@sindarin-switch chart {period} deals")
    stage.watch("acme-finance", until=r"deals by account")
    print("\nrehearsal done")


def run_close_chain(stage: Stage, *, amend: bool) -> None:
    """The month-end chain: the plan, the payment run (the limit, a payable amended under the
    run), the lock, the closing note."""
    import subprocess

    close_room = f"close-{BASIS}"
    ask = stage.say("isaac", "acme-finance", "@analyst @bookkeeper close October")
    posts = stage.watch("acme-finance", until=r"\*\*CLOSE-\d+\*\* pending", wait=stage.wait * 2)
    plan = pending_id(posts, "CLOSE", "CLOSE-1")
    stage.say("vincent", "acme-finance", f"@sindarin-switch approve {plan}", root_id=ask["id"])
    stage.watch("acme-finance", until=r"is not in the controllers group")
    stage.say("leonardo", "acme-finance", f"@sindarin-switch approve {plan}", root_id=ask["id"])
    stage.watch("acme-finance", until=rf"period room \*\*#{close_room}\*\* is open")
    posts = stage.watch("payments", until=r"\*\*PAY-\d+\*\* pending", wait=stage.wait * 2)
    pay = pending_id(posts, "PAY", "PAY-1")
    root = stage.proposal_root("payments")
    stage.say("leonardo", "payments", f"@sindarin-ledger approve {pay}", root_id=root)
    stage.watch("payments", until=r"needs the cfo group")
    if amend:
        print("\n>>> (the books move) uv run sindarin ledger amend INV-1042 --amount 4900.00")
        subprocess.run(
            ["uv", "run", "sindarin", "ledger", "amend", "INV-1042", "--amount", "4900.00"],
            cwd=REPO,
            check=False,
        )
        stage.say("john", "payments", f"@sindarin-ledger approve {pay}", root_id=root)
        stage.watch("payments", until=r"the ledger changed since the proposal")
        stage.watch("payments", until=r"\*\*PAY-\d+\*\* pending \(revised", wait=stage.wait * 2)
    stage.say("john", "payments", f"@sindarin-ledger approve {pay}", root_id=root)
    stage.watch("payments", until=r"approved by John · paid", wait=stage.wait * 2)
    posts = stage.watch(close_room, until=r"\*\*LOCK-\d+\*\* pending", wait=stage.wait * 2)
    lock = pending_id(posts, "LOCK", "LOCK-1")
    root = stage.proposal_root(close_room)
    stage.say("leonardo", close_room, f"@sindarin-ledger approve {lock}", root_id=root)
    stage.watch(close_room, until=r"approved by Leonardo · .* locked", wait=stage.wait * 2)
    stage.watch("acme-finance", until=r"(?<!2026-10 )is \*{0,2}locked", wait=stage.wait * 2)
    stage.say("leonardo", "acme-finance", "@sindarin-switch board")
    stage.watch("acme-finance", until=r"\*\*Board\*\*")
    print("\nrehearsal done")


def run_ask_and_goals(stage: Stage) -> None:
    # 0–12 s · the ask: the analyst reports October, the planner proposes November's goals
    ask = stage.say("isaac", "acme-finance", "@analyst @planner plan November")
    posts = stage.watch("acme-finance", until=r"\*\*GOALS-\d+\*\* pending", wait=stage.wait * 2)
    goals = pending_id(posts, "GOALS", "GOALS-1")
    # 12–26 s · the goals gate: a sales person is refused by id, the CFO approves
    stage.say("vincent", "acme-finance", f"@sindarin-switch approve {goals}", root_id=ask["id"])
    stage.watch("acme-finance", until=r"is not in the cfo group")
    stage.say("john", "acme-finance", f"@sindarin-switch approve {goals}", root_id=ask["id"])
    stage.watch("acme-finance", until=rf"plan room \*\*#{PERIOD_ROOM}\*\* is open")


def run_budget(stage: Stage, *, sale: bool) -> None:
    # 26–46 s · the budget gate: the CFO is over the limit; the books move; the board approves
    posts = stage.watch("planning", until=r"\*\*BUDGET-\d+\*\* pending", wait=stage.wait * 2)
    budget = pending_id(posts, "BUDGET", "BUDGET-1")
    root = stage.proposal_root("planning")
    stage.say("john", "planning", f"@sindarin-ledger approve {budget}", root_id=root)
    stage.watch("planning", until=r"needs the board group")
    if sale:
        # the books move, from chat: a late October sale typed to the ledger executor
        stage.say(
            "isaac",
            "planning",
            "@sindarin-ledger sale S-2026-1055 3980.00 Contoso Ltd",
            root_id=root,
        )
        stage.watch("planning", until=r"landed in 2026-10")
        stage.say("isaac", "planning", f"@sindarin-ledger approve {budget}", root_id=root)
        stage.watch("planning", until=r"the ledger changed since the proposal")
        stage.watch("planning", until=r"\*\*BUDGET-\d+\*\* pending \(revised", wait=stage.wait * 2)
    stage.say("isaac", "planning", f"@sindarin-ledger approve {budget}", root_id=root)
    stage.watch("planning", until=r"approved by Isaac · recorded", wait=stage.wait * 2)


def run_entries_and_note(stage: Stage) -> None:
    # 46–60 s · the entries gate
    posts = stage.watch(PERIOD_ROOM, until=r"\*\*ENTRIES-\d+\*\* pending", wait=stage.wait * 2)
    entries = pending_id(posts, "ENTRIES", "ENTRIES-1")
    root = stage.proposal_root(PERIOD_ROOM)
    stage.say("leonardo", PERIOD_ROOM, f"@sindarin-ledger approve {entries}", root_id=root)
    stage.watch(PERIOD_ROOM, until=r"approved by Leonardo · .* planned", wait=stage.wait * 2)
    # 60–70 s · the business is told, the board
    # the analyst's note, not the stage line that woke it ("period 2026-11 is planned")
    stage.watch("acme-finance", until=r"(?<!period 2026-11 )is \*{0,2}planned", wait=stage.wait * 2)
    stage.say("leonardo", "acme-finance", "@sindarin-switch board")
    stage.watch("acme-finance", until=r"\*\*Board\*\*")
    print("\nrehearsal done")


if __name__ == "__main__":
    sys.exit(main())
