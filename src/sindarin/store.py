"""SQLite store, one file per executor (DDD §11): artifacts, approvals, reviews, actions, audit,
messages, seen_message_ids, cursor. The state machine of §7.3 is enforced here, per kind."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sindarin.schema import (
    PREFIX,
    TERMINAL_STATES,
    Artifact,
    AuditEntry,
    Kind,
    State,
    utcnow,
)

_WAITING = {
    State.PENDING: frozenset(
        {State.APPROVED, State.DENIED, State.EXPIRED, State.CHANGES_REQUESTED}
    ),
    State.CHANGES_REQUESTED: frozenset({State.PENDING, State.DENIED, State.EXPIRED}),
    State.DENIED: frozenset(),
    State.EXPIRED: frozenset(),
}
TRANSITIONS: dict[Kind, dict[State, frozenset[State]]] = {
    Kind.GOALS: {
        **_WAITING,
        State.APPROVED: frozenset({State.OPENING}),
        State.OPENING: frozenset({State.OPEN, State.OPEN_FAILED}),
        State.OPEN_FAILED: frozenset({State.OPENING}),
        State.OPEN: frozenset({State.DONE}),
        State.DONE: frozenset(),
    },
    Kind.BUDGET: {
        **_WAITING,
        State.APPROVED: frozenset({State.EXECUTING}),
        State.EXECUTING: frozenset({State.RECORDED, State.RECORD_FAILED}),
        State.RECORD_FAILED: frozenset({State.EXECUTING, State.FAILED}),
        State.RECORDED: frozenset(),
        State.FAILED: frozenset(),
    },
    Kind.ENTRIES: {
        **_WAITING,
        State.APPROVED: frozenset({State.EXECUTING}),
        State.EXECUTING: frozenset({State.PLANNED, State.PLAN_FAILED}),
        State.PLAN_FAILED: frozenset({State.EXECUTING, State.FAILED}),
        State.PLANNED: frozenset(),
        State.FAILED: frozenset(),
    },
    Kind.CLOSE: {
        **_WAITING,
        State.APPROVED: frozenset({State.OPENING}),
        State.OPENING: frozenset({State.OPEN, State.OPEN_FAILED}),
        State.OPEN_FAILED: frozenset({State.OPENING}),
        State.OPEN: frozenset({State.DONE}),
        State.DONE: frozenset(),
    },
    Kind.PAY: {
        **_WAITING,
        State.APPROVED: frozenset({State.EXECUTING}),
        State.EXECUTING: frozenset({State.PAID, State.PAY_FAILED}),
        State.PAY_FAILED: frozenset({State.EXECUTING, State.FAILED}),
        State.PAID: frozenset(),
        State.FAILED: frozenset(),
    },
    Kind.LOCK: {
        **_WAITING,
        State.APPROVED: frozenset({State.EXECUTING}),
        State.EXECUTING: frozenset({State.LOCKED, State.LOCK_FAILED}),
        State.LOCK_FAILED: frozenset({State.EXECUTING, State.FAILED}),
        State.LOCKED: frozenset(),
        State.FAILED: frozenset(),
    },
    Kind.DEAL: {
        **_WAITING,
        State.APPROVED: frozenset({State.EXECUTING}),
        State.EXECUTING: frozenset({State.WON, State.DEAL_FAILED}),
        State.DEAL_FAILED: frozenset({State.EXECUTING, State.FAILED}),
        State.WON: frozenset(),
        State.FAILED: frozenset(),
    },
}
assert all(not TRANSITIONS[k].get(s, ()) for k in Kind for s in TERMINAL_STATES)


class IllegalTransition(Exception):
    pass


class UnknownArtifact(Exception):
    pass


@dataclass(frozen=True)
class MessageRecord:
    """What the executor keeps about every message: identity, never body text."""

    message_id: str
    room_id: str
    sender: str
    sender_name: str
    thread_id: str | None
    timestamp_ms: int
    addressed: bool = False


_COLUMNS = [
    "id",
    "kind",
    "state",
    "rule",
    "room_id",
    "thread_id",
    "proposal_message_id",
    "proposer_id",
    "proposer_name",
    "proposal_json",
    "snapshot_json",
    "snapshot_sha",
    "ask_root_id",
    "created_at",
    "updated_at",
    "expires_at",
    "seq",
    "approver_id",
    "approver_name",
    "approved_at",
    "approval_message_id",
    "note",
    "error",
    "failed_at",
    "attempts",
    "result_json",
]
_DT = {"created_at", "updated_at", "expires_at", "approved_at", "failed_at"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
  n INTEGER PRIMARY KEY AUTOINCREMENT,
  id TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL,
  state TEXT NOT NULL,
  rule TEXT NOT NULL,
  room_id TEXT NOT NULL,
  thread_id TEXT NOT NULL,
  proposal_message_id TEXT NOT NULL,
  proposer_id TEXT NOT NULL,
  proposer_name TEXT NOT NULL,
  proposal_json TEXT NOT NULL,
  snapshot_json TEXT NOT NULL,
  snapshot_sha TEXT NOT NULL,
  ask_root_id TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT,
  expires_at TEXT NOT NULL,
  seq INTEGER NOT NULL DEFAULT 1,
  approver_id TEXT,
  approver_name TEXT,
  approved_at TEXT,
  approval_message_id TEXT,
  note TEXT,
  error TEXT,
  failed_at TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  result_json TEXT
);
CREATE INDEX IF NOT EXISTS artifacts_state ON artifacts(state);
CREATE INDEX IF NOT EXISTS artifacts_thread ON artifacts(thread_id);
CREATE TABLE IF NOT EXISTS approvals (
  n INTEGER PRIMARY KEY AUTOINCREMENT,
  artifact_id TEXT NOT NULL,
  approver_id TEXT NOT NULL,
  room_id TEXT NOT NULL,
  message_id TEXT NOT NULL UNIQUE,
  snapshot_sha TEXT NOT NULL,
  decision TEXT NOT NULL,
  note TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
  n INTEGER PRIMARY KEY AUTOINCREMENT,
  artifact_id TEXT NOT NULL,
  head_sha TEXT NOT NULL,
  reviewer_id TEXT NOT NULL,
  reviewer_name TEXT NOT NULL DEFAULT '',
  report_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actions (
  n INTEGER PRIMARY KEY AUTOINCREMENT,
  artifact_id TEXT NOT NULL,
  action TEXT NOT NULL,
  attempt INTEGER NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  result_json TEXT NOT NULL,
  verified INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS audit (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  artifact_id TEXT,
  event TEXT NOT NULL,
  actor_id TEXT,
  actor_name TEXT,
  room_id TEXT,
  message_id TEXT,
  details_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
  message_id TEXT PRIMARY KEY,
  room_id TEXT NOT NULL,
  sender TEXT NOT NULL,
  sender_name TEXT NOT NULL,
  thread_id TEXT,
  timestamp_ms INTEGER NOT NULL,
  addressed INTEGER NOT NULL DEFAULT 0,
  seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seen_message_ids (
  message_id TEXT PRIMARY KEY,
  seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cursor (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def _dt_out(v: datetime | None) -> str | None:
    if v is None:
        return None
    if v.tzinfo is None:
        v = v.replace(tzinfo=UTC)
    return v.astimezone(UTC).isoformat()


def _dt_in(v: str | None) -> datetime | None:
    if v is None:
        return None
    dt = datetime.fromisoformat(v)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class Store:
    """All persistent state of one executor. One connection, one thread at a time."""

    def __init__(self, path: str | Path = ":memory:", *, readonly: bool = False) -> None:
        self.path = str(path)
        self.readonly = readonly
        if readonly:
            self._db = sqlite3.connect(
                f"file:{self.path}?mode=ro", uri=True, check_same_thread=False, isolation_level=None
            )
        else:
            self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        if not readonly:
            if self.path != ":memory:":
                self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    # -- messages ---------------------------------------------------------------------------

    def record_message(self, m: MessageRecord) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO messages(message_id, room_id, sender, sender_name, thread_id,"
            " timestamp_ms, addressed, seen_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                m.message_id,
                m.room_id,
                m.sender,
                m.sender_name,
                m.thread_id,
                m.timestamp_ms,
                int(m.addressed),
                _dt_out(utcnow()),
            ),
        )

    def get_message(self, message_id: str) -> MessageRecord | None:
        row = self._db.execute(
            "SELECT * FROM messages WHERE message_id=?", (message_id,)
        ).fetchone()
        if row is None:
            return None
        return MessageRecord(
            message_id=row["message_id"],
            room_id=row["room_id"],
            sender=row["sender"],
            sender_name=row["sender_name"],
            thread_id=row["thread_id"],
            timestamp_ms=row["timestamp_ms"],
            addressed=bool(row["addressed"]),
        )

    def mark_seen(self, message_id: str) -> bool:
        cur = self._db.execute(
            "INSERT OR IGNORE INTO seen_message_ids(message_id, seen_at) VALUES (?,?)",
            (message_id, _dt_out(utcnow())),
        )
        return cur.rowcount == 1

    def is_seen(self, message_id: str) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM seen_message_ids WHERE message_id=?", (message_id,)
        ).fetchone()
        return row is not None

    # -- cursor / counters ----------------------------------------------------------------

    def get_value(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM cursor WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_value(self, key: str, value: str) -> None:
        self._db.execute(
            "INSERT INTO cursor(key, value) VALUES (?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def peek_next_id(self, kind: Kind) -> str:
        """The id the next artifact of this kind gets: GOALS-1, BUDGET-1, ENTRIES-1, per kind."""
        n = int(self.get_value(f"next_{kind.value}") or 1)
        return f"{PREFIX[kind]}-{n}"

    def insert_artifact(self, a: Artifact) -> Artifact:
        if a.state is not State.PENDING:
            raise IllegalTransition(f"new artifacts start pending, not {a.state.value}")
        expected = self.peek_next_id(a.kind)
        if a.id != expected:
            raise ValueError(f"artifact id {a.id} must be {expected}")
        if self.get(a.id) is not None:
            raise ValueError(f"artifact {a.id} already exists")
        data = self._row(a)
        cols = ", ".join(_COLUMNS)
        qs = ", ".join("?" for _ in _COLUMNS)
        self._db.execute(
            f"INSERT INTO artifacts({cols}) VALUES ({qs})", [data[c] for c in _COLUMNS]
        )
        n = int(a.id.split("-", 1)[1])
        self.set_value(f"next_{a.kind.value}", str(n + 1))
        return self.get(a.id)  # type: ignore[return-value]

    def get(self, artifact_id: str) -> Artifact | None:
        row = self._db.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
        return self._artifact(row) if row else None

    def by_thread(self, thread_id: str, *states: State) -> list[Artifact]:
        if states:
            qs = ", ".join("?" for _ in states)
            rows = self._db.execute(
                f"SELECT * FROM artifacts WHERE thread_id=? AND state IN ({qs}) ORDER BY n",
                [thread_id, *(s.value for s in states)],
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT * FROM artifacts WHERE thread_id=? ORDER BY n", (thread_id,)
            ).fetchall()
        return [self._artifact(r) for r in rows]

    def in_states(self, *states: State, kind: Kind | None = None) -> list[Artifact]:
        if not states:
            return []
        qs = ", ".join("?" for _ in states)
        sql = f"SELECT * FROM artifacts WHERE state IN ({qs})"
        args: list[Any] = [s.value for s in states]
        if kind is not None:
            sql += " AND kind=?"
            args.append(kind.value)
        rows = self._db.execute(sql + " ORDER BY n", args).fetchall()
        return [self._artifact(r) for r in rows]

    def all(self) -> list[Artifact]:
        rows = self._db.execute("SELECT * FROM artifacts ORDER BY n").fetchall()
        return [self._artifact(r) for r in rows]

    def transition(self, artifact_id: str, new_state: State, **fields: Any) -> Artifact:
        """One legal transition plus field updates, atomically; `seq` increments (§11)."""
        a = self.get(artifact_id)
        if a is None:
            raise UnknownArtifact(artifact_id)
        if new_state not in TRANSITIONS[a.kind].get(a.state, frozenset()):
            raise IllegalTransition(f"{artifact_id}: {a.state.value} → {new_state.value}")
        return self._update(a, {**fields, "state": new_state.value})

    def update_snapshot(
        self,
        artifact_id: str,
        *,
        proposal_json: str,
        snapshot_json: str,
        snapshot_sha: str,
        proposal_message_id: str | None = None,
        expires_at: datetime | None = None,
    ) -> Artifact:
        """A new snapshot for the same id: re-proposal after `changes`, or a head that moved.
        Clears any recorded approver; the state is untouched."""
        a = self.get(artifact_id)
        if a is None:
            raise UnknownArtifact(artifact_id)
        fields: dict[str, Any] = {
            "proposal_json": proposal_json,
            "snapshot_json": snapshot_json,
            "snapshot_sha": snapshot_sha,
            "approver_id": None,
            "approver_name": None,
            "approved_at": None,
            "approval_message_id": None,
        }
        if proposal_message_id is not None:
            fields["proposal_message_id"] = proposal_message_id
        if expires_at is not None:
            fields["expires_at"] = expires_at
        return self._update(a, fields)

    def _update(self, a: Artifact, fields: dict[str, Any]) -> Artifact:
        unknown = set(fields) - set(_COLUMNS) - {"state"}
        if unknown or "id" in fields or "kind" in fields or "seq" in fields:
            raise ValueError(f"unknown or protected artifact fields: {sorted(unknown)}")
        fields = dict(fields)
        fields["updated_at"] = utcnow()
        sets = ", ".join(f"{k}=?" for k in fields) + ", seq=seq+1"
        values = [_dt_out(v) if k in _DT else v for k, v in fields.items()]
        # the WHERE on state makes the write itself the lock: a second writer loses
        cur = self._db.execute(
            f"UPDATE artifacts SET {sets} WHERE id=? AND state=?",
            [*values, a.id, a.state.value],
        )
        if cur.rowcount != 1:
            raise IllegalTransition(f"{a.id}: concurrent state change")
        return self.get(a.id)  # type: ignore[return-value]

    # -- approvals (one row per command; message_id unique = replay guard) -----------------

    def record_approval(
        self,
        artifact_id: str,
        *,
        approver_id: str,
        room_id: str,
        message_id: str,
        snapshot_sha: str,
        decision: str,
        note: str | None = None,
    ) -> bool:
        cur = self._db.execute(
            "INSERT OR IGNORE INTO approvals(artifact_id, approver_id, room_id, message_id,"
            " snapshot_sha, decision, note, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                artifact_id,
                approver_id,
                room_id,
                message_id,
                snapshot_sha,
                decision,
                note,
                _dt_out(utcnow()),
            ),
        )
        return cur.rowcount == 1

    def approvals_for(self, artifact_id: str) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM approvals WHERE artifact_id=? ORDER BY n", (artifact_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # -- actions -----------------------------------------------------------------------------

    def record_action(
        self,
        artifact_id: str,
        action: str,
        attempt: int,
        started_at: datetime,
        result: dict[str, Any],
        verified: bool,
    ) -> None:
        self._db.execute(
            "INSERT INTO actions(artifact_id, action, attempt, started_at, finished_at,"
            " result_json, verified) VALUES (?,?,?,?,?,?,?)",
            (
                artifact_id,
                action,
                attempt,
                _dt_out(started_at),
                _dt_out(utcnow()),
                json.dumps(result, sort_keys=True),
                int(verified),
            ),
        )

    def actions_for(self, artifact_id: str) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM actions WHERE artifact_id=? ORDER BY n", (artifact_id,)
        ).fetchall()
        return [
            {**dict(r), "result": json.loads(r["result_json"]), "verified": bool(r["verified"])}
            for r in rows
        ]

    # -- audit -------------------------------------------------------------------------------

    def append_audit(self, e: AuditEntry) -> AuditEntry:
        cur = self._db.execute(
            "INSERT INTO audit(ts, artifact_id, event, actor_id, actor_name, room_id, message_id,"
            " details_json) VALUES (?,?,?,?,?,?,?,?)",
            (
                _dt_out(e.ts),
                e.artifact_id,
                e.event,
                e.actor_id,
                e.actor_name,
                e.room_id,
                e.message_id,
                json.dumps(e.details, sort_keys=True, ensure_ascii=False),
            ),
        )
        return e.model_copy(update={"id": cur.lastrowid})

    def audit_last(self, n: int = 50) -> list[AuditEntry]:
        rows = self._db.execute("SELECT * FROM audit ORDER BY seq DESC LIMIT ?", (n,)).fetchall()
        return [self._audit(r) for r in reversed(rows)]

    def audit_for(self, artifact_id: str) -> list[AuditEntry]:
        rows = self._db.execute(
            "SELECT * FROM audit WHERE artifact_id=? ORDER BY seq", (artifact_id,)
        ).fetchall()
        return [self._audit(r) for r in rows]

    def audit_all(self) -> list[AuditEntry]:
        rows = self._db.execute("SELECT * FROM audit ORDER BY seq").fetchall()
        return [self._audit(r) for r in rows]

    # -- row mapping -------------------------------------------------------------------------

    @staticmethod
    def _row(a: Artifact) -> dict[str, Any]:
        d = a.model_dump()
        d["state"] = a.state.value
        d["kind"] = a.kind.value
        for k in _DT:
            d[k] = _dt_out(d.get(k))
        return d

    @staticmethod
    def _artifact(row: sqlite3.Row) -> Artifact:
        d = {c: row[c] for c in _COLUMNS}
        for k in _DT:
            d[k] = _dt_in(d[k])
        d["state"] = State(d["state"])
        d["kind"] = Kind(d["kind"])
        return Artifact.model_validate(d)

    @staticmethod
    def _audit(row: sqlite3.Row) -> AuditEntry:
        return AuditEntry(
            id=row["seq"],
            ts=_dt_in(row["ts"]),  # type: ignore[arg-type]
            artifact_id=row["artifact_id"],
            event=row["event"],
            actor_id=row["actor_id"],
            actor_name=row["actor_name"],
            room_id=row["room_id"],
            message_id=row["message_id"],
            details=json.loads(row["details_json"]),
        )
