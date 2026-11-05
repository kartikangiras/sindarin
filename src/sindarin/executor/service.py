"""The long-running executor process (DDD §7, §16.1): one asyncio loop with a WebSocket reader,
a serial event handler, and a scheduler. `sindarin-exec run --executor switch|github`.
Also the `--probe` mode for the §12.4 verification."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

from sindarin.adapters.base import Adapter
from sindarin.executor import core
from sindarin.executor.config import Env, ExecutorConfig, load_executor
from sindarin.executor.runner import Effector, RunnerConfig
from sindarin.names import DISPLAY, GITHUB_EXECUTOR, SWITCH_EXECUTOR
from sindarin.store import Store
from sindarin.switch.client import ConnectionState, FatalEviction, SwitchClient, SwitchError
from sindarin.switch.events import ControlFrame, Frame, MessageEvent, RoomJoinEvent

log = logging.getLogger("sindarin.service")

TICK = object()


def runner_config(exec_dir: Path, cfg: ExecutorConfig) -> RunnerConfig:
    return RunnerConfig(
        executor=cfg.executor,
        self_id=cfg.self_id,
        policy_path=cfg.resolve(exec_dir, cfg.policy_path),
        users_path=cfg.resolve(exec_dir, cfg.users_path),
        peer_ids=frozenset(cfg.peer_ids),
        room_names=dict(cfg.room_names),
        executor_names=(cfg.executor, cfg.display_name),
        retry_after=timedelta(minutes=cfg.retry_minutes),
        max_attempts=cfg.max_attempts,
        post_audit_lines=cfg.post_audit_lines,
        post_status_fences=cfg.status_fences,
        peer_state_db=cfg.resolve(exec_dir, cfg.peer_state_db) if cfg.peer_state_db else None,
    )


class ExecutorService:
    def __init__(
        self,
        exec_dir: Path,
        env: Env,
        cfg: ExecutorConfig,
        client: SwitchClient,
        adapters: dict[str, Adapter],
    ) -> None:
        self.exec_dir = exec_dir
        self.env = env
        self.cfg = cfg
        self.client = client
        self.store = Store(cfg.db_path(exec_dir))
        self.effector = Effector(self.store, adapters, self._post, runner_config(exec_dir, cfg))
        self.queue: asyncio.Queue[object] = asyncio.Queue()
        self.stop = asyncio.Event()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sindarin-handler")
        saved = self.store.get_value("cursor")
        gen = self.store.get_value("generation")
        self.state = ConnectionState(
            connection_id=env.connection_id,
            cursor=int(saved) if saved else None,
            generation=int(gen) if gen else None,
        )

    def _post(self, room_id: str, text: str, thread_id: str | None) -> None:
        self.client.post(
            room_id,
            text,
            thread_id if self.cfg.thread_replies else None,
            connection_id=self.env.connection_id if self.state.connected else None,
        )

    def _learn_room_names(self) -> None:
        """Feature rooms are opened after we start; their names come from connection_state."""
        for room in self.state.rooms:
            rid = room.get("room_id") or room.get("id")
            name = room.get("name") or room.get("slug")
            if rid and name and rid not in self.effector.config.room_names:
                self.effector.config.room_names[rid] = f"#{str(name).lstrip('#')}"

    async def _on_frame(self, frame: Frame) -> None:
        if isinstance(frame, MessageEvent):
            await self.queue.put(frame)
        elif isinstance(frame, RoomJoinEvent):
            log.info("room_join %s in %s", frame.member_name or frame.member, frame.room_id)
        elif isinstance(frame, ControlFrame) and frame.type == "connection_state":
            if self.state.generation is not None:
                try:
                    self.store.set_value("generation", str(self.state.generation))
                except Exception:  # a busy store must not drop the connection
                    log.exception("could not persist generation")
            self._learn_room_names()
            for room in self.state.rooms:
                log.info("room: %s", json.dumps(room, sort_keys=True)[:200])
            if not self.state.rooms:
                log.info("connection_state lists no rooms; bridged rooms arrive with their events")

    async def _handler(self) -> None:
        while not self.stop.is_set():
            item = await self.queue.get()
            try:
                if item is TICK:
                    await self._in_worker(self.effector.sweep)
                elif isinstance(item, MessageEvent):
                    log.info(
                        "message %s in %s from %s (addressed=%s, thread=%s)",
                        item.message_id,
                        item.room_id,
                        item.sender,
                        item.addressed,
                        item.thread_id,
                    )
                    await self._in_worker(self.effector.handle_message, item)
                    if item.sequence is not None and (
                        self.state.cursor is None or item.sequence > self.state.cursor
                    ):
                        self.state.cursor = item.sequence
                        self.store.set_value("cursor", str(item.sequence))
            except Exception:
                log.exception("handler error; continuing")
            finally:
                self.queue.task_done()

    async def _in_worker(self, fn, *args):
        """Run store/adapter work on the single worker thread so pings keep being answered."""
        return await asyncio.get_running_loop().run_in_executor(self._pool, fn, *args)

    async def _scheduler(self) -> None:
        await self.queue.put(TICK)  # crash recovery on startup
        while not self.stop.is_set():
            await asyncio.sleep(self.cfg.sweep_interval_seconds)
            await self.queue.put(TICK)

    async def run(self) -> int:
        reader = asyncio.create_task(
            self.client.run_connection(self.state, self._on_frame, stop=self.stop)
        )
        handler = asyncio.create_task(self._handler())
        scheduler = asyncio.create_task(self._scheduler())
        code = 0
        try:
            await reader
        except FatalEviction as exc:
            log.error("%s", exc)
            code = 2
        except SwitchError as exc:
            log.error("switch error: %s", exc)
            code = 3
        except asyncio.CancelledError:
            pass
        finally:
            self.stop.set()
            for t in (handler, scheduler):
                t.cancel()
            await asyncio.gather(handler, scheduler, return_exceptions=True)
            # Cancelling the handler does not stop a thread mid-merge: wait for it first.
            await asyncio.to_thread(self._pool.shutdown, True)
            self.store.close()
        return code


def build_adapters(env: Env, cfg: ExecutorConfig, exec_dir: Path) -> dict[str, Adapter]:
    """One adapter per executor, one credential each (DDD §4.2)."""
    if cfg.executor == GITHUB_EXECUTOR:
        if cfg.github_mode == "simulated":
            from sindarin.adapters.fake import FakeGitHub
            from sindarin.policy import load_policy

            fake = FakeGitHub(path=str(exec_dir / "simulated-github.json"))
            try:
                policy = load_policy(cfg.resolve(exec_dir, cfg.policy_path))
                repos = [r.repo for r in policy.rules if r.repo]
                if repos:
                    fake.repo = repos[0]
                    fake._save()
            except (OSError, ValueError):
                pass
            return {"github": fake}
        from sindarin.adapters.github import GitHubAdapter

        return {"github": GitHubAdapter(env.github_token)}
    from sindarin.adapters.switch_rooms import SwitchRoomsAdapter
    from sindarin.switch.gateway import GatewayClient

    gateway = GatewayClient.login(env.gateway_url, env.gateway_email, env.gateway_password)
    bridge = cfg.bridge_id or gateway.default_bridge_id() or ""
    return {
        "switch": SwitchRoomsAdapter(
            gateway,
            bridge_id=bridge,
            directory=cfg.directory,
            scoped_addressing=cfg.addressing == "rooms",
        ),
    }


async def run_executor(exec_dir: Path, short: str) -> int:
    env, cfg = load_executor(exec_dir, short)
    required = [
        "SWITCH_BRIDGE_URL",
        "SWITCH_AGENT_ID",
        "SWITCH_AGENT_API_KEY",
        "SWITCH_CONNECTION_ID",
    ]
    simulated = cfg.executor == GITHUB_EXECUTOR and cfg.github_mode == "simulated"
    if cfg.executor == GITHUB_EXECUTOR and not simulated:
        required.append("GITHUB_TOKEN")
    if cfg.executor == SWITCH_EXECUTOR:
        required += ["SWITCH_GATEWAY_EMAIL", "SWITCH_GATEWAY_PASSWORD"]
    missing = env.missing(*required)
    env_file = exec_dir / f"{short}.env"
    if missing:
        log.error("missing in %s: %s (run `sindarin bootstrap`)", env_file, ", ".join(missing))
        return 1
    if cfg.missing():
        log.error("missing in %s: %s", exec_dir / f"{short}.yaml", ", ".join(cfg.missing()))
        return 1
    if simulated:
        log.warning(
            "github_mode: simulated — merges and releases are recorded, GitHub is not called"
        )
    client = SwitchClient(env.bridge_url, env.agent_id, env.api_key, client_name=cfg.executor)
    try:
        adapters = build_adapters(env, cfg, exec_dir)
    except Exception as exc:
        log.error("adapter setup failed: %s", str(exc)[:200])
        return 1
    service = ExecutorService(exec_dir, env, cfg, client, adapters)
    log.info("%s starting · self=%s · peers=%s", cfg.executor, cfg.self_id, cfg.peer_ids)
    try:
        return await service.run()
    finally:
        client.close()


async def run_probe(exec_dir: Path, short: str, seconds: int = 60) -> int:
    """Connect, print connection_state, answer pings, log frames, print the post_message op
    schema. Never posts, never touches GitHub or the Gateway."""
    env, cfg = load_executor(exec_dir, short)
    missing = env.missing(
        "SWITCH_BRIDGE_URL", "SWITCH_AGENT_ID", "SWITCH_AGENT_API_KEY", "SWITCH_CONNECTION_ID"
    )
    if missing:
        print(f"missing in {exec_dir / (short + '.env')}: {', '.join(missing)} (run bootstrap)")
        return 1
    client = SwitchClient(env.bridge_url, env.agent_id, env.api_key, client_name=cfg.executor)
    state = ConnectionState(connection_id=env.connection_id)
    stop = asyncio.Event()
    counts: dict[str, int] = {}

    async def on_frame(frame: Frame) -> None:
        if isinstance(frame, ControlFrame):
            counts[frame.type] = counts.get(frame.type, 0) + 1
            if frame.type == "connection_state":
                print("connection_state:")
                print(json.dumps(frame.data, indent=2, sort_keys=True)[:4000])
            else:
                print(f"control frame: {json.dumps(frame.data, sort_keys=True)[:300]}")
        elif isinstance(frame, MessageEvent):
            counts["message"] = counts.get("message", 0) + 1
            print(
                f"message seq={frame.sequence} room={frame.room_id} sender={frame.sender} "
                f"name={frame.sender_name!r} message_id={frame.message_id} "
                f"thread_id={frame.thread_id} "
                f"addressed={frame.addressed} body={frame.body[:80]!r}"
            )
            if frame.sequence is not None:
                state.cursor = frame.sequence
        elif isinstance(frame, RoomJoinEvent):
            counts["room_join"] = counts.get("room_join", 0) + 1
            print(
                f"room_join room={frame.room_id} member={frame.member} "
                f"name={frame.member_name!r} listening={frame.listening}"
            )
        else:
            counts[frame.event] = counts.get(frame.event, 0) + 1
            print(f"event {frame.event} seq={frame.sequence} room={frame.room_id}")

    print(
        f"probe {DISPLAY[cfg.executor]}: bridge={env.bridge_url} agent={env.agent_id} "
        f"connection_id={env.connection_id}"
    )
    try:
        print("health:", json.dumps(client.health())[:300])
    except SwitchError as exc:
        print("health failed:", exc)
    started = time.monotonic()
    task = asyncio.create_task(client.run_connection(state, on_frame, stop=stop))
    code = 0
    try:
        while time.monotonic() - started < seconds and not task.done():
            await asyncio.sleep(0.5)
        if task.done() and task.exception():
            raise task.exception()  # type: ignore[misc]
    except (FatalEviction, SwitchError) as exc:
        print("connection failed:", exc)
        code = 2
    finally:
        stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    print(
        f"probe done after {int(time.monotonic() - started)}s: "
        f"pings answered={state.pings_answered}, frames={counts}"
    )
    print(f"rooms seen in connection_state: {len(state.rooms)}")
    for room in state.rooms:
        print("  ", json.dumps(room, sort_keys=True)[:300])
    try:
        ops = client.ops_catalog()
        print(f"ops catalog: {sorted(ops)}")
        for name in ("post_message", "create_room", "switch_create_room", "list_rooms"):
            if name in ops:
                print(
                    f"{name} input_schema: "
                    f"{json.dumps(ops[name].get('input_schema', ops[name]), sort_keys=True)[:800]}"
                )
    except SwitchError as exc:
        print("ops catalog failed:", exc)
    client.close()
    return code


__all__ = ["ExecutorService", "build_adapters", "core", "run_executor", "run_probe"]
