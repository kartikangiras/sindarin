"""Two adapters, one platform each (design v5 §5): the ledger (budget, entries), Switch (rooms)."""

from __future__ import annotations

from sindarin.schema import Kind

# platform → kinds. The executor validates against the adapters it actually loaded.
KNOWN_PLATFORMS: dict[str, frozenset[Kind]] = {
    "ledger": frozenset({Kind.BUDGET, Kind.ENTRIES, Kind.PAY, Kind.LOCK, Kind.DEAL}),
    "switch": frozenset({Kind.GOALS, Kind.CLOSE}),
}
