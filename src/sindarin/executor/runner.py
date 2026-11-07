"""Applies Effects from core.py against the store, the adapter and the Switch poster (DDD §7).

`Effector` is synchronous and I/O-free except through the objects handed to it, so the validation
harness drives it with the fake adapters and a list-capturing poster. `service.py` wraps it.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from sindarin import workbook
from sindarin.adapters.base import ActionResult, Adapter, AdapterError, Facts, VerifyResult
from sindarin.executor import commands as cmd
from sindarin.executor import core
from sindarin.policy import Policy, load_policy
from sindarin.schema import PROPOSAL_FENCE, Artifact, AuditEntry, has_fence, money, utcnow
from sindarin.store import MessageRecord, Store
from sindarin.switch.events import MessageEvent
from sindarin.users import Users, load_users

log = logging.getLogger("sindarin.runner")

Poster = Callable[[str, str, str | None], None]
CHART_ORDER = ("revenue", "budget", "target", "costs", "deals")  # a report's charts, in order


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


class FilePoster(Protocol):
    """Posts a file into a room (Switch attachments); returns False when it cannot."""

    def post_file(
        self, room_id: str, text: str, filename: str, data: bytes, mime: str, thread_id: str | None
    ) -> bool: ...

    def post_files(
        self, room_id: str, text: str, files: list[tuple[str, bytes, str]], thread_id: str | None
    ) -> bool: ...


class Effector:
    def __init__(
        self,
        store: Store,
        adapters: Mapping[str, Adapter],
        poster: Poster,
        config: RunnerConfig,
        clock: Callable[[], datetime] = utcnow,
        *,
        ledger: Any | None = None,
        files: FilePoster | None = None,
        pack: Any | None = None,
    ) -> None:
        self.store = store
        self.adapters = dict(adapters)
        self.poster = poster
        self.config = config
        self.clock = clock
        self.ledger = ledger  # a read-only handle for reports (sindarin-switch)
        self.files = files
        self.pack = pack  # a GoogleDocs when the board pack goes to Drive (sindarin-switch)
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
                case core.BookSale():
                    applied.extend(self._book_sale(e))
                case core.SetPrice():
                    applied.extend(self._set_price(e))
                case core.InspectFile():
                    applied.extend(self._inspect_file(e))
                case core.LoadBooks():
                    applied.extend(self._load_books(e))
                case core.RunReport():
                    applied.extend(self._run_report(e))
                case core.RunAudit():
                    applied.extend(self._run_audit(e))
                case core.ConfirmPaid():
                    applied.extend(self._confirm_paid(e))
                case core.PollPayment(artifact_id=aid):
                    applied.extend(self._poll_payment(aid))
                case core.PollInvoice(artifact_id=aid):
                    applied.extend(self._poll_invoice(aid))
                case core.CrmSync():
                    applied.extend(self._crm_sync(e))
                case core.SendContract(artifact_id=aid):
                    applied.extend(self._send_contract(aid))
                case core.PollSignature(artifact_id=aid):
                    applied.extend(self._poll_signature(aid))
                case core.BuildPack():
                    applied.extend(self._build_pack(e))
        return applied

    # -- the audit trail ----------------------------------------------------------------------

    def _audit_rows(self, target: str, with_peer: bool) -> list[dict[str, Any]]:
        """Audit entries for one artifact or one month, from this executor's store and, when
        asked, the peer's; each row carries the executor that wrote it."""
        stores: list[tuple[str, Store, bool]] = [(self.config.executor or "", self.store, False)]
        peer: Store | None = None
        if with_peer and self.config.peer_state_db and self.config.peer_state_db.exists():
            try:
                peer = Store(self.config.peer_state_db, readonly=True)
                stores.append(("peer", peer, True))
            except Exception as exc:
                log.warning("peer state unreadable: %s", exc.__class__.__name__)
        rows: list[dict[str, Any]] = []
        is_period = bool(re.fullmatch(r"\d{4}-\d{2}", target))
        try:
            for owner, store, _ in stores:
                entries = store.audit_for(target) if not is_period else store.audit_all()
                periods: dict[str, str] = {}
                for entry in entries:
                    aid = entry.artifact_id or ""
                    if is_period:
                        if aid and aid not in periods:
                            a = store.get(aid)
                            periods[aid] = a.period if a is not None else ""
                        in_month = (
                            periods.get(aid) == target
                            or str(entry.details.get("period", "")) == target
                            or (not aid and entry.ts.strftime("%Y-%m") == target)
                        )
                        if not in_month:
                            continue
                    rows.append(
                        {
                            "ts": entry.ts.strftime("%Y-%m-%d %H:%M:%S"),
                            "seq": int(entry.id or 0),
                            "executor": owner,
                            "event": entry.event,
                            "artifact": aid,
                            "actor": entry.actor_name or "",
                            "actor_id": entry.actor_id or "",
                            "details": _compact(entry.details),
                        }
                    )
        finally:
            if peer is not None:
                peer.close()
        rows.sort(key=lambda r: (r["ts"], r["seq"]))  # same second: the order they were written
        return rows

    def _run_audit(self, e: core.RunAudit) -> list[core.Effect]:
        rows = self._audit_rows(e.target, e.with_peer)
        if not rows:
            text = f"**audit {e.target}** · nothing recorded."
        else:
            shown = rows[-40:]
            head = f"**audit {e.target}** · {len(rows)} event{'s' if len(rows) != 1 else ''}"
            if len(rows) > len(shown):
                head += f" · the last {len(shown)}; `export {e.target} audit` has them all"
            lines = [head, "```"]
            for r in shown:
                who = f" · {r['actor']}" if r["actor"] else ""
                aid = f" · {r['artifact']}" if r["artifact"] else ""
                det = f" · {r['details']}" if r["details"] else ""
                lines.append(f"{r['ts'][11:16]} {r['ts'][:10]} · {r['event']}{aid}{who}{det}")
            lines.append("```")
            text = "\n".join(lines)
        return self.apply(
            [
                core.Audit(
                    "audit_posted",
                    actor_id=e.actor_id,
                    room_id=e.room_id,
                    details={"target": e.target, "rows": len(rows)},
                ),
                core.Post(e.room_id, text, e.thread_id),
            ]
        )

    def _run_report(self, e: core.RunReport) -> list[core.Effect]:
        """Render from the read-only ledger; attach the file through Switch when a poster is
        wired, else fall back to the text form (a code-block chart, the rows inline)."""
        from sindarin import reports

        if self.ledger is None:
            return self.apply([core.Post(e.room_id, "No ledger here to report from.", e.thread_id)])
        try:
            if e.what == "report":
                out = reports.report(self.ledger, e.period)
            elif e.what == "export" and e.subject == "audit":
                out = self._audit_export(e.period)
            elif e.what == "export":
                out = reports.export(self.ledger, e.period, e.subject)
            elif self.files is not None:
                out = reports.chart_png(self.ledger, e.period, e.subject)
            else:
                out = reports.chart_text(self.ledger, e.period, e.subject)
        except reports.ReportError as exc:
            return self.apply(
                [core.Post(e.room_id, f"**{e.what} {e.period}** — {exc}.", e.thread_id)]
            )
        except Exception as exc:  # a drawing library failure must not take the executor down
            log.warning("%s failed: %s", e.what, exc.__class__.__name__)
            return self.apply(
                [core.Post(e.room_id, f"**{e.what} {e.period}** — could not render.", e.thread_id)]
            )
        posted_file = False
        charts: list[reports.Output] = []
        extra: list[core.Effect] = []
        if e.what == "report" and self.files is not None:
            # the text first, then each chart as its own message so it renders inline and
            # large, the way a person would paste it, rather than as a strip of thumbnails
            extra.append(core.Post(e.room_id, out.text, e.thread_id))
            for kind in CHART_ORDER:
                try:
                    charts.append(reports.chart_png(self.ledger, e.period, kind))
                except reports.ReportError:
                    continue
                except Exception as exc:
                    log.warning("chart %s failed: %s", kind, exc.__class__.__name__)
            for png in charts:
                try:
                    posted_file = (
                        self.files.post_file(
                            e.room_id, png.text, png.filename, png.data, png.mime, e.thread_id
                        )
                        or posted_file
                    )
                except Exception as exc:
                    log.warning("report chart not attached: %s", str(exc)[:120])
            posted_file = True  # the text went out as a post; the charts are best effort
        elif out.data and self.files is not None:
            try:
                posted_file = self.files.post_file(
                    e.room_id, out.text, out.filename, out.data, out.mime, e.thread_id
                )
            except Exception as exc:
                log.warning("file post failed: %s", str(exc)[:120])
            if posted_file and e.what == "chart" and out.facts.get("reading"):
                extra.append(core.Post(e.room_id, str(out.facts["reading"]), e.thread_id))
        effects: list[core.Effect] = [
            core.Audit(
                f"{e.what}_posted",
                actor_id=e.actor_id,
                actor_name=e.actor_name,
                room_id=e.room_id,
                details={
                    **{k: v for k, v in out.facts.items() if k != "reading"},
                    "rows": out.rows,
                    "file": posted_file,
                    "charts": len(charts),
                },
            ),
            *extra,
        ]
        if not posted_file:
            text = out.text
            if out.data and e.what == "export":
                body = out.data.decode("utf-8", errors="replace")
                text = f"{out.text}\n```csv\n{body[:3000]}\n```"
            elif out.data and e.what == "chart":
                text = reports.chart_text(self.ledger, e.period, e.subject).text
            effects.append(core.Post(e.room_id, text, e.thread_id))
        return self.apply(effects)

    def _audit_export(self, period: str):
        """The month's audit trail across both executors as a CSV file."""
        import csv
        import io

        from sindarin import reports

        rows = self._audit_rows(period, True)
        if not rows:
            raise reports.ReportError(f"nothing recorded for {period}")
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
        return reports.Output(
            text=f"**audit {period}** · {len(rows)} event{'s' if len(rows) != 1 else ''}",
            filename=f"audit-{period}.csv",
            data=buf.getvalue().encode("utf-8"),
            mime="text/csv",
            rows=len(rows),
            facts={"period": period, "what": "audit"},
        )

    def _inspect_file(self, e: core.InspectFile) -> list[core.Effect]:
        """Fetch the linked file, describe it and check it against the books' layout."""
        who = {"actor_id": e.actor_id, "actor_name": e.actor_name, "room_id": e.room_id}
        try:
            book = workbook.fetch(e.url)
            if e.tab:
                text, data, filename = workbook.rows_table(book, e.tab)
                details = {"url": e.url[:200], "name": book.name, "tab": e.tab, "bytes": len(data)}
                out: list[core.Effect] = [core.Audit("file_read", details=details, **who)]
                attached = False
                if data and self.files is not None:
                    try:
                        attached = self.files.post_file(
                            e.room_id, text, filename, data, "text/csv", e.thread_id
                        )
                    except Exception as exc:
                        log.warning("tab not attached: %s", str(exc)[:120])
                if not attached:
                    out.append(core.Post(e.room_id, text, e.thread_id))
                return self.apply(out)
            ins = workbook.inspect(book)
            details = {
                "url": e.url[:200],
                "name": book.name,
                "tabs": len(ins.tabs),
                "problems": len(ins.problems),
                "loadable": ins.loadable,
            }
            out = [
                core.Audit("file_read", details=details, **who),
                core.Post(e.room_id, workbook.render(ins), e.thread_id),
            ]
        except workbook.FileError as exc:
            out = [
                core.Audit(
                    "file_refused", details={"url": e.url[:200], "error": str(exc)[:160]}, **who
                ),
                core.Post(e.room_id, f"**read** — {exc}.", e.thread_id),
            ]
        return self.apply(out)

    def _load_books(self, e: core.LoadBooks) -> list[core.Effect]:
        """Fetch the linked file; if the books can take it, the ledger adapter loads it."""
        from sindarin.names import SWITCH_EXECUTOR

        who = {"actor_id": e.actor_id, "actor_name": e.actor_name, "room_id": e.room_id}
        adapter = self.adapters.get("ledger")
        if adapter is None or not hasattr(adapter, "load_books"):
            return self.apply([core.Post(e.room_id, "**load** — no ledger here.", e.thread_id)])
        try:
            book = workbook.fetch(e.url)
            ins = workbook.inspect(book)
            if not ins.loadable:
                why = "; ".join([f"no `{m}` tab" for m in ins.missing] + ins.problems[:5])
                text = (
                    f"**load** refused — {book.name} is not loadable as the books: {why}. "
                    f"`read <link>` to {SWITCH_EXECUTOR} shows the whole check."
                )
                out: list[core.Effect] = [
                    core.Audit(
                        "load_refused", details={"url": e.url[:200], "why": why[:300]}, **who
                    ),
                    core.Post(e.room_id, text, e.thread_id),
                ]
            else:
                res = adapter.load_books(workbook.books_tabs(ins))
                counts = {k: int(v) for k, v in res["counts"].items() if v}
                skipped = list(res.get("skipped", []))
                loaded = ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in counts.items())
                by_reason: dict[str, list[str]] = {}
                for s in skipped:
                    by_reason.setdefault(str(s["reason"]), []).append(str(s["invoice"]))
                kept = ", ".join(
                    f"{', '.join(invs)} ({why})" if len(invs) <= 3 else f"{len(invs)} rows ({why})"
                    for why, invs in by_reason.items()
                )
                skip = f" · kept as they were: {kept}" if kept else ""
                per = ", ".join(res.get("periods", [])) or "no month"
                text = (
                    f"**{book.name}** loaded into the books by {e.actor_name} · {loaded} · "
                    f"{per}{skip}. `report <period>` to {SWITCH_EXECUTOR} reads them."
                )
                details = {"url": e.url[:200], "name": book.name, **counts, "skipped": len(skipped)}
                out = [
                    core.Audit("books_loaded", details=details, **who),
                    core.Post(e.room_id, text, e.thread_id),
                ]
        except (workbook.FileError, AdapterError) as exc:
            out = [
                core.Audit(
                    "load_refused", details={"url": e.url[:200], "error": str(exc)[:160]}, **who
                ),
                core.Post(e.room_id, f"**load** — {exc}.", e.thread_id),
            ]
        return self.apply(out)

    def _set_price(self, e: core.SetPrice) -> list[core.Effect]:
        """A person's list price edit, written by the ledger adapter and answered in the thread."""
        adapter = self.adapters.get("ledger")
        if adapter is None or not hasattr(adapter, "set_price"):
            return self.apply(
                [
                    core.Post(
                        e.room_id, f"**{e.product}** not repriced — no ledger here.", e.thread_id
                    )
                ]
            )
        try:
            data = adapter.set_price(e.product, e.amount_cents)
            out: list[core.Effect] = [
                core.Audit(
                    "price_set",
                    actor_id=e.actor_id,
                    actor_name=e.actor_name,
                    room_id=e.room_id,
                    details={"product": e.product, **data},
                ),
                core.Post(e.room_id, core.price_set_text(e, data), e.thread_id),
            ]
        except AdapterError as exc:
            out = [
                core.Audit(
                    "price_refused",
                    actor_id=e.actor_id,
                    actor_name=e.actor_name,
                    room_id=e.room_id,
                    details={"product": e.product, "error": str(exc)[:160]},
                ),
                core.Post(e.room_id, f"**{e.product}** not repriced — {exc}.", e.thread_id),
            ]
        return self.apply(out)

    def _book_sale(self, e: core.BookSale) -> list[core.Effect]:
        """A person's late sale, written by the ledger adapter and answered in the thread."""
        adapter = self.adapters.get("ledger")
        if adapter is None or not hasattr(adapter, "book_sale"):
            out: list[core.Effect] = [
                core.Post(e.room_id, f"**{e.invoice}** not booked — no ledger here.", e.thread_id)
            ]
        else:
            try:
                data = adapter.book_sale(e.invoice, e.customer, e.amount_cents)
                out = [
                    core.Audit(
                        "sale_booked",
                        actor_id=e.actor_id,
                        actor_name=e.actor_name,
                        room_id=e.room_id,
                        details={"invoice": e.invoice, **data},
                    ),
                    core.Post(e.room_id, core.sale_booked_text(e, data), e.thread_id),
                ]
            except AdapterError as exc:
                out = [
                    core.Audit(
                        "sale_refused",
                        actor_id=e.actor_id,
                        actor_name=e.actor_name,
                        room_id=e.room_id,
                        details={"invoice": e.invoice, "error": str(exc)[:160]},
                    ),
                    core.Post(e.room_id, f"**{e.invoice}** not booked — {exc}.", e.thread_id),
                ]
        return self.apply(out)

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

    def _confirm_paid(self, e: core.ConfirmPaid) -> list[core.Effect]:
        """The bank's confirmation for a sent run, typed by a person: the payables become paid,
        the payments are booked, the artifact lands in PAID."""
        a = self.store.get(e.artifact_id)
        adapter = self.adapters.get("ledger")
        if a is None or adapter is None or not hasattr(adapter, "confirm"):
            return self.apply(
                [
                    core.Post(
                        e.room_id,
                        f"**{e.artifact_id}** not confirmed — no ledger here.",
                        e.thread_id,
                    )
                ]
            )
        try:
            data = adapter.confirm(a.id, e.reference)
        except AdapterError as exc:
            return self.apply(
                [
                    core.Audit(
                        "paid_refused",
                        a.id,
                        actor_id=e.actor_id,
                        actor_name=e.actor_name,
                        room_id=e.room_id,
                        details={"error": str(exc)},
                    ),
                    core.Post(e.room_id, f"**{a.id}** not confirmed — {exc}.", e.thread_id),
                ]
            )
        current = self.store.get(a.id) or a
        return self.apply(
            core.after_confirm(self.context(), current, data, actor_name=e.actor_name)
        )

    def _poll_payment(self, aid: str) -> list[core.Effect]:
        """Where a sent run stands on its rail; book what the rail calls paid."""
        a = self.store.get(aid)
        adapter = self.adapters.get("ledger")
        if a is None or adapter is None or not hasattr(adapter, "poll"):
            return []
        try:
            data = adapter.poll(a.id)
        except AdapterError as exc:
            return self.apply([core.Audit("poll_failed", a.id, details={"error": str(exc)})])
        except Exception as exc:  # the rail is a network; never let it kill the executor
            log.exception("poll crashed")
            return self.apply(
                [core.Audit("poll_failed", a.id, details={"error": exc.__class__.__name__})]
            )
        effects: list[core.Effect] = []
        if data.get("failed") and self.store.get_value(f"poll_failed:{a.id}") is None:
            effects += [
                core.SetValue(f"poll_failed:{a.id}", ", ".join(data["failed"])[:200]),
                core.Post(
                    a.room_id,
                    f"**{a.id}** · {data.get('rail', 'the rail')} reports failed payouts: "
                    f"{', '.join(data['failed'])}. The run stays open until they are retried.",
                    a.thread_id,
                ),
            ]
        if data.get("paid_now") or int(data.get("paid", 0)) >= len(a.proposal().body.run):
            current = self.store.get(a.id) or a
            effects += core.after_confirm(self.context(), current, data)
        return self.apply(effects) if effects else []

    def _poll_invoice(self, aid: str) -> list[core.Effect]:
        """Whether a won deal's invoice is paid; the receipt is booked when it is."""
        a = self.store.get(aid)
        adapter = self.adapters.get("ledger")
        if a is None or adapter is None or not hasattr(adapter, "poll_invoice"):
            return []
        invoice_id = str(a.result().get("invoice_id") or "")
        try:
            data = adapter.poll_invoice(a.proposal().body.deal_id, invoice_id)
        except AdapterError as exc:
            return self.apply([core.Audit("poll_failed", a.id, details={"error": str(exc)})])
        except Exception as exc:  # the biller is a network; never let it kill the executor
            log.exception("invoice poll crashed")
            return self.apply(
                [core.Audit("poll_failed", a.id, details={"error": exc.__class__.__name__})]
            )
        if not data.get("paid"):
            return []
        return self.apply(core.after_collect(self.context(), a, data))

    # -- the connectors: the CRM, the signature, the board pack ----------------------------

    def _crm_sync(self, e: core.CrmSync) -> list[core.Effect]:
        """The deal into the CRM at its stage; a CRM failure is audited and, for a won deal,
        said in the room, never a reason to touch the books."""
        a = self.store.get(e.artifact_id)
        adapter = self.adapters.get("ledger")
        if a is None or adapter is None or getattr(adapter, "crm", None) is None:
            return []
        known = self.store.get_value(f"crm:{a.id}")
        try:
            data = adapter.crm_sync(a, e.stage, known_id=known)
        except AdapterError as exc:
            out: list[core.Effect] = [
                core.Audit("crm_failed", a.id, details={"stage": e.stage, "error": str(exc)})
            ]
            if e.stage != "open":
                out.append(
                    core.Post(a.room_id, f"**{a.id}** CRM not updated — {exc}.", a.thread_id)
                )
            return self.apply(out)
        except Exception as exc:  # the CRM is a network; never let it kill the executor
            log.exception("crm sync crashed")
            return self.apply(
                [
                    core.Audit(
                        "crm_failed",
                        a.id,
                        details={"stage": e.stage, "error": exc.__class__.__name__},
                    )
                ]
            )
        body: Any = a.proposal().body
        name = str(data.get("crm_name") or "the CRM")
        url = str(data.get("crm_url") or "")
        link = f" · {url}" if url else ""
        if e.stage == "open":
            text = f"**{a.id}** tracked in {name} at {data.get('crm_stage', 'open')}{link}"
        elif e.stage == "won":
            amount = money(int(body.price_cents), body.currency)
            text = f"**{a.id}** {name} deal closed won at {amount}{link}"
        else:
            text = f"**{a.id}** {name} deal closed lost{link}"
        return self.apply(
            [
                core.SetValue(f"crm:{a.id}", str(data.get("crm_deal_id") or "")),
                core.Audit("crm_synced", a.id, details={"stage": e.stage, **data}),
                core.Post(a.room_id, text, a.thread_id),
            ]
        )

    def _send_contract(self, aid: str) -> list[core.Effect]:
        """The won deal's order form out for signature, once."""
        a = self.store.get(aid)
        adapter = self.adapters.get("ledger")
        if a is None or adapter is None or getattr(adapter, "esign", None) is None:
            return []
        if self.store.get_value(f"esign:{a.id}") is not None:
            return []
        try:
            data = adapter.send_contract(a)
        except AdapterError as exc:
            return self.apply(
                [
                    core.Audit("contract_failed", a.id, details={"error": str(exc)}),
                    core.Post(a.room_id, f"**{a.id}** order form not sent — {exc}.", a.thread_id),
                ]
            )
        except Exception as exc:
            log.exception("send contract crashed")
            return self.apply(
                [core.Audit("contract_failed", a.id, details={"error": exc.__class__.__name__})]
            )
        mode = " (test mode)" if data.get("test_mode") else ""
        return self.apply(
            [
                core.SetValue(f"esign:{a.id}", str(data.get("signature_request_id") or "")),
                core.Audit("contract_sent", a.id, details=dict(data)),
                core.Post(
                    a.room_id,
                    f"**{a.id}** order form sent to {data.get('signer', 'the customer')} for "
                    f"signature via {data.get('provider', 'Dropbox Sign')}{mode} · request "
                    f"**{data.get('signature_request_id', '')}** · I check it until it is signed.",
                    a.thread_id,
                ),
            ]
        )

    def _poll_signature(self, aid: str) -> list[core.Effect]:
        a = self.store.get(aid)
        adapter = self.adapters.get("ledger")
        request_id = self.store.get_value(f"esign:{aid}")
        if a is None or adapter is None or not request_id or not hasattr(adapter, "poll_contract"):
            return []
        try:
            data = adapter.poll_contract(request_id)
        except AdapterError as exc:
            return self.apply([core.Audit("poll_failed", a.id, details={"error": str(exc)})])
        except Exception as exc:
            log.exception("signature poll crashed")
            return self.apply(
                [core.Audit("poll_failed", a.id, details={"error": exc.__class__.__name__})]
            )
        if data.get("declined"):
            return self.apply(
                [
                    core.SetValue(f"signed:{a.id}", "declined"),
                    core.Audit("contract_declined", a.id, details=dict(data)),
                    core.Post(
                        a.room_id,
                        f"**{a.id}** order form declined by the signer. The deal stays won on the "
                        "books; the CFO decides what follows.",
                        a.thread_id,
                    ),
                ]
            )
        if not data.get("complete"):
            return []
        when = f" at {data['signed_at']}" if data.get("signed_at") else ""
        return self.apply(
            [
                core.SetValue(f"signed:{a.id}", str(data.get("signed_at") or "signed")),
                core.Audit("contract_signed", a.id, details=dict(data)),
                core.Post(
                    a.room_id,
                    f"**{a.id}** order form signed by {data.get('signed_by') or 'the signer'}"
                    f"{when} · request **{request_id}**.",
                    a.thread_id,
                ),
            ]
        )

    def _build_pack(self, e: core.BuildPack) -> list[core.Effect]:
        """The month's report, charts and exports into Drive and a Slides deck."""
        from sindarin import reports
        from sindarin.agents.finance import month_label, report_lines

        if self.pack is None:
            if e.auto:
                return []
            return self.apply(
                [
                    core.Post(
                        e.room_id,
                        "No board pack destination is set: put SINDARIN_GOOGLE_SA and "
                        "GOOGLE_DRIVE_FOLDER_ID in .env, then `uv run sindarin connect drive`.",
                        e.thread_id,
                    )
                ]
            )
        if self.ledger is None:
            return self.apply([core.Post(e.room_id, "No ledger here to pack.", e.thread_id)])
        try:
            if e.period not in self.ledger.periods():
                held = ", ".join(self.ledger.periods()) or "no period at all"
                raise reports.ReportError(
                    f"the ledger has no period {e.period} yet; it holds {held}"
                )
            label = month_label(e.period)
            summary = self.ledger.summary(e.period)
            text = reports.report(self.ledger, e.period).text
            charts: list[tuple[str, bytes, str, str]] = []
            for kind in CHART_ORDER:
                try:
                    png = reports.chart_png(self.ledger, e.period, kind)
                except reports.ReportError:
                    continue
                caption = png.text.split(".**")[0].strip("*")
                charts.append((caption, png.data, png.mime, png.filename))
            exports: list[tuple[str, bytes, str]] = []
            for table in reports.EXPORTS:
                try:
                    out = reports.export(self.ledger, e.period, table)
                except reports.ReportError:
                    continue
                exports.append((out.filename, out.data, out.mime))
            built = self.pack.board_pack(
                e.period,
                label,
                report_lines=report_lines(summary),
                charts=charts,
                exports=exports,
                report_text=text,
            )
        except reports.ReportError as exc:
            return self.apply([core.Post(e.room_id, f"**pack {e.period}** — {exc}.", e.thread_id)])
        except Exception as exc:
            log.warning("board pack failed: %s", str(exc)[:160])
            return self.apply(
                [
                    core.Audit(
                        "pack_failed", details={"period": e.period, "error": str(exc)[:160]}
                    ),
                    core.Post(
                        e.room_id,
                        f"**pack {e.period}** — not built: {str(exc)[:160]}.",
                        e.thread_id,
                    ),
                ]
            )
        return self.apply(
            [
                core.Audit(
                    "pack_built",
                    actor_id=e.actor_id or None,
                    actor_name=e.actor_name or None,
                    room_id=e.room_id,
                    details={
                        "period": e.period,
                        **{k: v for k, v in built.items() if "link" not in k},
                    },
                ),
                core.Post(
                    e.room_id,
                    f"**{label} board pack** · deck {built.get('deck_link', '')} · files "
                    f"{built.get('folder_link', '')} · {built.get('files', 0)} files, "
                    f"{built.get('slides', 0)} slides, from the books.",
                    e.thread_id,
                ),
            ]
        )

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


def _compact(details: Mapping[str, Any], limit: int = 110) -> str:
    """Audit details as `key=value` pairs a person can read; digests and ids stay out."""
    skip = {"digest", "snapshot", "fence", "room_id", "url", "document_csv", "references"}
    parts = []
    for k, v in details.items():
        if k in skip or k.endswith(("_sha", "_id")) or v in (None, "", [], {}):
            continue
        if isinstance(v, int) and k.endswith("_cents"):
            v = f"{v / 100:,.2f}"
            k = k[:-6]
        elif isinstance(v, dict | list):
            v = str(len(v)) + (" items" if isinstance(v, list) else " fields")
        parts.append(f"{k}={v}")
    text = " ".join(parts)
    return text if len(text) <= limit else text[: limit - 1] + "…"
