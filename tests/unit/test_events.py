from __future__ import annotations

import json

import pytest

from sindarin.switch.events import (
    ControlFrame,
    MessageEvent,
    OtherEvent,
    RoomJoinEvent,
    message_envelope,
    parse_frame,
)


def test_message_envelope_round_trip():
    env = message_envelope(
        room_id="!r:s",
        sequence=4813,
        sender="@switch-user-1:s",
        sender_name="Alice",
        message_id="m1",
        body="hi",
        addressed=True,
        thread_id=None,
        timestamp=1700000000000,
    )
    ev = parse_frame(json.dumps(env))
    assert ev == MessageEvent(
        sequence=4813,
        room_id="!r:s",
        addressed=True,
        sender="@switch-user-1:s",
        sender_name="Alice",
        message_id="m1",
        body="hi",
        timestamp=1700000000000,
        thread_id=None,
        attachments=(),
        bridge_id="bridge-1",
        channel_type="channel_public",
    )
    assert env["id"] == 4813 and env["data"]["sequence"] == 4813


def test_control_frames():
    assert parse_frame('{"type":"ping"}') == ControlFrame("ping", {"type": "ping"})
    cs = parse_frame(
        {"type": "connection_state", "rooms": [{"room_id": "!r:s"}], "cursor": 7, "generation": 2}
    )
    assert isinstance(cs, ControlFrame) and cs.data["generation"] == 2


def test_live_control_envelope_is_a_control_frame():
    """switch-core 0.31 (agent-protocol 9) wraps control frames as {"event", "data"} (verified
    2026-11-01). They must not be mistaken for domain events, or pings go unanswered."""
    ping = parse_frame('{"event":"ping","data":{}}')
    assert ping == ControlFrame("ping", {})
    cs = parse_frame(
        {
            "event": "connection_state",
            "data": {
                "rooms": [],
                "cursor": 0,
                "generation": 8589934596,
                "heartbeat_interval_seconds": 2.0,
                "protocol": 9,
            },
        }
    )
    assert isinstance(cs, ControlFrame) and cs.type == "connection_state"
    assert cs.data["generation"] == 8589934596 and cs.data["heartbeat_interval_seconds"] == 2.0
    ev = parse_frame(
        {
            "event": "evicted",
            "data": {"code": "heartbeat_lapsed", "reason": "heartbeat lapsed", "room_id": None},
        }
    )
    assert isinstance(ev, ControlFrame) and ev.data["code"] == "heartbeat_lapsed"
    gap = parse_frame({"event": "gap", "data": {"resumed_at": 40}})
    assert isinstance(gap, ControlFrame) and gap.data["resumed_at"] == 40
    # a control name without a payload object still parses, with empty data
    assert parse_frame('{"event":"ping"}') == ControlFrame("ping", {})
    # domain events keep their own path
    assert not isinstance(
        parse_frame({"event": "room_join", "data": {"type": "room_join"}}), ControlFrame
    )


def test_room_join_and_other():
    rj = parse_frame(
        {
            "event": "room_join",
            "data": {
                "type": "room_join",
                "room_id": "!r:s",
                "payload": {
                    "member": "@a:s",
                    "member_name": "A",
                    "timestamp": 1,
                    "listening": True,
                },
                "sequence": 9,
            },
            "id": 9,
        }
    )
    assert rj == RoomJoinEvent(9, "!r:s", "@a:s", "A", 1, True)
    other = parse_frame(
        {"event": "room_leave", "data": {"room_id": "!r:s", "sequence": 10}, "id": 10}
    )
    assert isinstance(other, OtherEvent) and other.event == "room_leave" and other.sequence == 10


def test_bad_frames():
    with pytest.raises(ValueError):
        parse_frame("[]")
    with pytest.raises(ValueError):
        parse_frame({"nothing": 1})


def test_null_sequence_falls_back_to_envelope_id():
    ev = parse_frame(
        {
            "event": "message",
            "data": {"room_id": "!r:s", "payload": {}, "sequence": None},
            "id": 4813,
        }
    )
    assert isinstance(ev, MessageEvent) and ev.sequence == 4813


def test_missing_payload_fields_are_lenient():
    ev = parse_frame({"event": "message", "data": {"room_id": "!r:s", "payload": {}}, "id": 1})
    assert (
        isinstance(ev, MessageEvent)
        and ev.sender == ""
        and ev.thread_id is None
        and ev.sequence == 1
    )
