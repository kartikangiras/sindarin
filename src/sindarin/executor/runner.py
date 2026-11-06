"""Applies Effects from core.py against the store, the adapter and the Switch poster (DDD §7).

`Effector` is synchronous and I/O-free except through the objects handed to it, so the validation
harness drives it with the fake adapters and a list-capturing poster. `service.py` wraps it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sindarin.adapters.base import ActionResult, Adapter, AdapterError, Facts, VerifyResult
from sindarin.executor import commands as cmd
from sindarin.executor import core
from sindarin.policy import Policy, load_policy
from sindarin.schema import PROPOSAL_FENCE, Artifact, AuditEntry, has_fence, utcnow
from sindarin.store import MessageRecord, Store
from sindarin.switch.events import MessageEvent
from sindarin.users import Users, load_users

log = logging.getLogger("sindarin.runner")

Poster = Callable[[str, str, str | None], None]


@dataclass
class RunnerConfig:
    executor: str
    self_id: str
    policy_path: Path
    users_path: Path
    peer_ids: frozenset[str] = frozenset()
    room_names: dict[str, str] = field(default_factory=dict)
    executor_names: tuple[str, ...] = cmd.DEFAULT_NAMES
    retry_after: timedelta = timedelta(hours=1)
    max_attempts: int = 2
    post_audit_lines: bool = False
    post_status_fences: bool = False
    peer_state_db: Path | None = None
    audit_events_posted: frozenset[str] = field(
        default_factory=lambda: frozenset({"approved", "applied", "denied", "expired"})
    )


class Effector:
    def __init__(
        self,
        store: Store,
        adapters: Mapping[str, Adapter],
        poster: Poster,
        config: RunnerConfig,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.store = store
        self.adapters = dict(adapters)
        self.poster = poster
        self.config = config
        self.clock = clock
        self._file_cache: dict[Path, tuple[int, Any]] = {}

    # -- context ------------------------------------------------------------------------------

    def _load(self, path: Path, loader: Callable[[Path], Any]) -> Any:
        """Re-read on every check but only parse when the file actually changed."""
        key = path.resolve()
        mtime = key.stat().st_mtime_ns
        cached = self._file_cache.get(key)
        if cached is not None and cached[0] == mtime:
            return cached[1]
        value = loader(key)
        self._file_cache[key] = (mtime, value)
        return value

    def policy(self) -> Policy:
        return self._load(self.config.policy_path, load_policy)

    def users(self) -> Users:
        return self._load(self.config.users_path, load_users)

    def peer_artifacts(self) -> tuple[Artifact, ...]:
        """The other executor's state, read-only: the board and the derived stages for
        sindarin-switch, the plan a payment run or a lock names for sindarin-ledger."""
        path = self.config.peer_state_db
        if not path or not Path(path).exists():
            return ()
        try:
            peer = Store(path, readonly=True)
            try:
                return tuple(peer.all())
            finally:
                peer.close()
        except Exception as exc:  # a locked or missing peer file must not stop the sweep
            log.warning("peer state unreadable: %s", exc.__class__.__name__)
            return ()

    def context(
        self, facts: Mapping[str, Facts] | None = None, *, peers: bool = False
    ) -> core.Context:
        return core.Context(
            executor=self.config.executor,
            self_id=self.config.self_id,
            policy=self.policy(),
            users=self.users(),
            view=self.store,
            now=self.clock(),
            facts=dict(facts or {}),
            peer_ids=self.config.peer_ids,
            room_names=self.config.room_names,
            executor_names=self.config.executor_names,
            retry_after=self.config.retry_after,
            max_attempts=self.config.max_attempts,
            post_status_fences=self.config.post_status_fences,
            peer_artifacts=self.peer_artifacts() if peers else (),
        )

    # -- entry points -------------------------------------------------------------------------

    def handle_message(self, ev: MessageEvent) -> list[core.Effect]:
        if not ev.message_id or self.store.is_seen(ev.message_id):
            log.debug("duplicate or id-less message skipped: %s", ev.message_id)
            return []
        # Identity is recorded before anything that can fail; the id is marked seen only once
        # the pipeline produced its effects, so a redelivery can retry after a transient error.
        self.store.record_message(
            MessageRecord(
                message_id=ev.message_id,
                room_id=ev.room_id,
                sender=ev.sender,
                sender_name=ev.sender_name,
                thread_id=ev.thread_id,
                timestamp_ms=ev.timestamp,
                addressed=ev.addressed,
            )
        )
        facts = self._read_facts(core.lookups(self.context(), ev))
        # commands and proposals both consult the other executor's state (order, stages)
        needs_peers = has_fence(ev.body, PROPOSAL_FENCE) or (
            cmd.parse_command(ev.body, self.config.executor_names) is not None
        )
        effects = core.handle_message(self.context(facts, peers=needs_peers), ev)
        self.store.mark_seen(ev.message_id)
        return self.apply(effects)

    def sweep(self) -> list[core.Effect]:
        return self.apply(core.handle_sweep(self.context(peers=True)))

    def _read_facts(self, lookups: list[core.Lookup]) -> dict[str, Facts]:
        """P4 / A6: the platform is read before the pipeline runs; the fence is never trusted."""
        out: dict[str, Facts] = {}
        for lk in lookups:
            adapter = self.adapters.get(lk.proposal.platform)
            if adapter is None:
                out[lk.proposal.target] = Facts(
                    ok=False, error=f"no adapter for {lk.proposal.platform}"
                )
                continue
            try:
                out[lk.proposal.target] = adapter.read_facts(lk.proposal)
            except AdapterError as exc:
                out[lk.proposal.target] = Facts(ok=False, error=str(exc))
            except Exception as exc:
                log.exception("adapter read_facts crashed")
                out[lk.proposal.target] = Facts(
                    ok=False, error=f"adapter error: {exc.__class__.__name__}"
                )
        return out

    # -- apply ---------------------------------------------------------------------------------

    def apply(self, effects: list[core.Effect]) -> list[core.Effect]:
        """Apply in order. Returns every effect applied, including continuations."""
        applied: list[core.Effect] = []
        for e in effects:
            applied.append(e)
            match e:
                case core.RecordMessage(message=m):
                    self.store.record_message(m)
                case core.CreateArtifact(artifact=a):
                    self.store.insert_artifact(a)
                case core.Transition(artifact_id=aid, state=state, fields=fields):
                    self.store.transition(aid, state, **fields)
                case core.UpdateSnapshot():
                    self.store.update_snapshot(
                        e.artifact_id,
                        proposal_json=e.proposal_json,
                        snapshot_json=e.snapshot_json,
                        snapshot_sha=e.snapshot_sha,
                        proposal_message_id=e.proposal_message_id,
                        expires_at=e.expires_at,
                    )
                case core.RecordApproval():
                    self.store.record_approval(
                        e.artifact_id,
                        approver_id=e.approver_id,
                        room_id=e.room_id,
                        message_id=e.message_id,
                        snapshot_sha=e.snapshot_sha,
                        decision=e.decision,
                        note=e.note,
                    )
                case core.SetValue(key=k, value=v):
                    self.store.set_value(k, v)
                case core.Audit():
                    self._audit(e)
                case core.Post(room_id=room, text=text, thread_id=thread):
                    self._post(room, text, thread)
                case core.Execute(artifact_id=aid):
                    applied.extend(self._execute(aid))
                case core.Verify(artifact_id=aid):
                    applied.extend(self._verify(aid))
        return applied

    def _audit(self, e: core.Audit) -> None:
        entry = self.store.append_audit(
            AuditEntry(
                ts=self.clock(),
                artifact_id=e.artifact_id,
                event=e.event,
                actor_id=e.actor_id,
                actor_name=e.actor_name,
                room_id=e.room_id,
                message_id=e.message_id,
                details=e.details,
            )
        )
        log.info("audit %s", entry.render_line())
        if self.config.post_audit_lines and e.event in self.config.audit_events_posted:
            a = self.store.get(e.artifact_id) if e.artifact_id else None
            if a is not None:
                self._post(a.room_id, f"audit · {entry.render_line()}", a.thread_id)

    def _post(self, room_id: str, text: str, thread_id: str | None) -> None:
        try:
            self.poster(room_id, text, thread_id)
        except Exception as exc:  # a failed post must never stop state handling
            log.error("post to %s failed: %s", room_id, exc.__class__.__name__)

    # -- adapter calls (the only network calls that change anything) ---------------------------

    def _execute(self, aid: str) -> list[core.Effect]:
        a = self.store.get(aid)
        if a is None:
            return []
        adapter = self.adapters.get(a.proposal().platform)
        started = self.clock()
        verified = False
        if adapter is None:
            result = ActionResult(ok=False, error=f"no adapter for {a.proposal().platform}")
        else:
            try:
                result = adapter.execute(a)
                if result.ok:
                    v = adapter.verify(a)
                    verified = v.present is True
                    if verified and v.data:
                        result = ActionResult(ok=True, data={**result.data, **v.data})
                    elif v.present is None:
                        result = ActionResult(
                            ok=False,
                            data=result.data,
                            error=f"applied but read-back failed: {v.error}",
                        )
            except AdapterError as exc:
                result = ActionResult(ok=False, error=str(exc))
            except Exception as exc:  # never let a platform exception kill the executor
                log.exception("adapter execute crashed")
                result = ActionResult(ok=False, error=f"adapter error: {exc.__class__.__name__}")
        self.store.record_action(
            aid, a.proposal().action, a.attempts, started, result.to_dict(), verified
        )
        current = self.store.get(aid) or a
        return self.apply(core.after_execute(self.context(), current, result, verified))

    def _verify(self, aid: str) -> list[core.Effect]:
        a = self.store.get(aid)
        if a is None:
            return []
        adapter = self.adapters.get(a.proposal().platform)
        if adapter is None:
            v = VerifyResult(present=None, error=f"no adapter for {a.proposal().platform}")
        else:
            try:
                v = adapter.verify(a)
            except AdapterError as exc:
                v = VerifyResult(present=None, error=str(exc))
            except Exception as exc:
                log.exception("adapter verify crashed")
                v = VerifyResult(present=None, error=f"adapter error: {exc.__class__.__name__}")
        return self.apply(core.after_verify(self.context(), a, v))
