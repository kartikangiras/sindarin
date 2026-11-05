"""Adapter protocol (DDD §7): read facts, execute one action, read it back. One platform each."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from sindarin.schema import Artifact, Kind, Proposal


class AdapterError(Exception):
    """Platform failure. The message is shown in a room, so it must hold no secret."""


@dataclass(frozen=True)
class Facts:
    """Platform-read facts about a proposal target (P4, A6). `data` is bound into the snapshot."""

    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass(frozen=True)
class ActionResult:
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "data": self.data, "error": self.error}


@dataclass(frozen=True)
class VerifyResult:
    """`present` is None when the platform could not be read."""

    present: bool | None
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


class Adapter(Protocol):
    platform: str
    kinds: frozenset[Kind]

    def read_facts(self, proposal: Proposal) -> Facts:
        """What the executor binds the approval to: head sha and base for a PR, the commit and
        tag state for a release. Never writes."""

    def execute(self, artifact: Artifact) -> ActionResult:
        """The one write: merge, create the release, open the room."""

    def verify(self, artifact: Artifact) -> VerifyResult:
        """Read the platform back: did the action land?"""
