"""Process configuration (DDD §16.1): `executor/<name>.yaml` for settings and one root `.env` for
every secret and setting of the six processes. A person fills the top of the file; `sindarin
bootstrap` writes each process's Switch identity below it under the process's prefix
(`PRODUCT_SWITCH_AGENT_ID`, `SINDARIN_GITHUB_SWITCH_AGENT_API_KEY`, ...).

Each process is handed only its own block (`process_env`): its identity, the shared bridge URL and
the root keys it is entitled to (`ROOT_ENV_KEYS`). The Gateway credential reaches
`sindarin-switch` alone; `sindarin-ledger` alone opens the ledger for writing. `<name>` is
`switch` or `ledger`.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from sindarin.names import (
    ANALYST,
    BOOKKEEPER,
    DISPLAY,
    LEDGER_EXECUTOR,
    PLANNER,
    SALES,
    SWITCH_EXECUTOR,
    TREASURY,
)

ENV_KEYS = (
    "SWITCH_BRIDGE_URL",
    "SWITCH_AGENT_ID",
    "SWITCH_AGENT_API_KEY",
    "SWITCH_CONNECTION_ID",
    "SWITCH_GATEWAY_URL",
    "SWITCH_GATEWAY_EMAIL",
    "SWITCH_GATEWAY_PASSWORD",
)
SECRET_KEYS = frozenset({"SWITCH_AGENT_API_KEY", "SWITCH_GATEWAY_PASSWORD"})
EXECUTOR_SHORT = {SWITCH_EXECUTOR: "switch", LEDGER_EXECUTOR: "ledger"}
EXECUTOR_HANDLE = {v: k for k, v in EXECUTOR_SHORT.items()}

ROOT_ENV = ".env"  # the one env file; gitignored, `.env.example` is the template
IDENTITY_KEYS = ("SWITCH_AGENT_ID", "SWITCH_AGENT_API_KEY", "SWITCH_CONNECTION_ID")
# What each process may read from the root .env besides its own identity and SWITCH_BRIDGE_URL
# (DDD §4.2, one credential per process).
ROOT_ENV_KEYS: dict[str, tuple[str, ...]] = {
    ANALYST: ("ANTHROPIC_API_KEY",),
    BOOKKEEPER: ("ANTHROPIC_API_KEY",),
    PLANNER: ("ANTHROPIC_API_KEY",),
    TREASURY: ("ANTHROPIC_API_KEY",),
    SALES: ("ANTHROPIC_API_KEY",),
    SWITCH_EXECUTOR: ("SWITCH_GATEWAY_URL", "SWITCH_GATEWAY_EMAIL", "SWITCH_GATEWAY_PASSWORD"),
    # the ledger file is the credential; the Google service-account key (for the write-back
    # into the team's sheet) reaches only this process
    LEDGER_EXECUTOR: ("SINDARIN_LEDGER_GOOGLE_SA",),
}
PROCESSES = tuple(ROOT_ENV_KEYS)
# Read by `sindarin bootstrap` and the live scripts, never by a process: a Mattermost server of
# your own instead of the one bundled with the Switch stack.
MATTERMOST_KEYS = (
    "MATTERMOST_URL",
    "MATTERMOST_INTERNAL_URL",
    "MATTERMOST_ADMIN_USER",
    "MATTERMOST_ADMIN_PASSWORD",
    "MATTERMOST_USER",
    "MATTERMOST_USER_PASSWORD",
    "MATTERMOST_TEAM_NAME",
    "SWITCH_BRIDGE_NAME",
)


def parse_env_text(text: str) -> dict[str, str]:
    """Minimal .env parser: KEY=VALUE lines, # comments, optional quotes. No interpolation."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif value.startswith("#"):
            value = ""
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        out[key] = value
    return out


def load_env(path: Path, *, env_override: bool = True) -> dict[str, str]:
    """Values from the file, overridden by the process environment for the same keys."""
    values = parse_env_text(path.read_text(encoding="utf-8")) if path.exists() else {}
    if env_override:
        for key in ENV_KEYS:
            if os.environ.get(key):
                values[key] = os.environ[key]
    return values


def root_env_path(repo: Path) -> Path:
    return repo / ROOT_ENV


def ensure_root_env(repo: Path) -> Path:
    """The root .env, created from `.env.example` (mode 600) when it does not exist yet."""
    path = root_env_path(repo)
    if not path.exists():
        template = repo / ".env.example"
        text = template.read_text(encoding="utf-8") if template.exists() else ""
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
    return path


def load_root_env(repo: Path, keys: Iterable[str]) -> dict[str, str]:
    """Only `keys` from the root .env, non-empty values; nothing else leaves the file."""
    path = root_env_path(repo)
    if not path.exists():
        return {}
    values = parse_env_text(path.read_text(encoding="utf-8"))
    return {k: values[k] for k in keys if values.get(k)}


def env_prefix(process: str) -> str:
    """`analyst` → `ANALYST_`, `sindarin-ledger` → `SINDARIN_LEDGER_`."""
    if process not in ROOT_ENV_KEYS:
        raise ValueError(f"unknown process {process!r}")
    return process.upper().replace("-", "_") + "_"


def identity_values(process: str, identity: dict[str, str]) -> dict[str, str]:
    """The prefixed lines bootstrap writes for one process's Switch identity."""
    prefix = env_prefix(process)
    return {prefix + k: identity[k] for k in IDENTITY_KEYS if identity.get(k)}


def process_env(repo: Path, process: str, *, env_override: bool = False) -> dict[str, str]:
    """The environment one process is handed from the root .env: SWITCH_BRIDGE_URL, its own
    identity (prefixed in the file, plain here) and the keys it is entitled to. With
    `env_override` the shell wins for `ENV_KEYS`. Other processes' blocks are never read."""
    prefix = env_prefix(process)
    prefixed = {prefix + k: k for k in IDENTITY_KEYS}  # CODER_GITHUB_TOKEN is not one of these
    values = load_root_env(repo, ("SWITCH_BRIDGE_URL", *ROOT_ENV_KEYS[process], *prefixed))
    out = {k: v for k, v in values.items() if k not in prefixed}
    out.update({plain: values[k] for k, plain in prefixed.items() if values.get(k)})
    if env_override:
        for key in ENV_KEYS:
            if os.environ.get(key):
                out[key] = os.environ[key]
    return out


def write_env_values(path: Path, updates: dict[str, str]) -> None:
    """Set or append keys in the .env file, keeping every other line. Mode 600."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        key = (
            stripped.split("=", 1)[0].strip()
            if "=" in stripped and not stripped.startswith("#")
            else None
        )
        if key and key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    for key, value in remaining.items():
        out.append(f"{key}={value}")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # private from creation
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    os.chmod(path, 0o600)


@dataclass(frozen=True)
class Env:
    bridge_url: str
    agent_id: str
    api_key: str
    connection_id: str
    gateway_url: str = ""
    gateway_email: str = ""
    gateway_password: str = ""
    google_sa: str = ""  # path to the service-account key, sindarin-ledger only

    @classmethod
    def from_values(cls, v: dict[str, str]) -> Env:
        return cls(
            bridge_url=v.get("SWITCH_BRIDGE_URL", "").rstrip("/"),
            agent_id=v.get("SWITCH_AGENT_ID", ""),
            api_key=v.get("SWITCH_AGENT_API_KEY", ""),
            connection_id=v.get("SWITCH_CONNECTION_ID", ""),
            gateway_url=(v.get("SWITCH_GATEWAY_URL") or v.get("SWITCH_BRIDGE_URL", "")).rstrip("/"),
            gateway_email=v.get("SWITCH_GATEWAY_EMAIL", ""),
            gateway_password=v.get("SWITCH_GATEWAY_PASSWORD", ""),
            google_sa=v.get("SINDARIN_LEDGER_GOOGLE_SA", ""),
        )

    def missing(self, *names: str) -> list[str]:
        mapping = {
            "SWITCH_BRIDGE_URL": self.bridge_url,
            "SWITCH_AGENT_ID": self.agent_id,
            "SWITCH_AGENT_API_KEY": self.api_key,
            "SWITCH_CONNECTION_ID": self.connection_id,
            "SWITCH_GATEWAY_URL": self.gateway_url,
            "SWITCH_GATEWAY_EMAIL": self.gateway_email,
            "SWITCH_GATEWAY_PASSWORD": self.gateway_password,
        }
        return [n for n in names if not mapping[n]]


DEFAULT_ROOM_NAMES = {
    "finance": "#acme-finance",
    "planning": "#planning",
    "payments": "#payments",
    "sales": "#sales",
}


class ExecutorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    executor: str = Field(default=SWITCH_EXECUTOR, pattern="^sindarin-(switch|ledger)$")
    room_names: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_ROOM_NAMES))
    self_id: str = ""
    peer_ids: list[str] = Field(default_factory=list)
    policy_path: str = "../agents/policy/policy.yaml"
    users_path: str = "../agents/policy/users.yaml"
    state_db: str = ""
    sweep_interval_seconds: int = Field(default=30, ge=5)
    retry_minutes: int = Field(default=60, ge=1)
    max_attempts: int = Field(default=2, ge=1, le=5)
    thread_replies: bool = True
    post_audit_lines: bool = False
    status_fences: bool = False
    # sindarin-ledger
    ledger_db: str = "books.db"  # the ledger file (ledger.db is the executor's own state)
    data_dir: str = "../data/acme"  # the period folders `sindarin ledger import` loads
    sheet_url: str = ""  # the team's Google Sheet the ledger mirrors (import and write-back)
    # sindarin-switch
    room_creation: str = Field(default="gateway", pattern="^(gateway|agent_op)$")
    addressing: str = Field(default="rooms", pattern="^(rooms|open)$")  # layer 0 (§5, §12.4 item 5)
    bridge_id: str = ""
    peer_state_db: str = ""
    directory: dict[str, str] = Field(default_factory=dict)

    @property
    def short(self) -> str:
        return EXECUTOR_SHORT[self.executor]

    @property
    def display_name(self) -> str:
        return DISPLAY[self.executor]

    def db_path(self, base: Path) -> Path:
        return self.resolve(base, self.state_db or f"{self.short}.db")

    def resolve(self, base: Path, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else (base / p).resolve()

    def missing(self) -> list[str]:
        out = []
        if not self.self_id:
            out.append("self_id")
        return out


HEADER = {
    "switch": (
        "# sindarin-switch (design v5 §5). Owns GOALS-n, CLOSE-n and the board. Secrets live in "
        "the root .env, never here.\n"
        "# Room ids and participant ids are in agents/policy/policy.yaml; bootstrap writes both.\n"
    ),
    "ledger": (
        "# sindarin-ledger (design v5 §5). Owns BUDGET-n, ENTRIES-n, PAY-n and LOCK-n; the only "
        "process that writes ledger_db.\n"
        "# Secrets live in the root .env, never here.\n"
    ),
}


def render_config(cfg: ExecutorConfig) -> str:
    data = cfg.model_dump()
    return HEADER[cfg.short] + yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def load_config(path: Path) -> ExecutorConfig:
    if not path.exists():
        return ExecutorConfig()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return ExecutorConfig.model_validate(data)


def save_config(path: Path, cfg: ExecutorConfig) -> None:
    path.write_text(render_config(cfg), encoding="utf-8")


def config_path(exec_dir: Path, short: str) -> Path:
    return exec_dir / f"{short}.yaml"


def load_executor(exec_dir: Path, short: str) -> tuple[Env, ExecutorConfig]:
    if short not in EXECUTOR_HANDLE:
        raise ValueError(f"unknown executor {short!r}; use switch or ledger")
    cfg = load_config(config_path(exec_dir, short))
    if cfg.executor != EXECUTOR_HANDLE[short]:
        cfg = cfg.model_copy(update={"executor": EXECUTOR_HANDLE[short]})
    env = Env.from_values(process_env(exec_dir.parent, EXECUTOR_HANDLE[short], env_override=True))
    return env, cfg


def find_executor_dir(explicit: Path | None = None) -> Path:
    """`--executor-dir`, then $SINDARIN_EXECUTOR_DIR, then ./executor, then walk up, then the
    source checkout's executor/ folder."""
    if explicit is not None:
        return explicit.resolve()
    env = os.environ.get("SINDARIN_EXECUTOR_DIR")
    if env:
        return Path(env).resolve()
    here = Path.cwd().resolve()
    markers = ("switch.yaml", "ledger.yaml")
    for candidate in (here, *here.parents):
        if any((candidate / "executor" / m).exists() for m in markers):
            return candidate / "executor"
        if candidate.name == "executor" and any((candidate / m).exists() for m in markers):
            return candidate
    pkg_root = Path(__file__).resolve().parents[3]
    if (pkg_root / "executor").is_dir():
        return pkg_root / "executor"
    return here / "executor"
