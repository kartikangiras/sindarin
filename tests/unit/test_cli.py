"""CLI contract (DDD §6, §16.1): `sindarin propose` prints the exact block or one line + exit 1;
audit and board read the state files; help works for every app."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner
from validation.harness import FIXTURES, ISAAC, REPO

from sindarin.cli import agent_app, app, exec_app

runner = CliRunner()

SPEC = [
    "propose",
    "spec",
    "--title",
    "CSV export of orders",
    "--press-release",
    "Acme can export orders as CSV.",
    "--faq",
    "a",
    "--faq",
    "b",
    "--faq",
    "c",
    "--criteria",
    "1",
    "--criteria",
    "2",
    "--criteria",
    "3",
    "--target-date",
    "2026-11-07",
    "--engineer",
    ISAAC,
    "--repo",
    REPO,
    "--slug",
    "feat-csv-export",
    "--ask",
    "m-ask-1",
    "--justification",
    "Acme asked",
]


@pytest.fixture(autouse=True)
def _policy_env(monkeypatch):
    monkeypatch.setenv("SINDARIN_POLICY_PATH", str(FIXTURES / "policy.yaml"))


def test_propose_spec_prints_exact_block():
    result = runner.invoke(app, SPEC)
    assert result.exit_code == 0, result.output
    lines = result.output.rstrip("\n").split("\n")
    assert (
        lines[0] == "@sindarin-switch proposal"
        and lines[1] == "```sindarin-proposal"
        and lines[3] == "```"
    )
    body = json.loads(lines[2])
    assert (
        body["kind"] == "spec" and body["target"] == "feat-csv-export" and body["ask"] == "m-ask-1"
    )
    assert lines[2] == json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@pytest.mark.parametrize(
    "override,fragment",
    [
        (("--repo", "acme/other"), "not allowed by rule spec"),
        (("--engineer", "@switch-user-vincent:test"), "not in group engineering"),
        (("--target-date", "soon"), "target_date"),
        (("--slug", "Feat Room"), "room slug"),
    ],
)
def test_propose_spec_rejections_are_one_line_exit_1(override, fragment):
    args = list(SPEC)
    args[args.index(override[0]) + 1] = override[1]
    result = runner.invoke(app, args)
    assert result.exit_code == 1
    err = (result.stderr or result.output).strip()
    assert fragment in err and "\n" not in err


def test_propose_pr_and_release():
    result = runner.invoke(
        app,
        [
            "propose",
            "pr",
            "--number",
            "41",
            "--title",
            "t",
            "--branch",
            "feat/SPEC-1",
            "--spec",
            "SPEC-1",
            "--repo",
            REPO,
            "--head-sha",
            "a" * 40,
            "--justification",
            "ok",
        ],
    )
    assert result.exit_code == 0, result.output
    assert result.output.startswith("@sindarin-github proposal\n")
    assert json.loads(result.output.split("\n")[2])["target"] == f"{REPO}#41"
    result = runner.invoke(
        app,
        [
            "propose",
            "release",
            "--tag",
            "v1.4.0",
            "--target-sha",
            "b" * 40,
            "--notes",
            "n",
            "--spec",
            "SPEC-1",
            "--ask",
            "m1",
            "--repo",
            REPO,
            "--justification",
            "go",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output.split("\n")[2])["body"]["tag"] == "v1.4.0"
    result = runner.invoke(
        app,
        [
            "propose",
            "release",
            "--tag",
            "9",
            "--target-sha",
            "b" * 40,
            "--notes",
            "n",
            "--spec",
            "SPEC-1",
            "--repo",
            REPO,
            "--justification",
            "go",
        ],
    )
    assert result.exit_code == 1 and "does not match" in (result.stderr or result.output)


def test_propose_missing_policy(monkeypatch, tmp_path):
    monkeypatch.setenv("SINDARIN_POLICY_PATH", str(tmp_path / "nope.yaml"))
    result = runner.invoke(app, SPEC)
    assert result.exit_code == 1 and "policy file not found" in (result.stderr or result.output)


def test_help_and_version():
    assert runner.invoke(app, ["--help"]).exit_code == 0
    assert runner.invoke(exec_app, ["--help"]).exit_code == 0
    assert runner.invoke(agent_app, ["--help"]).exit_code == 0
    assert runner.invoke(exec_app, ["version"]).output.strip() == "0.1.0"
    assert runner.invoke(exec_app, ["run", "--executor", "nope"]).exit_code == 1
    assert runner.invoke(agent_app, ["run", "nope"]).exit_code == 1


def test_audit_and_board(tmp_path):
    from sindarin.executor.config import ExecutorConfig, save_config
    from sindarin.names import GITHUB_EXECUTOR, SWITCH_EXECUTOR
    from sindarin.schema import AuditEntry
    from sindarin.store import Store

    result = runner.invoke(app, ["audit", "--executor-dir", str(tmp_path)])
    assert result.exit_code == 1
    save_config(
        tmp_path / "github.yaml",
        ExecutorConfig(
            executor=GITHUB_EXECUTOR,
            policy_path=str(FIXTURES / "policy.yaml"),
            users_path=str(FIXTURES / "users.yaml"),
        ),
    )
    save_config(
        tmp_path / "switch.yaml",
        ExecutorConfig(
            executor=SWITCH_EXECUTOR,
            peer_state_db="github.db",
            policy_path=str(FIXTURES / "policy.yaml"),
            users_path=str(FIXTURES / "users.yaml"),
        ),
    )
    s = Store(tmp_path / "github.db")
    s.append_audit(
        AuditEntry(
            ts=datetime(2026, 11, 5, tzinfo=UTC),
            artifact_id="PR-41",
            event="approved",
            actor_name="Isaac",
            details={"snapshot_sha": "abc"},
        )
    )
    s.close()
    Store(tmp_path / "switch.db").close()
    result = runner.invoke(app, ["audit", "--md", "--executor-dir", str(tmp_path)])
    assert (
        result.exit_code == 0
        and "| github | `approved` | PR-41 | Isaac | snapshot_sha=abc |" in result.output
    )
    result = runner.invoke(app, ["audit", "--executor-dir", str(tmp_path), "--artifact", "PR-41"])
    assert "approved · PR-41 · by Isaac" in result.output
    result = runner.invoke(app, ["board", "--executor-dir", str(tmp_path)])
    assert (
        result.exit_code == 0
        and result.output.startswith("**Board**")
        and "nothing in flight" in result.output
    )
