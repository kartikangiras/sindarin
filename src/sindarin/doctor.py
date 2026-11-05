"""`sindarin doctor` (DDD §16.1): say what is missing and how to fix it, in order. Read-only.
Never prints a key or a password."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import httpx

from sindarin.executor.config import load_env, load_executor, parse_env_text
from sindarin.names import AGENT_ROLES


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    fix: str = ""


def run_doctor(repo: Path, stack_env: Path) -> list[Check]:
    out: list[Check] = []
    has_docker = shutil.which("docker") is not None
    daemon = (
        has_docker
        and subprocess.run(["docker", "info"], capture_output=True, check=False).returncode == 0
    )
    out.append(
        Check(
            "docker", daemon, "daemon running" if daemon else "not running", "start Docker Desktop"
        )
    )
    if stack_env.exists():
        st = parse_env_text(stack_env.read_text())
        api = f"http://localhost:{st.get('API_HOST_PORT') or '8000'}"
        from sindarin.bootstrap import MattermostTarget

        try:
            mm = MattermostTarget.resolve(repo, st).url
        except ValueError:
            mm = f"http://localhost:{st.get('MATTERMOST_HOST_PORT') or '8065'}"
        for name, url, fix in (
            ("switch", f"{api}/health", 'PROFILES="gateway collab" scripts/switch_stack.sh up'),
            (
                "mattermost",
                f"{mm}/api/v4/system/ping",
                "the collab profile: scripts/switch_stack.sh up",
            ),
        ):
            try:
                r = httpx.get(url, timeout=5)
                out.append(Check(name, r.status_code == 200, f"{url} → {r.status_code}", fix))
            except httpx.HTTPError as exc:
                out.append(Check(name, False, f"{url} unreachable ({exc.__class__.__name__})", fix))
    else:
        out.append(
            Check("switch", False, f"no stack env at {stack_env}", "scripts/switch_stack.sh up")
        )
    missing_keys = []
    for role in AGENT_ROLES:
        v = load_env(repo / "agents" / role / ".env", env_override=False)
        if not (v.get("SWITCH_AGENT_ID") and v.get("SWITCH_AGENT_API_KEY")):
            missing_keys.append(role)
    for short in ("switch", "github"):
        v = load_env(repo / "executor" / f"{short}.env", env_override=False)
        if not (v.get("SWITCH_AGENT_ID") and v.get("SWITCH_AGENT_API_KEY")):
            missing_keys.append(f"sindarin-{short}")
    out.append(
        Check(
            "agents registered",
            not missing_keys,
            "six keys present" if not missing_keys else f"no key for {', '.join(missing_keys)}",
            "uv run sindarin bootstrap",
        )
    )
    policy_text = (repo / "agents" / "policy" / "policy.yaml").read_text()
    ids_ok = "REPLACE-ME" not in policy_text and 'product: ""' not in policy_text
    out.append(
        Check(
            "policy ids + rooms",
            ids_ok,
            "filled" if ids_ok else "placeholders or empty rooms",
            "uv run sindarin bootstrap",
        )
    )
    for short in ("switch", "github"):
        env, cfg = load_executor(repo / "executor", short)
        out.append(
            Check(
                f"{short}.yaml self_id",
                bool(cfg.self_id),
                cfg.self_id or "empty",
                "uv run sindarin bootstrap",
            )
        )
        if short == "switch":
            ok = not env.missing("SWITCH_GATEWAY_EMAIL", "SWITCH_GATEWAY_PASSWORD")
            out.append(
                Check(
                    "switch gateway credential",
                    ok,
                    "present" if ok else "missing",
                    "uv run sindarin bootstrap",
                )
            )
        else:
            if cfg.github_mode == "simulated":
                out.append(Check("github", True, "simulated (no token needed)", ""))
            else:
                has = bool(env.github_token)
                out.append(
                    Check(
                        "github token",
                        has,
                        "present" if has else "GITHUB_TOKEN empty",
                        "put a PAT in executor/github.env, or set github_mode: simulated in "
                        "executor/github.yaml",
                    )
                )
    keys = [
        role
        for role in AGENT_ROLES
        if load_env(repo / "agents" / role / ".env", env_override=False).get("ANTHROPIC_API_KEY")
    ]
    out.append(
        Check(
            "model key",
            True,
            f"ANTHROPIC_API_KEY present for {', '.join(keys)}"
            if keys
            else "none: use SINDARIN_MODEL=fake (stand-in)",
            "" if keys else "add ANTHROPIC_API_KEY to agents/<role>/.env for Claude",
        )
    )
    procs = [
        ("sindarin-switch", "sindarin-exec run --executor switch"),
        ("sindarin-github", "sindarin-exec run --executor github"),
    ]
    procs += [(role, f"sindarin-agent run {role}") for role in AGENT_ROLES]
    for name, pat in procs:
        running = (
            subprocess.run(["pgrep", "-f", pat], capture_output=True, check=False).returncode == 0
        )
        out.append(
            Check(
                f"process {name}",
                running,
                "running" if running else "stopped",
                "scripts/dev_stack.sh start [--fake]",
            )
        )
    return out
