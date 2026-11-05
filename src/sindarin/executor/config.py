"""Executor configuration (DDD §16.1): `executor/<name>.yaml` for settings, `executor/<name>.env`
for the one credential each executor holds. `<name>` is `switch` or `github`."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from sindarin.names import DISPLAY, GITHUB_EXECUTOR, SWITCH_EXECUTOR

ENV_KEYS = (
    "SWITCH_BRIDGE_URL",
    "SWITCH_AGENT_ID",
    "SWITCH_AGENT_API_KEY",
    "SWITCH_CONNECTION_ID",
    "GITHUB_TOKEN",
    "SWITCH_GATEWAY_URL",
    "SWITCH_GATEWAY_EMAIL",
    "SWITCH_GATEWAY_PASSWORD",
)
SECRET_KEYS = frozenset({"SWITCH_AGENT_API_KEY", "GITHUB_TOKEN", "SWITCH_GATEWAY_PASSWORD"})
EXECUTOR_SHORT = {SWITCH_EXECUTOR: "switch", GITHUB_EXECUTOR: "github"}
EXECUTOR_HANDLE = {v: k for k, v in EXECUTOR_SHORT.items()}


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
    github_token: str = ""
    gateway_url: str = ""
    gateway_email: str = ""
    gateway_password: str = ""

    @classmethod
    def from_values(cls, v: dict[str, str]) -> Env:
        return cls(
            bridge_url=v.get("SWITCH_BRIDGE_URL", "").rstrip("/"),
            agent_id=v.get("SWITCH_AGENT_ID", ""),
            api_key=v.get("SWITCH_AGENT_API_KEY", ""),
            connection_id=v.get("SWITCH_CONNECTION_ID", ""),
            github_token=v.get("GITHUB_TOKEN", ""),
            gateway_url=(v.get("SWITCH_GATEWAY_URL") or v.get("SWITCH_BRIDGE_URL", "")).rstrip("/"),
            gateway_email=v.get("SWITCH_GATEWAY_EMAIL", ""),
            gateway_password=v.get("SWITCH_GATEWAY_PASSWORD", ""),
        )

    def missing(self, *names: str) -> list[str]:
        mapping = {
            "SWITCH_BRIDGE_URL": self.bridge_url,
            "SWITCH_AGENT_ID": self.agent_id,
            "SWITCH_AGENT_API_KEY": self.api_key,
            "SWITCH_CONNECTION_ID": self.connection_id,
            "GITHUB_TOKEN": self.github_token,
            "SWITCH_GATEWAY_URL": self.gateway_url,
            "SWITCH_GATEWAY_EMAIL": self.gateway_email,
            "SWITCH_GATEWAY_PASSWORD": self.gateway_password,
        }
        return [n for n in names if not mapping[n]]


DEFAULT_ROOM_NAMES = {"product": "#product", "releases": "#releases", "customer": "#acme-shared"}


class ExecutorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    executor: str = Field(default=SWITCH_EXECUTOR, pattern="^sindarin-(switch|github)$")
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
    # sindarin-github
    github_mode: str = Field(default="real", pattern="^(real|simulated)$")
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
        "# sindarin-switch (DDD §7.2, §16.1). Owns SPEC-n and the board. Secrets live in "
        "switch.env, never here.\n"
        "# Room ids and participant ids are in agents/policy/policy.yaml; bootstrap writes both.\n"
    ),
    "github": (
        "# sindarin-github (DDD §7.1, §16.1). Owns PR-n and REL-n. Secrets live in github.env, "
        "never here.\n"
        "# github_mode: real (GITHUB_TOKEN in github.env) | simulated (in-memory, no token).\n"
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


def env_path(exec_dir: Path, short: str) -> Path:
    return exec_dir / f"{short}.env"


def load_executor(exec_dir: Path, short: str) -> tuple[Env, ExecutorConfig]:
    if short not in EXECUTOR_HANDLE:
        raise ValueError(f"unknown executor {short!r}; use switch or github")
    cfg = load_config(config_path(exec_dir, short))
    if cfg.executor != EXECUTOR_HANDLE[short]:
        cfg = cfg.model_copy(update={"executor": EXECUTOR_HANDLE[short]})
    env = Env.from_values(load_env(env_path(exec_dir, short)))
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
    markers = ("switch.yaml", "github.yaml", "switch.env", "github.env")
    for candidate in (here, *here.parents):
        if any((candidate / "executor" / m).exists() for m in markers):
            return candidate / "executor"
        if candidate.name == "executor" and any((candidate / m).exists() for m in markers):
            return candidate
    pkg_root = Path(__file__).resolve().parents[3]
    if (pkg_root / "executor").is_dir():
        return pkg_root / "executor"
    return here / "executor"
