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


@dataclass(frozen=True)
class Sale:
    """A late sale lands in the books (sindarin-ledger): the demo's "the books moved" moment,
    typed in chat instead of a terminal. A write by a person, recorded under their id."""

    invoice: str
    amount_cents: int
    customer: str


@dataclass(frozen=True)
class Price:
    """A list price edited in the price book (sindarin-ledger): the books move under a pending
    deal. A write by a person, recorded under their id."""

    product: str
    amount_cents: int


@dataclass(frozen=True)
class Read:
    """Any file a person links (sindarin-switch): a Google Sheet, a file on Drive, an .xlsx or
    .csv link. What it holds, tab by tab, and what is wrong with it; with a tab name, that
    tab's entries. Read-only."""

    url: str
    tab: str = ""


@dataclass(frozen=True)
class Load:
    """The team's file into the books (sindarin-ledger): its tabs replace the open rows they
    describe. A write by a person, recorded under their id."""

    url: str


@dataclass(frozen=True)
class Report:
    """The period as a person reads it (sindarin-switch, from the ledger, read-only)."""

    period: str


@dataclass(frozen=True)
class Export:
    """One table of the period as a CSV file in the room."""

    period: str
    what: str


@dataclass(frozen=True)
class Chart:
    """A bar chart of the period: revenue, budget, costs or target."""

    period: str
    what: str


Command = (
    Approve
    | Deny
    | Changes
    | Status
    | Whoami
    | Help
    | Board
    | Brief
    | Sale
    | Price
    | Read
    | Load
    | Report
    | Export
    | Chart
)

_ID = r"((?:GOALS|BUDGET|ENTRIES|CLOSE|PAY|LOCK|DEAL)-\d+)"
_TABLE: tuple[tuple[re.Pattern[str], type], ...] = (
    (re.compile(rf"^approve\s+{_ID}$", re.IGNORECASE), Approve),
    (re.compile(rf"^deny\s+{_ID}(?:\s+(.{{1,300}}))?$", re.IGNORECASE | re.DOTALL), Deny),
    (re.compile(rf"^changes\s+{_ID}\s+(.{{1,500}})$", re.IGNORECASE | re.DOTALL), Changes),
    (re.compile(rf"^status(?:\s+{_ID})?$", re.IGNORECASE), Status),
    (re.compile(r"^whoami$", re.IGNORECASE), Whoami),
    (re.compile(r"^help$", re.IGNORECASE), Help),
    (re.compile(r"^board$", re.IGNORECASE), Board),
    (re.compile(r"^brief$", re.IGNORECASE), Brief),
    (
        re.compile(
            r"^sale\s+([A-Za-z][A-Za-z0-9-]{2,30})\s+(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{2}))?\s+(.{1,80})$",
            re.IGNORECASE,
        ),
        Sale,
    ),
    (
        re.compile(
            r"^price\s+([A-Za-z][A-Za-z0-9-]{1,39})\s+(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{2}))?$",
            re.IGNORECASE,
        ),
        Price,
    ),
    (
        re.compile(
            r"^(?:read|inspect)\s+<?(https?://[^\s<>]+)>?(?:\s+([A-Za-z][\w -]{0,39}))?$",
            re.IGNORECASE,
        ),
        Read,
    ),
    (re.compile(r"^load\s+<?(https?://[^\s<>]+)>?$", re.IGNORECASE), Load),
    (re.compile(r"^report\s+(\d{4}-\d{2})$", re.IGNORECASE), Report),
    (re.compile(r"^export\s+(\d{4}-\d{2})\s+([a-z]{3,12})$", re.IGNORECASE), Export),
    (re.compile(r"^chart\s+(\d{4}-\d{2})\s+([a-z]{3,12})$", re.IGNORECASE), Chart),
)
TRAILING_PUNCTUATION = " .!,;:"  # people end commands with a period; it changes nothing

HELP_TEXT = (
    "Commands: `approve <id>` · `deny <id> <reason>` · `changes <id> <note>` · "
    "`status [<id>]` · `whoami` · `help` · `board` · `brief` (the plan room's goals) · "
    "`sale <invoice> <amount> <customer>` (a late sale lands in the books) · "
    "`price <product> <amount>` (a list price moves) · "
    "`read <link> [tab]` (any Google Sheet, Drive file, .xlsx or .csv: what it holds, what to "
    "fix; a tab's entries) · "
    "`load <link>` (the team's file into the books) · "
    "`report <period>` · `export <period> sales|payables|budget|planned|targets|deals|journal` · "
    "`chart <period> revenue|budget|costs|target|deals`. "
    "Ids are GOALS-n, BUDGET-n, ENTRIES-n (the plan), CLOSE-n, PAY-n, LOCK-n (the close) and "
    "DEAL-n (sales)."
)


def mention_pattern(names: tuple[str, ...] = DEFAULT_NAMES) -> re.Pattern[str]:
    """Leading `@name` tokens, optionally followed by `:` or `,`."""
    alts = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True) if n)
    return re.compile(rf"^(?:\s*@(?:{alts})\b[:,]?\s*)+", re.IGNORECASE)


def strip_mentions(body: str, names: tuple[str, ...] = DEFAULT_NAMES) -> str:
    return mention_pattern(names).sub("", body or "", count=1).strip()


def is_bare_mention(body: str, names: tuple[str, ...] = DEFAULT_NAMES) -> bool:
    """`@sindarin-switch` and nothing else: the one non-command that gets an answer (help)."""
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
        if cls is Sale:
            cents = int(m.group(2).replace(",", "")) * 100 + int(m.group(3) or 0)
            return Sale(m.group(1).upper(), cents, m.group(4).strip())
        if cls is Price:
            cents = int(m.group(2).replace(",", "")) * 100 + int(m.group(3) or 0)
            return Price(m.group(1), cents)
        if cls is Report:
            return Report(m.group(1))
        if cls is Read:
            return Read(m.group(1), (m.group(2) or "").strip())
        if cls is Load:
            return Load(m.group(1))
        if cls in (Export, Chart):
            return cls(m.group(1), m.group(2).lower())
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
