"""Two adapters, one platform each (DDD §4.2, §7): GitHub (merge, release) and Switch (rooms)."""

from __future__ import annotations

from sindarin.schema import Kind

# platform → kinds. The CLI validates against this; the executor validates against the adapters
# it actually loaded.
KNOWN_PLATFORMS: dict[str, frozenset[Kind]] = {
    "github": frozenset({Kind.PR, Kind.RELEASE}),
    "switch": frozenset({Kind.SPEC}),
}
