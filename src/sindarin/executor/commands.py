"""Command grammar: the only human input executors accept (DDD §9). A regex table, nothing else."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

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
class Paid:
    """The bank has paid a sent run (sindarin-ledger): a person in the run's approver groups
    types the bank's reference; the payables become paid and the payments are booked."""

    artifact_id: str
    reference: str


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
class Audit:
    """The audit trail (both executors): every event on one artifact, or a month's events. Who
    proposed, who was refused and why, who approved by participant id, what was verified."""

    target: str  # an artifact id (GOALS-1) or a period (2026-10)


@dataclass(frozen=True)
class Approvers:
    """Who approves a kind of thing: the group, the limit and the names, read from the policy."""

    kind: str | None = None  # goals | budget | entries | close | pay | lock | deal, or everything


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


@dataclass(frozen=True)
class Pack:
    """The month's board pack (sindarin-switch): the report, its charts and the exports into a
    Drive folder and a Slides deck, the links posted back."""

    period: str


Command = (
    Approve
    | Deny
    | Changes
    | Status
    | Paid
    | Whoami
    | Help
    | Board
    | Brief
    | Sale
    | Price
    | Read
    | Load
    | Audit
    | Approvers
    | Report
    | Export
    | Chart
    | Pack
)

_ID = r"((?:GOALS|BUDGET|ENTRIES|CLOSE|PAY|LOCK|DEAL)-\d+)"
_TABLE: tuple[tuple[re.Pattern[str], type], ...] = (
    (re.compile(rf"^approve\s+{_ID}$", re.IGNORECASE), Approve),
    (re.compile(rf"^deny\s+{_ID}(?:\s+(.{{1,300}}))?$", re.IGNORECASE | re.DOTALL), Deny),
    (re.compile(rf"^changes\s+{_ID}\s+(.{{1,500}})$", re.IGNORECASE | re.DOTALL), Changes),
    (re.compile(rf"^status(?:\s+{_ID})?$", re.IGNORECASE), Status),
    (re.compile(r"^paid\s+(PAY-\d+)\s+([^\s`]{1,60})$", re.IGNORECASE), Paid),
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
    (re.compile(rf"^audit\s+(?:{_ID}|(\d{{4}}-\d{{2}}))$", re.IGNORECASE), Audit),
    (
        re.compile(
            r"^who approves(?:\s+(?:the\s+)?(goals|budget|entries|close|pay|lock|deal)s?)?$",
            re.IGNORECASE,
        ),
        Approvers,
    ),
    (re.compile(r"^report\s+(\d{4}-\d{2})$", re.IGNORECASE), Report),
    (re.compile(r"^export\s+(\d{4}-\d{2})\s+([a-z]{3,12})$", re.IGNORECASE), Export),
    (re.compile(r"^chart\s+(\d{4}-\d{2})\s+([a-z]{3,12})$", re.IGNORECASE), Chart),
    (re.compile(r"^pack\s+(\d{4}-\d{2})$", re.IGNORECASE), Pack),
)
TRAILING_PUNCTUATION = " .!,;:"  # people end commands with a period; it changes nothing

HELP_TEXT = (
    "Commands: `approve <id>` · `deny <id> <reason>` · `changes <id> <note>` · "
    "`status [<id>]` · `paid <PAY-n> <bank reference>` (the bank has paid a sent run) · "
    "`whoami` · `help` · `board` · `brief` (the plan room's goals) · "
    "`sale <invoice> <amount> <customer>` (a late sale lands in the books) · "
    "`price <product> <amount>` (a list price moves) · "
    "`read <link> [tab]` (any Google Sheet, Drive file, .xlsx or .csv: what it holds, what to "
    "fix; a tab's entries) · "
    "`load <link>` (the team's file into the books) · "
    "`report <period>` · "
    "`export <period> sales|payables|budget|planned|targets|deals|journal|audit` · "
    "`chart <period> revenue|budget|costs|target|deals` · `pack <period>` (the board pack: "
    "report, charts and exports into Drive and a Slides deck) · `audit <id>|<period>` (who did "
    "what, "
    "by participant id) · `who approves [<kind>]`. Months may be named: `chart October revenue`, "
    "`report for November`, `export october sales`. "
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
        if cls is Pack:
            return Pack(m.group(1))
        if cls is Audit:
            return Audit((m.group(1) or m.group(2)).upper())
        if cls is Approvers:
            return Approvers(m.group(1).lower() if m.group(1) else None)
        if cls is Read:
            return Read(m.group(1), (m.group(2) or "").strip())
        if cls is Load:
            return Load(m.group(1))
        if cls in (Export, Chart):
            return cls(m.group(1), m.group(2).lower())
        if cls is Status:
            aid = m.group(1)
            return Status(aid.upper() if aid else None)
        if cls is Paid:
            return Paid(m.group(1).upper(), m.group(2))
        aid = m.group(1).upper()
        if cls is Deny:
            reason = (m.group(2) or "").strip() or None
            return Deny(aid, reason)
        if cls is Changes:
            return Changes(aid, m.group(2).strip())
        return cls(aid)
    return parse_loose(text)


# -- loose phrasing for the reading commands ----------------------------------------------------
# "chart October revenue", "export the november sales", "report for October 2026", "audit october".
# Gates stay exact: nothing here produces an approve, deny or changes.

MONTHS = {
    m: i
    for i, names in enumerate(
        (
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ),
        start=1,
    )
    for m in names
}
FILLER = frozenset(
    (
        "show",
        "me",
        "the",
        "a",
        "an",
        "for",
        "of",
        "on",
        "in",
        "please",
        "give",
        "get",
        "this",
        "that",
        "month",
        "months",
        "report's",
        "as",
    )
)
LOOSE_VERBS = ("report", "chart", "export", "audit", "pack")
CHART_KINDS = ("revenue", "budget", "costs", "target", "deals")
EXPORT_TABLES = ("sales", "payables", "budget", "planned", "targets", "deals", "journal", "audit")
_PERIOD = re.compile(r"^\d{4}-\d{2}$")
_YEAR = re.compile(r"^(?:20)\d{2}$")


def _today() -> datetime:  # patched in tests
    return datetime.now(UTC)


def month_period(name: str, year: str | None = None) -> str | None:
    """`october` → 2026-10 (this year, or last year when the month is still ahead)."""
    month = MONTHS.get(name.lower())
    if month is None:
        return None
    if year:
        return f"{int(year):04d}-{month:02d}"
    today = _today()
    y = today.year if month <= today.month else today.year - 1
    return f"{y:04d}-{month:02d}"


def parse_loose(text: str) -> Command | None:
    words = [w.strip(",.:;!?") for w in text.lower().split()]
    words = [w for w in words if w and w not in FILLER]
    verb = next((w for w in words if w in LOOSE_VERBS), None)
    if verb is None:
        return None
    period: str | None = None
    rest: list[str] = []
    i = 0
    while i < len(words):
        w = words[i]
        if w == verb and period is None and verb not in rest:
            rest.append(verb)
        elif _PERIOD.match(w):
            period = w
        elif w in MONTHS:
            year = words[i + 1] if i + 1 < len(words) and _YEAR.match(words[i + 1]) else None
            period = month_period(w, year)
            if year:
                i += 1
        elif not _YEAR.match(w):
            rest.append(w)
        i += 1
    if period is None:
        return None
    subjects = [w for w in rest if w != verb]
    if verb == "report":
        return Report(period) if not subjects else None
    if verb == "audit":
        return Audit(period) if not subjects else None
    if verb == "pack":
        return Pack(period) if not subjects else None
    kinds = CHART_KINDS if verb == "chart" else EXPORT_TABLES
    subject = next((w for w in subjects if w in kinds), None)
    if subject is None or len(subjects) != 1:
        return None
    return Chart(period, subject) if verb == "chart" else Export(period, subject)
