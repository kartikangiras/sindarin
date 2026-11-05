"""Frame parsing for the Switch agent WebSocket (SPEC §5.3). Pure; no I/O."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ControlFrame:
    type: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MessageEvent:
    sequence: int | None
    room_id: str
    addressed: bool
    sender: str
    sender_name: str
    message_id: str
    body: str
    timestamp: int
    thread_id: str | None = None
    attachments: tuple[Any, ...] = ()
    bridge_id: str | None = None
    channel_type: str | None = None


@dataclass(frozen=True)
class RoomJoinEvent:
    sequence: int | None
    room_id: str
    member: str
    member_name: str
    timestamp: int
    listening: bool


@dataclass(frozen=True)
class OtherEvent:
    event: str
    sequence: int | None
    room_id: str | None
    data: dict[str, Any]


Frame = ControlFrame | MessageEvent | RoomJoinEvent | OtherEvent


# Connection-level frames. The live bridge (switch-core 0.31, agent-protocol 9, verified
# 2026-11-01) sends them as {"event": "<name>", "data": {...}}; older docs showed
# {"type": "<name>", ...} with the payload at the top level. Both shapes become a ControlFrame
# whose `data` is the payload, so the client reads cursor/generation/rooms/code from one place.
CONTROL_EVENTS = frozenset({"connection_state", "ping", "pong", "evicted", "gap", "refused"})


def parse_frame(raw: str | bytes | dict[str, Any]) -> Frame:
    obj = json.loads(raw) if not isinstance(raw, dict) else raw
    if not isinstance(obj, dict):
        raise ValueError("frame is not an object")
    event = obj.get("event")
    if isinstance(event, str) and event in CONTROL_EVENTS:
        data = obj.get("data")
        return ControlFrame(type=event, data=data if isinstance(data, dict) else {})
    if isinstance(event, str) and isinstance(obj.get("data"), dict):
        return _parse_domain(obj)
    if isinstance(obj.get("type"), str):
        return ControlFrame(type=obj["type"], data=obj)
    raise ValueError(f"unrecognised frame keys: {sorted(obj)}")


def _parse_domain(obj: dict[str, Any]) -> Frame:
    data = obj["data"]
    seq = data.get("sequence")
    if seq is None:
        seq = obj.get("id")
    seq = int(seq) if seq is not None else None
    event = str(obj["event"])
    payload = data.get("payload") or {}
    room_id = data.get("room_id")
    if event == "message":
        return MessageEvent(
            sequence=seq,
            room_id=str(room_id or ""),
            addressed=bool(payload.get("addressed", False)),
            sender=str(payload.get("sender") or ""),
            sender_name=str(payload.get("sender_name") or ""),
            message_id=str(payload.get("message_id") or ""),
            body=str(payload.get("body") or ""),
            timestamp=int(payload.get("timestamp") or 0),
            thread_id=_opt_str(payload.get("thread_id")),
            attachments=tuple(payload.get("attachments") or ()),
            bridge_id=_opt_str(data.get("bridge_id")),
            channel_type=_opt_str(data.get("channel_type")),
        )
    if event == "room_join":
        return RoomJoinEvent(
            sequence=seq,
            room_id=str(room_id or ""),
            member=str(payload.get("member") or ""),
            member_name=str(payload.get("member_name") or ""),
            timestamp=int(payload.get("timestamp") or 0),
            listening=bool(payload.get("listening", False)),
        )
    return OtherEvent(event=event, sequence=seq, room_id=_opt_str(room_id), data=data)


def _opt_str(v: Any) -> str | None:
    if v is None or v == "":
        return None
    return str(v)


def message_envelope(
    *,
    room_id: str,
    sequence: int,
    sender: str,
    sender_name: str,
    message_id: str,
    body: str,
    addressed: bool,
    thread_id: str | None = None,
    timestamp: int = 0,
    channel_type: str = "channel_public",
    bridge_id: str = "bridge-1",
) -> dict[str, Any]:
    """Build a `message` envelope with the real frame shape (§5.3). Used by tests and --probe."""
    return {
        "event": "message",
        "data": {
            "type": "message",
            "room_id": room_id,
            "bridge_id": bridge_id,
            "channel_type": channel_type,
            "payload": {
                "addressed": addressed,
                "sender": sender,
                "sender_name": sender_name,
                "message_id": message_id,
                "body": body,
                "timestamp": timestamp,
                "thread_id": thread_id,
                "attachments": [],
            },
            "sequence": sequence,
        },
        "id": sequence,
    }
