"""Production-facing R6 source/viewer local state tests."""
from __future__ import annotations

import pytest

from multimachine.remote_adapter import FlowControlError, RemoteAdapterError, ReplayError
from multimachine.remote_nested_protocol import RawFrame, StreamAnnouncement
from multimachine.remote_nested_service import SourceNestedState, ViewerNestedState


ANNOUNCEMENT = StreamAnnouncement(
    source_revision=7, width=4, height=3, stride=16,
    app_id="org.example.Editor", title="Remote editor")
FRAME_1 = RawFrame(4, 3, 16, b"a" * 48)
FRAME_2 = RawFrame(4, 3, 16, b"b" * 48)


def _attach() -> tuple[SourceNestedState, ViewerNestedState]:
    source = SourceNestedState()
    viewer = ViewerNestedState()
    source.transport_connected(1)
    viewer.transport_connected(1)
    request = source.announce(ANNOUNCEMENT)
    viewer.receive_control(request.kind, request.payload)
    return source, viewer


def test_source_to_viewer_announce_frame_decode_ack_and_input() -> None:
    source, viewer = _attach()
    media = source.capture_frame(FRAME_1)
    decoded = viewer.receive_media(1, media.payload)
    assert decoded.frame == FRAME_1
    assert viewer.decoder_pending == frozenset({1})
    ack = viewer.decoder_acknowledged(1)
    assert (ack.channel, ack.kind, ack.ack) == ("control", "media_ack", 1)

    outgoing = viewer.send_input("motion", {"x_fixed": 256, "y_fixed": -128})
    landed = source.receive_input(outgoing.kind, outgoing.payload)
    assert landed.event == {"x_fixed": 256, "y_fixed": -128}
    assert not landed.synthetic

    close_request = viewer.request_close(7)
    assert (close_request.channel, close_request.kind) == (
        "control", "close_request")
    assert source.receive_close_request(7) == 7


def test_decoder_window_is_bounded_and_ack_is_delivery_owned() -> None:
    source, viewer = _attach()
    viewer.receive_media(1, source.capture_frame(FRAME_1).payload)
    viewer.receive_media(2, source.capture_frame(FRAME_2).payload)
    with pytest.raises(FlowControlError, match="window is full"):
        viewer.receive_media(3, source.capture_frame(FRAME_1).payload)
    with pytest.raises(FlowControlError, match="undelivered"):
        viewer.decoder_acknowledged(3)
    assert viewer.decoder_acknowledged(2).ack == 2
    assert not viewer.decoder_pending


def test_disconnect_blanks_viewer_releases_source_input_and_reconciles() -> None:
    source, viewer = _attach()
    press = viewer.send_input("key", {"code": 42, "pressed": True})
    assert source.receive_input(press.kind, press.payload).event["pressed"]

    releases = source.transport_detached(
        1, [{"kind": "key", "code": 42}])
    viewer.transport_detached(1)
    assert len(releases) == 1
    assert releases[0].synthetic
    assert releases[0].event == {"code": 42, "pressed": False}
    with pytest.raises(RemoteAdapterError, match="disabled"):
        viewer.send_input("key", {"code": 30, "pressed": True})

    source.transport_connected(2)
    viewer.transport_connected(2)
    reconcile = source.reconcile()
    restored = viewer.receive_control(reconcile.kind, reconcile.payload)
    assert restored == ANNOUNCEMENT
    assert viewer.attached and viewer.source_alive
    assert viewer.receive_media(
        1, source.capture_frame(FRAME_2).payload).seq == 1


def test_old_epoch_detach_and_reconnect_are_rejected() -> None:
    source, viewer = _attach()
    source.transport_detached(1, [])
    viewer.transport_detached(1)
    source.transport_connected(2)
    viewer.transport_connected(2)
    for state in (source, viewer):
        with pytest.raises(ReplayError, match="stale"):
            state.transport_connected(2)
    with pytest.raises(ReplayError, match="stale"):
        source.transport_detached(1, [])
    with pytest.raises(ReplayError, match="stale"):
        viewer.transport_detached(1)


def test_source_close_is_authoritative_and_revision_guarded() -> None:
    source, viewer = _attach()
    with pytest.raises(ReplayError, match="revision"):
        source.source_closed(7)
    closed = source.source_closed(8)
    assert viewer.receive_control(closed.kind, closed.payload) is None
    assert not viewer.source_alive and not viewer.attached
    with pytest.raises(RemoteAdapterError, match="closed"):
        source.capture_frame(FRAME_1)
    with pytest.raises(RemoteAdapterError, match="disabled"):
        viewer.send_input("button", {"code": 272, "pressed": True})


def test_viewer_rejects_media_before_announce_and_wrong_geometry() -> None:
    viewer = ViewerNestedState()
    viewer.transport_connected(1)
    with pytest.raises(RemoteAdapterError, match="live attachment"):
        viewer.receive_media(1, FRAME_1.encode())
    viewer.receive_control("announce", ANNOUNCEMENT.encode())
    wrong = RawFrame(3, 3, 12, b"x" * 36)
    with pytest.raises(RemoteAdapterError, match="geometry"):
        viewer.receive_media(1, wrong.encode())


def test_reconcile_cannot_roll_back_source_revision() -> None:
    _source, viewer = _attach()
    older = StreamAnnouncement(
        source_revision=6, width=4, height=3, stride=16,
        app_id="org.example.Editor", title="stale")
    with pytest.raises(ReplayError, match="revision"):
        viewer.receive_control("reconcile", older.encode())
    changed_without_revision = StreamAnnouncement(
        source_revision=7, width=4, height=3, stride=16,
        app_id="org.example.Editor", title="changed")
    with pytest.raises(ReplayError, match="changed"):
        viewer.receive_control(
            "reconcile", changed_without_revision.encode())


def test_duplicate_announce_and_unknown_control_fail_closed() -> None:
    _source, viewer = _attach()
    with pytest.raises(ReplayError, match="duplicate"):
        viewer.receive_control("announce", ANNOUNCEMENT.encode())
    with pytest.raises(RemoteAdapterError, match="unsupported"):
        viewer.receive_control("clipboard", b"{}")
