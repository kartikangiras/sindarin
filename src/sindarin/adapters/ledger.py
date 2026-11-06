"""The ledger adapter (design v5 §5): the one process that writes the ledger.

`read_facts` binds an approval to what the ledger holds now: the basis period's actuals for a
budget (the rows the planner built it on), the recorded budget for its register entries.
`execute` does the one write; `verify` reads it back. Never trusts the fence's numbers.
"""

from __future__ import annotations

from typing import Any

from sindarin.adapters.base import ActionResult, AdapterError, Facts, VerifyResult
from sindarin.ledger import Ledger, LedgerError
from sindarin.schema import Artifact, Kind, Proposal


def budget_facts(ledger: Ledger, period: str, basis: str, total_cents: int) -> dict[str, Any]:
    """What a budget binds to: the basis period's actuals (revenue, rows) and the chart. The
    goals' cap is added by the executor from its own record of the approved goals."""
    s = ledger.summary(basis)
    return {
        "basis": basis,
        "basis_status": "unknown" if not s["sales_rows"] else s["period_status"],
        "revenue_cents": int(s["revenue_cents"]),
        "sales_rows": int(s["sales_rows"]),
        "costs_cents": int(s["costs_cents"]),
        "accounts": sorted(ledger.accounts()),
        "period_status": ledger.period_status(period),
        "total_cents": int(total_cents),
        "digest": ledger.actuals_digest(basis),
    }


def entries_facts(ledger: Ledger, period: str) -> dict[str, Any]:
    rows = ledger.budget(period)
    return {
        "period_status": ledger.period_status(period),
        "budget_id": rows[0]["source"] if rows else "",
        "budget_lines": [
            {
                "department": r["department"],
                "account": r["account"],
                "amount_cents": int(r["amount_cents"]),
            }
            for r in rows
        ],
        "digest": Ledger.budget_digest(rows),
    }


class LedgerAdapter:
    platform = "ledger"
    kinds = frozenset({Kind.BUDGET, Kind.ENTRIES})

    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger

    def read_facts(self, proposal: Proposal) -> Facts:
        body: Any = proposal.body
        try:
            if proposal.kind is Kind.BUDGET:
                from sindarin.schema import previous_period

                return Facts(
                    ok=True,
                    data=budget_facts(
                        self.ledger, body.period, previous_period(body.period), body.total_cents
                    ),
                )
            if proposal.kind is Kind.ENTRIES:
                return Facts(ok=True, data=entries_facts(self.ledger, body.period))
        except (LedgerError, OSError) as exc:
            return Facts(ok=False, error=str(exc)[:160])
        return Facts(ok=False, error=f"{proposal.kind.value} is not a ledger action")

    def execute(self, artifact: Artifact) -> ActionResult:
        p = artifact.proposal()
        body: Any = p.body
        facts = artifact.facts()
        try:
            if artifact.kind is Kind.BUDGET:
                targets = None
                if facts.get("cap_cents") is not None:
                    targets = {
                        "revenue_cents": int(facts.get("revenue_target_cents", 0)),
                        "margin_bps": int(facts.get("margin_target_bps", 0)),
                        "cost_cap_cents": int(facts["cap_cents"]),
                        "source": str(facts.get("goals") or body.goals),
                    }
                digest = self.ledger.record_budget(
                    body.period,
                    artifact.id,
                    [x.model_dump() for x in body.lines],
                    targets=targets,
                )
                return ActionResult(
                    ok=True,
                    data={
                        "period": body.period,
                        "lines": len(body.lines),
                        "total_cents": body.total_cents,
                        "currency": body.currency,
                        "budget_sha": digest,
                    },
                )
            if artifact.kind is Kind.ENTRIES:
                rows = self.ledger.post_budget_entries(
                    body.period, artifact.id, [x.model_dump() for x in body.lines]
                )
                return ActionResult(
                    ok=True,
                    data={
                        "period": body.period,
                        "lines_posted": len(rows),
                        "total_cents": body.total_cents,
                    },
                )
        except LedgerError as exc:
            raise AdapterError(str(exc)[:160]) from None
        raise AdapterError(f"{artifact.kind.value} is not a ledger action")

    def verify(self, artifact: Artifact) -> VerifyResult:
        p = artifact.proposal()
        body: Any = p.body
        try:
            if artifact.kind is Kind.BUDGET:
                rows = self.ledger.budget(body.period)
                ok = len(rows) == len(body.lines) and all(r["source"] == artifact.id for r in rows)
                return VerifyResult(present=ok, data={"lines": len(rows)} if ok else {})
            if artifact.kind is Kind.ENTRIES:
                planned = self.ledger.period_status(body.period) == "planned"
                rows = self.ledger.planned(body.period)
                ok = planned and len(rows) == len(body.lines)
                return VerifyResult(present=ok, data={"lines_posted": len(rows)} if ok else {})
        except (LedgerError, OSError) as exc:
            return VerifyResult(present=None, error=str(exc)[:160])
        return VerifyResult(present=None, error="not a ledger action")
