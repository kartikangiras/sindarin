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
    help="Sindarin: bootstrap, run and check the planning workflow.",
    no_args_is_help=True,
)
ledger_app = typer.Typer(
    help="The ledger: import a period, show it, land a late sale, reset.", no_args_is_help=True
)
exec_app = typer.Typer(
    help="Sindarin executors: sindarin-switch and sindarin-ledger.", no_args_is_help=True
)
agent_app = typer.Typer(help="Sindarin agents: analyst, planner, bookkeeper.", no_args_is_help=True)
app.add_typer(ledger_app, name="ledger")
people_app = typer.Typer(
    help="The people who approve: add a colleague to the rooms and a group, list, remove.",
    no_args_is_help=True,
)
app.add_typer(people_app, name="people")

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


def _ledger(executor_dir: Path | None):
    from sindarin.executor.config import load_config
    from sindarin.ledger import Ledger

    exec_dir = _exec_dir(executor_dir)
    cfg = load_config(exec_dir / "ledger.yaml")
    return Ledger(cfg.resolve(exec_dir, cfg.ledger_db)), cfg.resolve(exec_dir, cfg.data_dir), cfg


# ---------------------------------------------------------------------------------------------
# sindarin ledger import | show | add-sale | amend | reset (design v5 §5)
# ---------------------------------------------------------------------------------------------


@ledger_app.command("import")
def ledger_import(
    period: Annotated[str | None, typer.Option(help="YYYY-MM; default every folder")] = None,
    sheet: Annotated[
        str | None,
        typer.Option(
            help="A Google Sheets link shared by link: tabs accounts, sales, payables, targets "
            "and, optionally, price_book"
        ),
    ] = None,
    xlsx: Annotated[Path | None, typer.Option(help="An Excel workbook with the same tabs")] = None,
    executor_dir: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Load the books: from data/acme/<period>/ (CSV), from the team's Google Sheet, or from an
    Excel workbook. The sheet's link is remembered in executor/ledger.yaml for the write-back."""
    from sindarin.sheets import SheetError, read_sheet, read_xlsx

    ledger, data_dir, cfg = _ledger(executor_dir)
    if sheet or xlsx:
        try:
            tabs = read_sheet(sheet) if sheet else read_xlsx(xlsx)  # type: ignore[arg-type]
        except (SheetError, OSError) as exc:
            _fail(str(exc))
            return
        counts = ledger.import_tabs(tabs)
        if sheet:
            from sindarin.executor.config import save_config

            cfg.sheet_url = sheet
            save_config(_exec_dir(executor_dir) / "ledger.yaml", cfg)
        print(f"imported from {sheet or xlsx}: {counts}")
        print(f"ledger {ledger.path}: {counts['sales']} sales rows, {counts['payables']} payables")
        return
    folders = [data_dir / period] if period else sorted(p for p in data_dir.iterdir() if p.is_dir())
    total: dict[str, int] = {}
    for folder in folders:
        if not (folder / "sales.csv").exists():
            _fail(f"no sales.csv in {folder}")
        counts = ledger.import_dir(folder)
        for k, v in counts.items():
            total[k] = total.get(k, 0) + v
        print(f"imported {folder.name}: {counts}")
    print(f"ledger {ledger.path}: {total['sales']} sales rows, {total['payables']} payables")


@ledger_app.command("show")
def ledger_show(
    period: Annotated[str | None, typer.Argument(help="YYYY-MM; default the newest")] = None,
    executor_dir: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """The period's state, its targets, its recorded budget, the trial balance, the payables."""
    from sindarin.ledger import render_trial_balance
    from sindarin.schema import money, percent

    ledger, _, _ = _ledger(executor_dir)
    periods = ledger.periods()
    if not periods:
        _fail("the ledger is empty; run `sindarin ledger import`")
    period = period or periods[-1]
    s = ledger.summary(period)
    print(
        f"{period} · {s['period_status']} · revenue {money(s['revenue_cents'])} · costs "
        f"{money(s['costs_cents'])} · margin {percent(s['margin_bps'])}"
    )
    t = s["targets"]
    if t:
        print(
            f"targets ({t['source']}): revenue {money(t['revenue_cents'])} · margin "
            f"{percent(t['margin_bps'])} · cost cap {money(t['cost_cap_cents'])}"
        )
    for row in s["budget"]:
        print(
            f"budget {row['department']} {row['account']} {money(row['amount_cents'])} "
            f"({row['source']})"
        )
    for row in ledger.planned(period):
        print(
            f"planned {row['department']} {row['account']} {money(row['amount_cents'])} "
            f"({row['source']})"
        )
    print(render_trial_balance(ledger, period))
    for row in ledger.payables(period=period):
        tail = f" ({row['run_id']})" if row.get("run_id") else ""
        print(
            f"{row['invoice']} {row['vendor']} {money(row['amount_cents'])} {row['status']}{tail}"
        )


@ledger_app.command("export-xlsx")
def ledger_export_xlsx(
    path: Annotated[Path, typer.Argument(help="where to write the workbook, e.g. acme.xlsx")],
    executor_dir: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """The whole ledger as an Excel workbook, one sheet per table, for teams that work in Excel."""
    from sindarin.sheets import write_xlsx

    ledger, _, _ = _ledger(executor_dir)
    periods = ledger.periods()
    tabs = {
        "accounts": [{"code": k, **v} for k, v in ledger.accounts().items()],
        "sales": [r for p in periods for r in ledger.sales(p)],
        "payables": [r for p in periods for r in ledger.payables(period=p)],
        "targets": [t for p in periods if (t := ledger.targets(p))],
        "budget": [r for p in periods for r in ledger.budget(p)],
        "planned": [r for p in periods for r in ledger.planned(p)],
        "price_book": [{"product": k, "list_cents": v} for k, v in ledger.price_book().items()],
        "deals": ledger.deals(),
    }
    out = write_xlsx(path, {k: v for k, v in tabs.items() if v})
    print(f"wrote {out} · " + ", ".join(f"{k} {len(v)}" for k, v in tabs.items() if v))


@ledger_app.command("add-sale")
def ledger_add_sale(
    invoice: Annotated[str, typer.Argument(help="e.g. S-2026-1055")],
    amount: Annotated[str, typer.Option(help="e.g. 3980.00")],
    customer: Annotated[str, typer.Option()] = "Contoso Ltd",
    date: Annotated[
        str, typer.Option(help="YYYY-MM-DD; default the last day of the newest period")
    ] = "",
    executor_dir: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """A late sale lands in the books: the demo's 'changed after the proposal' moment."""
    from sindarin.ledger import LedgerError
    from sindarin.schema import money

    ledger, _, _ = _ledger(executor_dir)
    if not date:
        periods = [p for p in ledger.periods() if ledger.sales(p)]
        if not periods:
            _fail("the ledger has no sales to add to; run `sindarin ledger import`")
        date = f"{periods[-1]}-28"
    cents = round(float(amount) * 100)
    try:
        row = ledger.add_sale(invoice, date, customer, cents)
    except LedgerError as exc:
        _fail(str(exc))
        return
    print(
        f"{row['invoice']} {row['customer']} {money(row['amount_cents'])} landed on {row['date']}"
    )


@ledger_app.command("amend")
def ledger_amend(
    invoice: Annotated[str, typer.Argument(help="e.g. INV-1042")],
    amount: Annotated[str, typer.Option(help="new amount, e.g. 4900.00")],
    executor_dir: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Edit an open payable: the other way the books move under a proposal."""
    from sindarin.ledger import LedgerError
    from sindarin.schema import money

    ledger, _, _ = _ledger(executor_dir)
    cents = round(float(amount) * 100)
    try:
        row = ledger.amend_payable(invoice, cents)
    except LedgerError as exc:
        _fail(str(exc))
        return
    print(f"{row['invoice']} {row['vendor']} is now {money(row['amount_cents'])}")


@ledger_app.command("price")
def ledger_price(
    product: Annotated[str, typer.Argument(help="e.g. Team")],
    amount: Annotated[str, typer.Option(help="new list price, e.g. 12500.00")],
    executor_dir: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Edit a list price in the price book: the books move under a pending deal."""
    from sindarin.ledger import LedgerError
    from sindarin.schema import money

    ledger, _, _ = _ledger(executor_dir)
    cents = round(float(amount) * 100)
    try:
        row = ledger.set_list_price(product, cents)
    except LedgerError as exc:
        _fail(str(exc))
        return
    print(f"{row['product']} list price is now {money(row['list_cents'])}")


@ledger_app.command("reset")
def ledger_reset(executor_dir: Annotated[Path | None, typer.Option()] = None) -> None:
    """Back to the imported data: every period open, no budget recorded."""
    ledger, data_dir, _ = _ledger(executor_dir)
    ledger.clear()
    for folder in sorted(p for p in data_dir.iterdir() if p.is_dir()):
        if (folder / "sales.csv").exists():
            print(f"reloaded {folder.name}: {ledger.import_dir(folder)}")


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
    addressing: Annotated[
        str, typer.Option(help="rooms | open: Switch addressing policy for the six agents")
    ] = "rooms",
    people: Annotated[
        str,
        typer.Option(
            help="me | cast | both: you alone drive every gate as the Mattermost user account "
            "Switch's stack hands out (MATTERMOST_USER); cast adds isaac, leonardo, john, vincent "
            "for the permission demo and the scripted rehearsal"
        ),
    ] = "me",
    repo_root: Annotated[Path | None, typer.Option(help="Repository root (default: auto)")] = None,
) -> None:
    """Register the six agents, create the four bridged rooms and the demo people, learn every
    participant id from the event stream, write the policy and executor files, pin an empty
    board. A Mattermost server of your own is used when the root .env sets MATTERMOST_URL.
    Idempotent. Never prints a key or a password."""
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
        addressing=addressing,
        people=people,
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
    executor: Annotated[str, typer.Option(help="switch | ledger | both")] = "both",
    executor_dir: Annotated[
        Path | None, typer.Option(help="Folder holding the state files")
    ] = None,
) -> None:
    """Print the audit log from the executors' state files."""
    from sindarin.executor.config import load_executor
    from sindarin.store import Store

    exec_dir = _exec_dir(executor_dir)
    shorts = ["switch", "ledger"] if executor == "both" else [executor]
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
# sindarin-exec run --executor switch|ledger [--probe]
# ---------------------------------------------------------------------------------------------


@exec_app.command("run")
def exec_run(
    executor: Annotated[str, typer.Option(help="switch | ledger")],
    probe: Annotated[
        bool, typer.Option("--probe", help="Connect, print connection_state, answer pings, exit")
    ] = False,
    probe_seconds: Annotated[int, typer.Option(help="How long the probe listens")] = 60,
    executor_dir: Annotated[Path | None, typer.Option()] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Run one executor (or its connection probe)."""
    from sindarin.executor.service import run_executor, run_probe

    if executor not in ("switch", "ledger"):
        _fail("--executor must be switch or ledger")
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
    executor: Annotated[str, typer.Option(help="switch | ledger")],
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
    role: Annotated[str, typer.Argument(help="analyst | planner | bookkeeper")],
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
    role: Annotated[str, typer.Argument(help="analyst | planner | bookkeeper")],
    text: Annotated[str, typer.Option(help="What the person wrote")],
    sender: Annotated[
        str, typer.Option(help="Sender participant id")
    ] = "@switch-user-demo:localhost",
    name: Annotated[str, typer.Option()] = "Isaac",
    room: Annotated[str, typer.Option(help="finance | planning | period")] = "finance",
    repo_root: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """One model turn without Switch: prints what the agent would post and where."""
    from sindarin.agents.base import ScriptedModel, Turn, anthropic_model_call, is_fake_model
    from sindarin.agents.roles import RoleContext, build_agent
    from sindarin.agents.service import ROLES, ledger_for
    from sindarin.executor.config import process_env
    from sindarin.policy import load_policy
    from sindarin.switch.events import MessageEvent

    if role not in ROLES:
        _fail(f"unknown role {role!r}; one of {', '.join(AGENT_ROLES)}")
    root = _root(repo_root)
    values = process_env(root, role)
    if values.get("ANTHROPIC_API_KEY"):
        os.environ.setdefault("ANTHROPIC_API_KEY", values["ANTHROPIC_API_KEY"])
    policy_path = root / "agents" / "policy" / "policy.yaml"
    policy = load_policy(policy_path)
    room_id = policy.room_id(room) if room != "period" else "!room-period:dry-run"
    ctx = RoleContext(
        role=role,
        repo_root=root,
        policy_path=policy_path,
        users_path=root / "agents" / "policy" / "users.yaml",
        self_id=policy.proposer_id(role) or f"@switch-agent-{role}:dry-run",
        ledger=ledger_for(root),
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


# sindarin people list | add | remove (design v5 §6): approvers without editing YAML by hand


def _policy_files(repo_root: Path | None) -> tuple[Path, Path]:
    root = _root(repo_root)
    return root / "agents" / "policy" / "policy.yaml", root / "agents" / "policy" / "users.yaml"


@people_app.command("list")
def people_list(repo_root: Annotated[Path | None, typer.Option()] = None) -> None:
    """Everyone the policy knows, with the groups each one may approve for."""
    from sindarin.people import members

    policy_path, users_path = _policy_files(repo_root)
    rows = members(policy_path, users_path)
    if not rows:
        print("nobody yet; run `sindarin bootstrap` or `sindarin people add`")
        return
    width = max(len(m.username) for m in rows)
    for m in rows:
        groups = ", ".join(m.groups) or "no group"
        print(f"{m.username:<{width}}  {m.display:<14}  {groups}")


@people_app.command("add")
def people_add(
    username: Annotated[str, typer.Argument(help="the Mattermost username, e.g. alice")],
    groups: Annotated[
        str, typer.Option(help="comma-separated: cfo, controllers, board (empty = rooms only)")
    ] = "",
    display: Annotated[str, typer.Option(help="how the rooms show the name")] = "",
    password: Annotated[
        str, typer.Option(help="password if the account has to be created")
    ] = "Sindarin-demo-1",
    stack_env: Annotated[Path | None, typer.Option(help="The Docker stack's .env")] = None,
    repo_root: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Put a colleague into every Sindarin room and into the approver groups named. The account
    is found on Mattermost, or created with the given password; its participant id is written
    into the policy, which the executors reload on their own. Never prints a password."""
    from sindarin.bootstrap import ROOMS, MattermostTarget, _email, ensure_bridge
    from sindarin.executor.config import parse_env_text
    from sindarin.people import (
        PeopleError,
        add_to_rooms,
        check_groups,
        participant_id,
        write_member,
    )
    from sindarin.policy import load_policy
    from sindarin.switch.gateway import GatewayClient, GatewayError
    from sindarin.switch.mattermost import MattermostClient, MattermostError

    root = _root(repo_root)
    policy_path, users_path = _policy_files(repo_root)
    stack_env = stack_env or Path.home() / ".sindarin" / "switch-stack" / ".env"
    if not stack_env.exists():
        _fail(f"stack env not found: {stack_env} (run scripts/switch_stack.sh up)")
    try:
        wanted = check_groups([g.strip() for g in groups.split(",") if g.strip()])
    except PeopleError as exc:
        _fail(str(exc))
        return
    stack = parse_env_text(stack_env.read_text())
    api = f"http://localhost:{stack.get('API_HOST_PORT') or '8000'}"
    target = MattermostTarget.resolve(root, stack)
    shown = display or username.capitalize()
    try:
        admin = GatewayClient.login(
            api, stack["GATEWAY_ADMIN_EMAIL"], stack["GATEWAY_ADMIN_PASSWORD"]
        )
        bridge_id, _ = ensure_bridge(admin, target)
        mm = MattermostClient.login(target.url, target.admin_user, target.admin_password)
        team_id = mm.team_by_name(target.team_name)["id"]
        user, created = mm.ensure_user(username, _email(username), password, shown)
        print(f"mattermost user {username}: {'created' if created else 'exists'}")
        policy = load_policy(policy_path)
        listing = admin.list_rooms()
        extra = [
            str(r["id"])
            for r in listing
            if str(r.get("name", "")).startswith(("plan-", "close-")) and r.get("id")
        ]
        room_ids = [rid for rid in policy.rooms.values() if rid] + extra
        channels = list(ROOMS) + [
            str(r["name"])
            for r in listing
            if str(r.get("name", "")).startswith(("plan-", "close-"))
        ]
        joined = add_to_rooms(
            gateway=admin,
            mattermost=mm,
            team_id=team_id,
            username=username,
            room_ids=room_ids,
            channel_names=channels,
        )
        print(f"rooms: {joined['rooms']} Switch rooms, {joined['channels']} channels")
        pid = participant_id(bridge_id, username)
        write_member(policy_path, users_path, pid, shown, wanted)
        print(f"policy: {username} ({shown}) in {', '.join(wanted) or 'no group'} · id {pid}")
        # the Switch executor's own directory, so the entry is there for its next start too
        # (a running executor seats a Mattermost member from the id itself)
        from sindarin.executor.config import load_config, save_config

        switch_yaml = root / "executor" / "switch.yaml"
        if switch_yaml.exists():
            cfg = load_config(switch_yaml)
            if cfg.directory.get(pid) != username:
                cfg.directory[pid] = username
                save_config(switch_yaml, cfg)
                print("executor/switch.yaml: directory updated")
        admin.close()
        mm.close()
    except (GatewayError, MattermostError, PeopleError, KeyError) as exc:
        _fail(f"{exc.__class__.__name__}: {exc}")


@people_app.command("remove")
def people_remove(
    username: Annotated[str, typer.Argument(help="the Mattermost username")],
    repo_root: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Take a person out of every approver group. The account and its room memberships stay."""
    from sindarin.people import PeopleError, members, write_member

    policy_path, users_path = _policy_files(repo_root)
    hit = next((m for m in members(policy_path, users_path) if m.username == username), None)
    if hit is None:
        _fail(f"{username} is not in users.yaml")
        return
    try:
        write_member(policy_path, users_path, hit.participant_id, hit.display, [])
    except PeopleError as exc:
        _fail(str(exc))
        return
    print(f"policy: {username} is in no group now")
