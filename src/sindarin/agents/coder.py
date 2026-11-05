"""`coder` (DDD §6.2): implements the spec on a branch, opens the PR, proposes the merge; fixes
on a reviewer failure or an engineer's `changes`. Holds a scoped write token (branches and PRs
on one repository, never main). Never merges, never approves."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sindarin.agents.base import Outgoing, Tool, Turn, human_turns, strict_tool, tool_history
from sindarin.agents.roles import RoleContext, split_fence_message, spoken_to
from sindarin.names import GITHUB_EXECUTOR
from sindarin.propose import build_pr, render
from sindarin.schema import Kind
from sindarin.switch.events import MessageEvent

NAME = "coder"
SPEC_RE = re.compile(r"\bSPEC-\d+\b")

OPEN_PR = strict_tool(
    "open_pr",
    "Open a pull request from an existing branch against the default branch. Returns the PR "
    "number and head sha. Title must carry the spec id.",
    {
        "branch": {"type": "string", "description": "feat/SPEC-n"},
        "title": {"type": "string"},
        "body": {"type": "string", "description": "what was built, which criteria it meets"},
    },
    ["branch", "title", "body"],
)
PROPOSE_MERGE = strict_tool(
    "propose_merge",
    "Validate a merge proposal for an open pull request and return the exact block to post, or "
    "a one-line reason it is not allowed. The head sha is read from GitHub, never typed.",
    {
        "number": {"type": "integer"},
        "spec": {"type": "string", "description": "SPEC-n this PR implements"},
        "justification": {"type": "string", "description": "one line: what the reviewer found"},
    },
    ["number", "spec", "justification"],
)
PUSH_FIX = strict_tool(
    "push_fix",
    "Simulated repository only: land one more commit on the PR branch (the head moves).",
    {"number": {"type": "integer"}, "message": {"type": "string"}},
    ["number", "message"],
)
READ_FILE = strict_tool(
    "read_file",
    "Read a file in the working clone.",
    {"path": {"type": "string"}},
    ["path"],
)
WRITE_FILE = strict_tool(
    "write_file",
    "Write a file in the working clone (whole content).",
    {"path": {"type": "string"}, "content": {"type": "string"}},
    ["path", "content"],
)
RUN_TESTS = strict_tool(
    "run_tests",
    "Run the test suite in the working clone; returns the tail of the output and the exit code.",
    {"args": {"type": "string", "description": "extra pytest args, may be empty"}},
    ["args"],
)
GIT_COMMIT_PUSH = strict_tool(
    "git_commit_push",
    "Commit everything in the working clone to the given branch and push it with the scoped token.",
    {"branch": {"type": "string"}, "message": {"type": "string"}},
    ["branch", "message"],
)


def _confine(workdir: Path, rel: str) -> Path:
    p = (workdir / rel).resolve()
    if workdir.resolve() not in p.parents and p != workdir.resolve():
        raise ValueError("path escapes the working clone")
    return p


def tools(ctx: RoleContext) -> list[Tool]:
    gh = ctx.github

    def open_pr(args: dict[str, Any]) -> tuple[str, bool]:
        if gh is None:
            return "no GitHub client configured", False
        try:
            pr = gh.open_pull(
                branch=str(args["branch"]), title=str(args["title"]), body=str(args.get("body", ""))
            )
        except Exception as exc:
            return f"could not open the pull request: {exc.__class__.__name__}", False
        return f"PR #{pr.number} opened from {pr.branch} at head {pr.head_sha}", True

    def propose_merge(args: dict[str, Any]) -> tuple[str, bool]:
        if gh is None:
            return "no GitHub client configured", False
        number = int(args.get("number", 0))
        pr = gh.pull(number)
        if pr is None:
            return f"PR #{number} not found", False
        try:
            p = build_pr(
                ctx.policy(),
                number=pr.number,
                title=pr.title,
                branch=pr.branch,
                spec=str(args.get("spec", "")),
                repo=gh.repo,
                head_sha=pr.head_sha,
                justification=str(args.get("justification", "")),
            )
        except ValueError as exc:
            return f"Not allowed: {exc}", False
        return render(p), True

    def push_fix(args: dict[str, Any]) -> tuple[str, bool]:
        if gh is None:
            return "no GitHub client configured", False
        try:
            sha = gh.push_fix(int(args["number"]), str(args.get("message", "")))
        except NotImplementedError:
            return "push with git_commit_push in a real clone", False
        return f"pushed; PR #{args['number']} head is now {sha}", True

    out = [Tool(OPEN_PR, open_pr), Tool(PROPOSE_MERGE, propose_merge), Tool(PUSH_FIX, push_fix)]
    workdir = ctx.workdir
    if workdir is not None:

        def read_file(args: dict[str, Any]) -> tuple[str, bool]:
            try:
                return _confine(workdir, str(args["path"])).read_text(encoding="utf-8")[
                    :20000
                ], True
            except (OSError, ValueError) as exc:
                return f"cannot read: {exc.__class__.__name__}", False

        def write_file(args: dict[str, Any]) -> tuple[str, bool]:
            try:
                path = _confine(workdir, str(args["path"]))
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(str(args.get("content", "")), encoding="utf-8")
                return f"wrote {args['path']}", True
            except (OSError, ValueError) as exc:
                return f"cannot write: {exc.__class__.__name__}", False

        def run_tests(args: dict[str, Any]) -> tuple[str, bool]:
            extra = [a for a in str(args.get("args", "")).split() if re.match(r"^[\w./:=-]+$", a)]
            r = subprocess.run(
                ["uv", "run", "pytest", "-q", *extra],
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=600,
            )
            tail = (r.stdout + r.stderr)[-4000:]
            return f"exit {r.returncode}\n{tail}", r.returncode == 0

        def git_commit_push(args: dict[str, Any]) -> tuple[str, bool]:
            branch = str(args["branch"])
            if not re.match(r"^[\w./-]+$", branch):
                return "bad branch name", False
            cmds = [
                ["git", "checkout", "-B", branch],
                ["git", "add", "-A"],
                ["git", "commit", "-qm", str(args.get("message", "update"))[:200]],
                ["git", "push", "-u", "origin", branch],
            ]
            for c in cmds:
                r = subprocess.run(c, cwd=workdir, capture_output=True, text=True, timeout=120)
                if r.returncode != 0 and not (
                    c[1] == "commit" and "nothing to commit" in r.stdout + r.stderr
                ):
                    return f"{' '.join(c[:2])} failed: {(r.stderr or r.stdout)[-300:]}", False
            return f"pushed {branch}", True

        out += [
            Tool(READ_FILE, read_file),
            Tool(WRITE_FILE, write_file),
            Tool(RUN_TESTS, run_tests),
            Tool(GIT_COMMIT_PUSH, git_commit_push),
        ]
    return out


def context_text(ctx: RoleContext, room_id: str = "") -> str:
    brief = ctx.brief(room_id)
    policy = ctx.policy_path.read_text(encoding="utf-8").strip()
    if ctx.workdir is None:
        mode = (
            "# Working clone\nNone. The repository is simulated for this run: `open_pr` records "
            "the branch as pushed with the feature implemented, `push_fix` lands one more commit. "
            "Do not try to run tests or read files; open the PR and propose the merge, and on a "
            "reviewer failure call `push_fix` then `propose_merge` again."
        )
    else:
        mode = f"# Working clone\n{ctx.workdir}: read_file, write_file, run_tests, git_commit_push."
    return (
        f"{mode}\n\n"
        f"# The spec (room instructions)\n{brief or '(not seen yet)'}\n\n"
        f"# policy.yaml\n```yaml\n{policy}\n```"
    )


def trigger(ev: MessageEvent, ctx: RoleContext, thread: list[Turn]) -> bool:
    if ev.sender == ctx.self_id or not ctx.is_feature_room(ev.room_id):
        return False
    body = ev.body or ""
    if ev.addressed and ev.sender in ctx.executor_ids():
        return True  # the kickoff brief, or `changes PR-n` sent back
    if spoken_to(ev, ctx, thread):
        return True
    # the reviewer's report names the coder when something is not met
    return "@coder" in body and ev.sender in ctx.agent_ids()


def header(ev: MessageEvent, ctx: RoleContext, thread: list[Turn]) -> str:
    spec = next(iter(SPEC_RE.findall(ctx.brief(ev.room_id) + " " + (ev.body or ""))), "")
    gh = ctx.github
    prs = ""
    if gh is not None and (m := re.search(r"PR[ #-](\d+)", ev.body or "")):
        pr = gh.pull(int(m.group(1)))
        if pr:
            prs = f" · PR #{pr.number} head {pr.head_sha} branch {pr.branch}"
    return (
        f"[Room {ctx.room_name(ev.room_id)} (feature) · "
        f"thread root {ev.thread_id or ev.message_id} · "
        f"spec {spec or '?'} · repo {gh.repo if gh else '?'}{prs}]"
    )


def route(text: str, ev: MessageEvent, ctx: RoleContext, thread: list[Turn]) -> list[Outgoing]:
    before, fence, after = split_fence_message(text)
    root = ev.thread_id or ev.message_id
    if not fence:
        return [Outgoing(ev.room_id, text, root)]
    if ev.thread_id and thread and "```sindarin-proposal" in "".join(t.body for t in thread):
        # a re-proposal inside the PR's own thread
        return [Outgoing(ev.room_id, "\n".join(x for x in (before, fence, after) if x), root)]
    outs = []
    if before:
        outs.append(Outgoing(ev.room_id, before, root))
    outs.append(Outgoing(ev.room_id, "\n".join(x for x in (fence, after) if x), None))
    return outs


# -- the stand-in (SINDARIN_MODEL=fake): a recorded path through the demo feature (§18.1, §20)


def fake_step(ctx: RoleContext) -> Callable[[list[dict[str, Any]], str], tuple[Any, ...]]:
    def step(messages: list[dict[str, Any]], system: str) -> tuple[Any, ...]:
        header_line = messages[0]["content"].splitlines()[0] if messages else ""
        spec = next(iter(SPEC_RE.findall(header_line)), "SPEC-1")
        turns = human_turns(messages)
        last = turns[-1] if turns else ""
        history = tool_history(messages)
        done = {name for name, _, _, ok in history if ok}
        engineer = _engineer_mention(ctx)
        if history and history[-1][0] == "propose_merge":
            name, args, out, ok = history[-1]
            if ok:
                return (
                    "text",
                    f"Merge proposal for #{args['number']} at the current head; "
                    f"the reviewer's report follows.\n{out}\n"
                    f"Your call, {engineer}: `@{GITHUB_EXECUTOR} approve PR-{args['number']}`.",
                )
            return ("text", f"I can't propose that merge: {out}")
        number = _pr_number(header_line, turns)
        kickoff = "instructions" in last and "SPEC-" in last
        fixing = (
            "not met" in last or "sent back" in last or ("of 4" in last and "4 of 4" not in last)
        )
        all_met = "All criteria met" in last or "4 of 4" in last
        if kickoff or ("open_pr" not in done and not number and not fixing):
            if "open_pr" in done or number:
                return (
                    "tool",
                    "propose_merge",
                    {
                        "number": number or _opened(history),
                        "spec": spec,
                        "justification": "tests pass locally; ready for review",
                    },
                )
            return (
                "tool",
                "open_pr",
                {
                    "branch": f"feat/{spec}",
                    "title": "CSV export of orders",
                    "body": (
                        f"Implements {spec}: GET /orders.csv, header row, 413 above 10,000 rows, "
                        "tests."
                    ),
                },
            )
        if fixing and number:
            if "push_fix" not in done:
                return (
                    "tool",
                    "push_fix",
                    {"number": number, "message": "header row when there are no orders"},
                )
            return (
                "tool",
                "propose_merge",
                {
                    "number": number,
                    "spec": spec,
                    "justification": "fixed criterion 2: header row on empty export",
                },
            )
        if all_met and number and demo_agent_refusal():
            # DDD §18.2 shot 32–44: the agent tries what it must never do, on camera, and the
            # executor refuses it by participant id. Only with SINDARIN_DEMO_AGENT_REFUSAL=1.
            return ("text", f"@{GITHUB_EXECUTOR} approve PR-{number}")
        if number:
            return (
                "text",
                f"{engineer} noted. PR-{number} is pending your approval; nothing more from me.",
            )
        return (
            "tool",
            "propose_merge",
            {
                "number": _opened(history),
                "spec": spec,
                "justification": "tests pass locally; ready for review",
            },
        )

    return step


def demo_agent_refusal() -> bool:
    import os

    return os.environ.get("SINDARIN_DEMO_AGENT_REFUSAL", "") == "1"


def _opened(history) -> int:
    opened = next((out for name, _, out, ok in history if name == "open_pr" and ok), "")
    m = re.search(r"PR #(\d+)", opened)
    return int(m.group(1)) if m else 0


def _pr_number(header_line: str, turns: list[str]) -> int:
    m = re.search(r"PR #(\d+)", header_line)
    if m:
        return int(m.group(1))
    for t in reversed(turns):
        m = re.search(r"\bPR-(\d+)\b", t)
        if m:
            return int(m.group(1))
    return 0


def _engineer_mention(ctx: RoleContext) -> str:
    p = ctx.policy()
    rule = next(iter(p.rules_for(Kind.PR)), None)
    members = p.group_members(rule.approvers) if rule else []
    return ctx.mention(members[0]) if members else "@engineer"
