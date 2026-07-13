"""Local pixel/input boundary tests for the R6 nested remote adapter."""
from __future__ import annotations

import json
import struct

import pytest

from multimachine.remote_adapter import CHANNEL_PAYLOAD_LIMITS, RemoteAdapterError
from multimachine.remote_nested_protocol import (
    FRAME_HEADER,
    LOCAL_HEADER,
    LocalBoundaryMessage,
    RawFrame,
    StreamAnnouncement,
    decode_input_event,
    encode_input_event,
)


def _announcement(**changes) -> StreamAnnouncement:
    values = {
        "source_revision": 7,
        "width": 4,
        "height": 3,
        "stride": 16,
        "app_id": "org.example.Editor",
        "title": "Editor — remote",
    }
    values.update(changes)
    return StreamAnnouncement(**values)


def _frame(**changes) -> RawFrame:
    values = {
        "width": 4,
        "height": 3,
        "stride": 16,
        "pixels": bytes(range(48)),
    }
    values.update(changes)
    return RawFrame(**values)


def test_announcement_and_frame_round_trip_exactly() -> None:
    announcement = _announcement()
    assert StreamAnnouncement.decode(announcement.encode()) == announcement
    frame = _frame()
    assert RawFrame.decode(frame.encode()) == frame
    assert frame.matches(announcement)


@pytest.mark.parametrize("change,match", [
    ({"width": 0}, "width"),
    ({"width": 4, "stride": 20}, "stride"),
    ({"app_id": ""}, "app_id"),
    ({"app_id": "org.example bad"}, "app_id"),
    ({"app_id": "org.example\nspoof"}, "control"),
    ({"title": "x" * 4097}, "title"),
    ({"source_revision": True}, "source_revision"),
])
def test_announcement_rejects_invalid_geometry_identity_and_revision(
        change, match) -> None:
    with pytest.raises(RemoteAdapterError, match=match):
        _announcement(**change).encode()


def test_announcement_rejects_unknown_fields_schema_and_pixel_format() -> None:
    valid = json.loads(_announcement().encode())
    for change, match in (
        ({"extra": 1}, "fields"),
        ({"schema": "other"}, "schema"),
        ({"pixel_format": "RGBA"}, "pixel format"),
    ):
        hostile = {**valid, **change}
        with pytest.raises(RemoteAdapterError, match=match):
            StreamAnnouncement.decode(json.dumps(hostile).encode())


def test_announcement_and_frame_share_the_exact_media_bound() -> None:
    # Header bytes count toward the authenticated media envelope limit.
    width = 1024
    height = (CHANNEL_PAYLOAD_LIMITS["media"] - FRAME_HEADER.size) // (width * 4)
    stride = width * 4
    good = RawFrame(width, height, stride, b"\0" * (height * stride))
    good.encode()
    _announcement(width=width, height=height, stride=stride).encode()
    with pytest.raises(RemoteAdapterError, match="media limit"):
        _announcement(width=width, height=height + 1, stride=stride).encode()


@pytest.mark.parametrize("payload,match", [
    (b"", "truncated"),
    (b"x" * (FRAME_HEADER.size - 1), "truncated"),
])
def test_frame_rejects_truncation(payload, match) -> None:
    with pytest.raises(RemoteAdapterError, match=match):
        RawFrame.decode(payload)


def test_frame_rejects_header_tampering_length_and_geometry_mismatch() -> None:
    encoded = bytearray(_frame().encode())
    cases = []
    hostile = encoded.copy()
    hostile[0:4] = b"NOPE"
    cases.append((bytes(hostile), "unsupported"))
    hostile = encoded.copy()
    hostile[5] = 9
    cases.append((bytes(hostile), "format"))
    hostile = encoded.copy()
    struct.pack_into("!I", hostile, FRAME_HEADER.size - 4, 47)
    cases.append((bytes(hostile), "length"))
    hostile = encoded.copy()
    struct.pack_into("!I", hostile, 16, 20)
    cases.append((bytes(hostile), "stride"))
    for payload, match in cases:
        with pytest.raises(RemoteAdapterError, match=match):
            RawFrame.decode(payload)


@pytest.mark.parametrize("kind,event", [
    ("key", {"code": 30, "pressed": True}),
    ("button", {"code": 272, "pressed": False}),
    ("motion", {"x_fixed": -256, "y_fixed": 8192}),
    ("axis", {"axis": 1, "value_fixed": -120}),
    ("focus", {"focused": True}),
])
def test_all_qdni_semantic_input_events_round_trip(kind, event) -> None:
    assert decode_input_event(kind, encode_input_event(kind, event)) == event


@pytest.mark.parametrize("kind,event,match", [
    ("touch", {"code": 1}, "unsupported"),
    ("key", {"code": 30, "pressed": 1}, "boolean"),
    ("button", {"code": 0, "pressed": True}, "code"),
    ("motion", {"x_fixed": 1}, "fields"),
    ("axis", {"axis": 2, "value_fixed": 1}, "axis"),
    ("focus", {"focused": True, "code": 1}, "fields"),
])
def test_input_rejects_unknown_kinds_shapes_and_boolean_confusion(
        kind, event, match) -> None:
    with pytest.raises(RemoteAdapterError, match=match):
        encode_input_event(kind, event)


def test_frame_must_match_announced_geometry() -> None:
    assert not _frame(width=3, stride=12, pixels=b"x" * 36).matches(
        _announcement())


@pytest.mark.parametrize("message", [
    LocalBoundaryMessage("connected", seq=2),
    LocalBoundaryMessage("announce", payload=_announcement().encode()),
    LocalBoundaryMessage("frame", seq=7, payload=_frame().encode()),
    LocalBoundaryMessage("key", payload=b'{"code":30,"pressed":true}'),
    LocalBoundaryMessage("detached", seq=3, payload=b"[]"),
    LocalBoundaryMessage("drop_transport"),
    LocalBoundaryMessage("shutdown"),
])
def test_local_helper_boundary_round_trip(message) -> None:
    assert LocalBoundaryMessage.decode(message.encode()) == message


def test_local_helper_boundary_rejects_truncation_header_and_unknown_kind() -> None:
    with pytest.raises(RemoteAdapterError, match="truncated"):
        LocalBoundaryMessage.decode(b"x" * (LOCAL_HEADER.size - 1))
    valid = bytearray(LocalBoundaryMessage("connected", seq=1).encode())
    valid[0:4] = b"NOPE"
    with pytest.raises(RemoteAdapterError, match="unsupported"):
        LocalBoundaryMessage.decode(bytes(valid))
    valid = bytearray(LocalBoundaryMessage("connected", seq=1).encode())
    valid[5] = 255
    with pytest.raises(RemoteAdapterError, match="unknown"):
        LocalBoundaryMessage.decode(bytes(valid))
    with pytest.raises(RemoteAdapterError, match="kind"):
        LocalBoundaryMessage("clipboard").encode()
