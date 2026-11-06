"""Command grammar: the only human input executors accept (DDD §9). A regex table, nothing else."""

from __future__ import annotations

import re
from dataclasses import dataclass

from sindarin.names import EXECUTORS

DEFAULT_NAMES: tuple[str, ...] = EXECUTORS


@dataclass(frozen=True)
class Approve:
    artifact_id: str


@dataclass(frozen=True)
class Deny:
    artifact_id: str
    reason: str | None = None


@dataclass(frozen=True)
class Changes:
    artifact_id: str
    note: str


@dataclass(frozen=True)
class Status:
    artifact_id: str | None = None


@dataclass(frozen=True)
class Whoami:
    pass


@dataclass(frozen=True)
class Help:
    pass


@dataclass(frozen=True)
class Board:
    pass


@dataclass(frozen=True)
class Brief:
    """Re-post the plan room's goals (sindarin-switch): for a member the opening brief missed."""


Command = Approve | Deny | Changes | Status | Whoami | Help | Board | Brief

_ID = r"((?:GOALS|BUDGET|ENTRIES)-\d+)"
_TABLE: tuple[tuple[re.Pattern[str], type], ...] = (
    (re.compile(rf"^approve\s+{_ID}$", re.IGNORECASE), Approve),
    (re.compile(rf"^deny\s+{_ID}(?:\s+(.{{1,300}}))?$", re.IGNORECASE | re.DOTALL), Deny),
    (re.compile(rf"^changes\s+{_ID}\s+(.{{1,500}})$", re.IGNORECASE | re.DOTALL), Changes),
    (re.compile(rf"^status(?:\s+{_ID})?$", re.IGNORECASE), Status),
    (re.compile(r"^whoami$", re.IGNORECASE), Whoami),
    (re.compile(r"^help$", re.IGNORECASE), Help),
    (re.compile(r"^board$", re.IGNORECASE), Board),
    (re.compile(r"^brief$", re.IGNORECASE), Brief),
)
TRAILING_PUNCTUATION = " .!,;:"  # people end commands with a period; it changes nothing

HELP_TEXT = (
    "Commands: `approve <id>` · `deny <id> <reason>` · `changes <id> <note>` · "
    "`status [<id>]` · `whoami` · `help` · `board` · `brief` (the plan room's goals). "
    "Ids are GOALS-n, BUDGET-n, ENTRIES-n."
)


def mention_pattern(names: tuple[str, ...] = DEFAULT_NAMES) -> re.Pattern[str]:
    """Leading `@name` tokens, optionally followed by `:` or `,`."""
    alts = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True) if n)
    return re.compile(rf"^(?:\s*@(?:{alts})\b[:,]?\s*)+", re.IGNORECASE)


def strip_mentions(body: str, names: tuple[str, ...] = DEFAULT_NAMES) -> str:
    return mention_pattern(names).sub("", body or "", count=1).strip()


def is_bare_mention(body: str, names: tuple[str, ...] = DEFAULT_NAMES) -> bool:
    """`@sindarin-github` and nothing else: the one non-command that gets an answer (help)."""
    text = (body or "").strip()
    return bool(text) and mention_pattern(names).fullmatch(text) is not None


def parse_command(body: str, names: tuple[str, ...] = DEFAULT_NAMES) -> Command | None:
    """Return the parsed command, or None for anything not in the table (ignored silently)."""
    text = strip_mentions(body, names).rstrip(TRAILING_PUNCTUATION)
    for pattern, cls in _TABLE:
        m = pattern.match(text)
        if not m:
            continue
        if cls in (Whoami, Help, Board, Brief):
            return cls()
        if cls is Status:
            aid = m.group(1)
            return Status(aid.upper() if aid else None)
        aid = m.group(1).upper()
        if cls is Deny:
            reason = (m.group(2) or "").strip() or None
            return Deny(aid, reason)
        if cls is Changes:
            return Changes(aid, m.group(2).strip())
        return cls(aid)
    return None
