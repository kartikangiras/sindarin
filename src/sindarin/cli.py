"""Command-line entry points (DDD §16.1): `sindarin`, `sindarin-exec`, `sindarin-agent`."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Annotated

import typer

from sindarin import __version__
from sindarin.names import AGENT_ROLES

app = typer.Typer(
    help="Sindarin: propose, audit, bootstrap and check the workflow.",
    no_args_is_help=True,
)
propose_app = typer.Typer(
    help="Validate a proposal and print the block to post verbatim.", no_args_is_help=True
)
adapter_app = typer.Typer(help="Adapter checks.", no_args_is_help=True)
exec_app = typer.Typer(
    help="Sindarin executors: sindarin-switch and sindarin-github.", no_args_is_help=True
)
agent_app = typer.Typer(
    help="Sindarin agents: product, coder, reviewer, release.", no_args_is_help=True
)
app.add_typer(propose_app, name="propose")
app.add_typer(adapter_app, name="adapter")

REPO_ROOT = Path(__file__).resolve().parents[2]


def _fail(reason: str, code: int = 1) -> None:
    print(reason, file=sys.stderr)
    raise typer.Exit(code)


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.INFO if verbose else logging.WARNING)


def _root(repo_root: Path | None) -> Path:
    return repo_root.resolve() if repo_root else REPO_ROOT


def _exec_dir(explicit: Path | None) -> Path:
    from sindarin.executor.config import find_executor_dir

    return find_executor_dir(explicit)


@app.callback()
def _app_root() -> None:
    """Sindarin command group."""


@exec_app.callback()
def _exec_root() -> None:
    """Sindarin executor command group."""


@agent_app.callback()
def _agent_root() -> None:
    """Sindarin agent command group."""


@propose_app.callback()
def _propose_root() -> None:
    """Proposal validators (the agents' one tool, as a command)."""


# ---------------------------------------------------------------------------------------------
# sindarin propose spec | pr | release (DDD §6; the executors never trust this validation)
# ---------------------------------------------------------------------------------------------


def policy_path_from_env() -> Path:
    return Path(
        os.environ.get("SINDARIN_POLICY_PATH") or REPO_ROOT / "agents" / "policy" / "policy.yaml"
    )


@propose_app.command("spec")
def propose_spec(
    title: Annotated[str, typer.Option()],
    press_release: Annotated[str, typer.Option(help="One sentence")],
    faq: Annotated[list[str], typer.Option(help="Exactly three, repeat the flag")],
    criteria: Annotated[list[str], typer.Option(help="Three to five, repeat the flag")],
    target_date: Annotated[str, typer.Option(help="ISO date")],
    engineer: Annotated[str, typer.Option(help="Participant id of the engineer")],
    repo: Annotated[str, typer.Option(help="owner/repo from allowed_repos")],
    slug: Annotated[str, typer.Option(help="Feature room slug, e.g. feat-csv-export")],
    justification: Annotated[str, typer.Option(help="One line, the customer's words")],
    scope_in: Annotated[list[str] | None, typer.Option()] = None,
    scope_out: Annotated[list[str] | None, typer.Option()] = None,
    ask: Annotated[str, typer.Option(help="Ask thread root message id")] = "",
    rule: Annotated[str | None, typer.Option()] = None,
) -> None:
    from sindarin.propose import build_spec, policy_from, render

    try:
        p = build_spec(
            policy_from(policy_path_from_env()),
            title=title,
            press_release=press_release,
            faq=faq,
            criteria=criteria,
            scope_in=scope_in or [],
            scope_out=scope_out or [],
            target_date=target_date,
            engineer=engineer,
            repo=repo,
            slug=slug,
            ask=ask,
            justification=justification,
            rule=rule,
        )
    except ValueError as exc:
        _fail(str(exc))
        return
    print(render(p))


@propose_app.command("pr")
def propose_pr(
    number: Annotated[int, typer.Option()],
    title: Annotated[str, typer.Option()],
    branch: Annotated[str, typer.Option()],
    spec: Annotated[str, typer.Option(help="SPEC-n")],
    repo: Annotated[str, typer.Option()],
    justification: Annotated[str, typer.Option()],
    head_sha: Annotated[str, typer.Option(help="40-hex head, if known")] = "",
    ask: Annotated[str, typer.Option()] = "",
    rule: Annotated[str | None, typer.Option()] = None,
) -> None:
    from sindarin.propose import build_pr, policy_from, render

    try:
        p = build_pr(
            policy_from(policy_path_from_env()),
            number=number,
            title=title,
            branch=branch,
            spec=spec,
            repo=repo,
            head_sha=head_sha,
            ask=ask,
            justification=justification,
            rule=rule,
        )
    except ValueError as exc:
        _fail(str(exc))
        return
    print(render(p))


@propose_app.command("release")
def propose_release(
    tag: Annotated[str, typer.Option()],
    target_sha: Annotated[str, typer.Option()],
    notes: Annotated[str, typer.Option()],
    spec: Annotated[list[str], typer.Option(help="SPEC-n, repeat the flag")],
    repo: Annotated[str, typer.Option()],
    justification: Annotated[str, typer.Option()],
    ask: Annotated[list[str] | None, typer.Option(help="Ask ids, repeat the flag")] = None,
    rule: Annotated[str | None, typer.Option()] = None,
) -> None:
    from sindarin.propose import build_release, policy_from, render

    try:
        p = build_release(
            policy_from(policy_path_from_env()),
            tag=tag,
            target_sha=target_sha,
            notes=notes,
            specs=spec,
            asks=ask or [],
            repo=repo,
            justification=justification,
            rule=rule,
        )
    except ValueError as exc:
        _fail(str(exc))
        return
    print(render(p))


# ---------------------------------------------------------------------------------------------
# sindarin bootstrap / doctor (DDD §17)
# ---------------------------------------------------------------------------------------------


@app.command()
def bootstrap(
    stack_env: Annotated[
        Path | None,
        typer.Option(help="The Docker stack's .env (default ~/.sindarin/switch-stack/.env)"),
    ] = None,
    api: Annotated[
        str | None, typer.Option(help="Bridge/Gateway base URL; default from the stack env")
    ] = None,
    demo_pass: Annotated[
        str, typer.Option(help="Password for the demo people in Mattermost")
    ] = "Sindarin-demo-1",
    write_ids: Annotated[
        bool, typer.Option(help="Write participant ids into the policy files")
    ] = True,
    github: Annotated[
        str | None, typer.Option(help="real | simulated: how sindarin-github reaches GitHub")
    ] = None,
    addressing: Annotated[
        str, typer.Option(help="rooms | open: Switch addressing policy for the six agents")
    ] = "rooms",
    repo_root: Annotated[Path | None, typer.Option(help="Repository root (default: auto)")] = None,
) -> None:
    """Register the six agents, create the four bridged rooms and the demo people, learn every
    participant id from the event stream, write the policy and executor files, pin an empty
    board. Idempotent. Never prints a key or a password."""
    from sindarin.bootstrap import run_bootstrap

    stack_env = stack_env or Path.home() / ".sindarin" / "switch-stack" / ".env"
    if not stack_env.exists():
        _fail(f"stack env not found: {stack_env} (run scripts/switch_stack.sh up)")
    rep = run_bootstrap(
        _root(repo_root),
        stack_env,
        api_url=api,
        demo_pass=demo_pass,
        write_ids=write_ids,
        github_mode=github,
        addressing=addressing,
    )
    for line in rep.lines:
        print(line)
    print("BOOTSTRAP:", "OK" if rep.ok else "FAILED")
    raise typer.Exit(0 if rep.ok else 1)


@app.command()
def doctor(
    stack_env: Annotated[Path | None, typer.Option(help="The Docker stack's .env")] = None,
    repo_root: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Say what is missing and how to fix it, in order. Read-only."""
    from sindarin.doctor import run_doctor

    stack_env = stack_env or Path.home() / ".sindarin" / "switch-stack" / ".env"
    checks = run_doctor(_root(repo_root), stack_env)
    for c in checks:
        print(
            f"{'ok  ' if c.ok else 'FIX '} {c.name:<24} {c.detail}"
            + (f"  → {c.fix}" if not c.ok and c.fix else "")
        )
    bad = [c for c in checks if not c.ok]
    print("DOCTOR:", "all good" if not bad else f"{len(bad)} thing(s) to fix")
    raise typer.Exit(0 if not bad else 1)


# ---------------------------------------------------------------------------------------------
# sindarin audit / board
# ---------------------------------------------------------------------------------------------


@app.command()
def audit(
    last: Annotated[int, typer.Option(help="Rows to show")] = 20,
    md: Annotated[bool, typer.Option("--md", help="Render as a Markdown table")] = False,
    artifact: Annotated[
        str | None, typer.Option(help="Only rows for this SPEC-n / PR-n / REL-n")
    ] = None,
    executor: Annotated[str, typer.Option(help="switch | github | both")] = "both",
    executor_dir: Annotated[
        Path | None, typer.Option(help="Folder holding the state files")
    ] = None,
) -> None:
    """Print the audit log from the executors' state files."""
    from sindarin.executor.config import load_executor
    from sindarin.store import Store

    exec_dir = _exec_dir(executor_dir)
    shorts = ["switch", "github"] if executor == "both" else [executor]
    rows = []
    for short in shorts:
        _env, cfg = load_executor(exec_dir, short)
        db = cfg.db_path(exec_dir)
        if not db.exists():
            continue
        store = Store(db, readonly=True)
        try:
            these = store.audit_for(artifact) if artifact else store.audit_last(last)
        finally:
            store.close()
        rows += [(short, e) for e in these]
    if not rows:
        _fail(f"no audit rows in {exec_dir} for {executor}")
    rows.sort(key=lambda x: x[1].ts)
    rows = rows[-last:] if last > 0 else []
    if md:
        print("| ts (UTC) | executor | event | artifact | actor | details |")
        print("|---|---|---|---|---|---|")
        for short, e in rows:
            details = ", ".join(f"{k}={v}" for k, v in e.details.items()) if e.details else ""
            print(
                f"| {e.ts:%Y-%m-%d %H:%M:%S} | {short} | `{e.event}` | {e.artifact_id or ''} | "
                f"{e.actor_name or ''} | {details} |"
            )
    else:
        for short, e in rows:
            print(f"{short:<7} {e.render_line()}")


@app.command()
def board(
    executor_dir: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Print the board (DDD §14) from both executors' state files, without Switch."""
    from sindarin.executor.config import load_executor
    from sindarin.executor.core import board_text
    from sindarin.executor.runner import Effector, RunnerConfig
    from sindarin.executor.service import runner_config
    from sindarin.store import Store

    exec_dir = _exec_dir(executor_dir)
    _env, cfg = load_executor(exec_dir, "switch")
    db = cfg.db_path(exec_dir)
    if not db.exists():
        _fail(f"no state at {db}")
    store = Store(db, readonly=True)
    rc: RunnerConfig = runner_config(exec_dir, cfg)
    effector = Effector(store, {}, lambda *a: None, rc)
    try:
        print(board_text(effector.context(peers=True)))
    finally:
        store.close()


# ---------------------------------------------------------------------------------------------
# sindarin adapter smoke
# ---------------------------------------------------------------------------------------------


@adapter_app.command("smoke")
def adapter_smoke(
    executor_dir: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Read-only GitHub checks for sindarin-github: the repository is visible, the token can
    push, the base branch is protected. No writes."""
    from sindarin.adapters.base import AdapterError
    from sindarin.adapters.github import GitHubAdapter
    from sindarin.executor.config import load_executor
    from sindarin.policy import load_policy

    exec_dir = _exec_dir(executor_dir)
    env, cfg = load_executor(exec_dir, "github")
    if env.missing("GITHUB_TOKEN"):
        _fail(f"GITHUB_TOKEN missing in {exec_dir / 'github.env'}")
    policy = load_policy(cfg.resolve(exec_dir, cfg.policy_path))
    rules = [r for r in policy.rules if r.repo]
    if not rules:
        _fail("policy.yaml has no pr or release rule with a repo")
    try:
        gh = GitHubAdapter(env.github_token)
    except AdapterError as exc:
        _fail(str(exc))
        return
    ok_all = True
    for repo, base in sorted({(r.repo, r.base) for r in rules}):
        for name, ok, detail in gh.smoke(repo, base):
            ok_all &= ok
            print(f"{'ok  ' if ok else 'FAIL'} {name}: {detail}")
    gh.close()
    raise typer.Exit(0 if ok_all else 1)


# ---------------------------------------------------------------------------------------------
# sindarin-exec run --executor switch|github [--probe]
# ---------------------------------------------------------------------------------------------


@exec_app.command("run")
def exec_run(
    executor: Annotated[str, typer.Option(help="switch | github")],
    probe: Annotated[
        bool, typer.Option("--probe", help="Connect, print connection_state, answer pings, exit")
    ] = False,
    probe_seconds: Annotated[int, typer.Option(help="How long the probe listens")] = 60,
    executor_dir: Annotated[Path | None, typer.Option()] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Run one executor (or its connection probe)."""
    from sindarin.executor.service import run_executor, run_probe

    if executor not in ("switch", "github"):
        _fail("--executor must be switch or github")
    _setup_logging(verbose)
    exec_dir = _exec_dir(executor_dir)
    if probe:
        code = asyncio.run(run_probe(exec_dir, executor, probe_seconds))
    else:
        try:
            code = asyncio.run(run_executor(exec_dir, executor))
        except KeyboardInterrupt:
            code = 0
    raise typer.Exit(code)


@exec_app.command("post")
def exec_post(
    executor: Annotated[str, typer.Option(help="switch | github")],
    room: Annotated[str, typer.Option(help="Room id")],
    text: Annotated[str, typer.Option()] = "sindarin post test",
    thread: Annotated[str | None, typer.Option(help="Message id to reply to")] = None,
    executor_dir: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Verification helper (§12.4 item 3): post as an executor, optionally in a thread."""
    from sindarin.executor.config import load_executor
    from sindarin.switch.client import SwitchClient, SwitchError

    exec_dir = _exec_dir(executor_dir)
    env, _cfg = load_executor(exec_dir, executor)
    if env.missing("SWITCH_BRIDGE_URL", "SWITCH_AGENT_ID", "SWITCH_AGENT_API_KEY"):
        _fail("run `sindarin bootstrap` first")
    client = SwitchClient(env.bridge_url, env.agent_id, env.api_key)
    try:
        print(client.post(room, text, thread, connection_id=env.connection_id if thread else None))
    except SwitchError as exc:
        _fail(f"post failed: {exc}")
    finally:
        client.close()


@exec_app.command("version")
def exec_version() -> None:
    print(__version__)


# ---------------------------------------------------------------------------------------------
# sindarin-agent run <role> | dry-run <role>
# ---------------------------------------------------------------------------------------------


@agent_app.command("run")
def agent_run(
    role: Annotated[str, typer.Argument(help="product | coder | reviewer | release")],
    repo_root: Annotated[Path | None, typer.Option(help="Repository root (default: auto)")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Connect one agent to Switch and let it work in its rooms."""
    from sindarin.agents.service import run_agent

    if role not in AGENT_ROLES:
        _fail(f"unknown role {role!r}; one of {', '.join(AGENT_ROLES)}")
    _setup_logging(verbose)
    try:
        code = asyncio.run(run_agent(_root(repo_root), role))
    except KeyboardInterrupt:
        code = 0
    raise typer.Exit(code)


@agent_app.command("dry-run")
def agent_dry_run(
    role: Annotated[str, typer.Argument(help="product | coder | reviewer | release")],
    text: Annotated[str, typer.Option(help="What the person wrote")],
    sender: Annotated[
        str, typer.Option(help="Sender participant id")
    ] = "@switch-user-demo:localhost",
    name: Annotated[str, typer.Option()] = "Isaac",
    room: Annotated[str, typer.Option(help="customer | product | releases | feature")] = "customer",
    repo_root: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """One model turn without Switch: prints what the agent would post and where."""
    from sindarin.agents.base import ScriptedModel, Turn, anthropic_model_call, is_fake_model
    from sindarin.agents.roles import RoleContext, build_agent
    from sindarin.agents.service import ROLES, github_for
    from sindarin.executor.config import load_env
    from sindarin.policy import load_policy
    from sindarin.switch.events import MessageEvent

    if role not in ROLES:
        _fail(f"unknown role {role!r}; one of {', '.join(AGENT_ROLES)}")
    root = _root(repo_root)
    values = load_env(root / "agents" / role / ".env", env_override=False)
    if values.get("ANTHROPIC_API_KEY"):
        os.environ.setdefault("ANTHROPIC_API_KEY", values["ANTHROPIC_API_KEY"])
    policy_path = root / "agents" / "policy" / "policy.yaml"
    policy = load_policy(policy_path)
    room_id = policy.room_id(room) if room != "feature" else "!room-feature:dry-run"
    ctx = RoleContext(
        role=role,
        repo_root=root,
        policy_path=policy_path,
        users_path=root / "agents" / "policy" / "users.yaml",
        self_id=policy.proposer_id(role) or f"@switch-agent-{role}:dry-run",
        github=github_for(role, root, values),
    )
    module = ROLES[role]
    model_call = ScriptedModel(module.fake_step(ctx)) if is_fake_model() else anthropic_model_call()
    agent = build_agent(module, ctx, model_call, room_id=room_id or "")
    ev = MessageEvent(
        sequence=1,
        room_id=room_id or "",
        addressed=True,
        sender=sender,
        sender_name=name,
        message_id="m-dry-1",
        body=text,
        timestamp=0,
    )
    thread = [Turn(sender, name, text, room_id=room_id or "", message_id="m-dry-1")]
    out = agent.respond(thread, module.header(ev, ctx, thread))
    for o in module.route(out, ev, ctx, thread):
        where = ctx.room_name(o.room_id) if o.room_id else "(no room)"
        print(f"→ {where}" + (f" (thread {o.thread_id})" if o.thread_id else "") + f":\n{o.text}\n")
