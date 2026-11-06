"""CLI contract (design v5 §5): the ledger commands, the audit view, the executor and agent
entry points refuse unknown names in one line."""

from __future__ import annotations

from datetime import UTC, datetime

from typer.testing import CliRunner
from validation.harness import DATA

from sindarin.cli import agent_app, app, exec_app
from sindarin.executor.config import ExecutorConfig, save_config
from sindarin.names import LEDGER_EXECUTOR, SWITCH_EXECUTOR
from sindarin.schema import AuditEntry
from sindarin.store import Store

runner = CliRunner()


def _exec_dir(tmp_path):
    exec_dir = tmp_path / "executor"
    exec_dir.mkdir()
    save_config(
        exec_dir / "ledger.yaml",
        ExecutorConfig(
            executor=LEDGER_EXECUTOR,
            self_id="@switch-agent-l:test",
            peer_state_db="switch.db",
            data_dir=str(DATA),
        ),
    )
    save_config(
        exec_dir / "switch.yaml",
        ExecutorConfig(
            executor=SWITCH_EXECUTOR, self_id="@switch-agent-s:test", peer_state_db="ledger.db"
        ),
    )
    return exec_dir


def test_ledger_import_show_amend_reset(tmp_path):
    exec_dir = _exec_dir(tmp_path)
    r = runner.invoke(app, ["ledger", "import", "--executor-dir", str(exec_dir)])
    assert r.exit_code == 0, r.output
    assert "13 sales rows, 3 payables" in r.output and (exec_dir / "books.db").exists()
    r = runner.invoke(app, ["ledger", "show", "2026-10", "--executor-dir", str(exec_dir)])
    assert r.exit_code == 0 and "INV-1042 Amazon Web Services 4,800.00 USD open" in r.output
    assert "revenue 89,905.00 USD" in r.output and "4000 Revenue" in r.output
    assert "targets (import): revenue 85,000.00 USD" in r.output
    r = runner.invoke(
        app,
        [
            "ledger",
            "add-sale",
            "S-2026-1055",
            "--amount",
            "3980.00",
            "--executor-dir",
            str(exec_dir),
        ],
    )
    assert (
        r.exit_code == 0 and "S-2026-1055 Contoso Ltd 3,980.00 USD landed on 2026-10-28" in r.output
    )
    r = runner.invoke(
        app, ["ledger", "amend", "INV-9", "--amount", "1", "--executor-dir", str(exec_dir)]
    )
    assert r.exit_code == 1 and "unknown payable INV-9" in r.output
    r = runner.invoke(app, ["ledger", "reset", "--executor-dir", str(exec_dir)])
    assert r.exit_code == 0 and "reloaded 2026-10" in r.output
    r = runner.invoke(app, ["ledger", "show", "--executor-dir", str(exec_dir)])
    assert "revenue 89,905.00 USD" in r.output


def test_audit_table_and_filter(tmp_path):
    exec_dir = _exec_dir(tmp_path)
    s = Store(exec_dir / "ledger.db")
    s.append_audit(
        AuditEntry(
            ts=datetime(2026, 11, 5, 12, 0, tzinfo=UTC),
            artifact_id="BUDGET-1",
            event="approved",
            actor_name="John",
            details={"snapshot_sha": "abc"},
        )
    )
    s.close()
    result = runner.invoke(app, ["audit", "--executor-dir", str(exec_dir)])
    assert result.exit_code == 0, result.output
    assert "BUDGET-1" in result.output and "approved" in result.output and "John" in result.output
    result = runner.invoke(
        app, ["audit", "--executor-dir", str(exec_dir), "--artifact", "BUDGET-1"]
    )
    assert result.exit_code == 0 and "BUDGET-1" in result.output


def test_exec_and_agent_refuse_unknown_names(tmp_path):
    r = runner.invoke(exec_app, ["run", "--executor", "github", "--executor-dir", str(tmp_path)])
    assert r.exit_code == 1 and "must be switch or ledger" in r.output
    r = runner.invoke(agent_app, ["run", "coder", "--repo-root", str(tmp_path)])
    assert r.exit_code == 1 and "unknown role" in r.output
    r = runner.invoke(exec_app, ["version"])
    assert r.exit_code == 0 and r.output.strip()
