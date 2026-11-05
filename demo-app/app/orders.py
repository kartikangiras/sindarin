"""Orders and their CSV export (SPEC-1). The export is a stub: the feature is what the demo builds."""

from __future__ import annotations

from dataclasses import dataclass

MAX_ROWS = 10_000
COLUMNS = ("id", "customer", "total_cents", "placed_at")


@dataclass(frozen=True)
class Order:
    id: int
    customer: str
    total_cents: int
    placed_at: str  # ISO date


class TooManyRows(Exception):
    """The export is above MAX_ROWS; the API answers 413."""


def sample_orders(n: int = 3) -> list[Order]:
    return [Order(i, f"acme-{i}", 1000 * i, f"2026-11-0{(i % 9) + 1}") for i in range(1, n + 1)]


def export_csv(orders: list[Order], limit: int = MAX_ROWS) -> str:
    """Return the orders as CSV text with a header row. Raise TooManyRows above `limit`.

    Stub: returns nothing useful yet. Criteria 2 and 3 are what SPEC-1 asks for.
    """
    return "\n".join(f"{o.id},{o.customer},{o.total_cents},{o.placed_at}" for o in orders)
