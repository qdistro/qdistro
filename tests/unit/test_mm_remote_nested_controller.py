"""Socket-boundary tests for the long-running R6 nested controller."""
from __future__ import annotations

import base64
import json
import select
import socket

import pytest

from multimachine.remote_adapter import RemoteAdapterError
from multimachine.remote_adapter_transport import MAX_LOCAL_FRAME_BYTES, recv_frame
from multimachine.remote_nested_protocol import (
    LocalBoundaryMessage,
    RawFrame,
    StreamAnnouncement,
    encode_input_event,
)
from multimachine.remote_nested_service import (
    MAX_BOUNDARY_FRAME_BYTES,
    RemoteNestedController,
)


ANNOUNCEMENT = StreamAnnouncement(
    source_revision=7, width=4, height=3, stride=16,
    app_id="org.example.Editor", title="Remote editor")
FRAMES = [
    RawFrame(4, 3, 16, bytes([value]) * 48) for value in (1, 2, 3)
]


def _controller(role: str):
    endpoint, endpoint_peer = socket.socketpair()
    helper, helper_peer = socket.socketpair()
    return (
        RemoteNestedController(role=role, endpoint=endpoint, helper=helper),
        endpoint_peer, helper_peer,
    )


def _event(controller: RemoteNestedController, **event) -> None:
    controller._handle_endpoint(json.dumps(event).encode())


def _endpoint_request(peer: socket.socket) -> dict:
    payload = recv_frame(peer, maximum=MAX_LOCAL_FRAME_BYTES)
    assert payload is not None
    return json.loads(payload)


def _helper_event(peer: socket.socket) -> LocalBoundaryMessage:
    payload = recv_frame(peer, maximum=MAX_BOUNDARY_FRAME_BYTES)
    assert payload is not None
    return LocalBoundaryMessage.decode(payload)


def _received(*, channel: str, kind: str, seq: int, payload: bytes = b"",
              ack: int = 0) -> dict:
    return {
        "op": "received", "channel": channel, "kind": kind,
        "seq": seq, "ack": ack,
        "payload": base64.urlsafe_b64encode(payload).rstrip(b"=").decode(),
    }


def test_source_controller_coalesces_frames_releases_input_and_reconciles() -> None:
    controller, endpoint, helper = _controller("source")
    try:
        _event(controller, op="connected", epoch=1)
        assert _helper_event(helper) == LocalBoundaryMessage("connected", seq=1)

        controller._handle_helper(LocalBoundaryMessage(
            "announce", payload=ANNOUNCEMENT.encode()))
        announce = _endpoint_request(endpoint)
        assert (announce["channel"], announce["kind"]) == ("control", "announce")

        for frame in FRAMES:
            controller._handle_helper(LocalBoundaryMessage(
                "frame", payload=frame.encode()))
        sent = [_endpoint_request(endpoint), _endpoint_request(endpoint)]
        assert [item["kind"] for item in sent] == ["frame_bgrx", "frame_bgrx"]
        assert not select.select([endpoint], [], [], 0)[0]

        _event(controller, **_received(
            channel="control", kind="media_ack", seq=1, ack=1))
        assert _helper_event(helper) == LocalBoundaryMessage("media_ack", seq=1)
        coalesced = _endpoint_request(endpoint)
        encoded = base64.urlsafe_b64decode(
            coalesced["payload"] + "=" * (-len(coalesced["payload"]) % 4))
        assert RawFrame.decode(encoded) == FRAMES[2]

        key = encode_input_event("key", {"code": 42, "pressed": True})
        _event(controller, **_received(
            channel="input", kind="key", seq=1, payload=key))
        assert _helper_event(helper) == LocalBoundaryMessage("key", payload=key)

        _event(controller, op="detached", epoch=1,
               releases=[{"kind": "key", "code": 42}])
        release = _helper_event(helper)
        assert release.kind == "key"
        assert json.loads(release.payload) == {"code": 42, "pressed": False}
        assert _helper_event(helper) == LocalBoundaryMessage("detached", seq=1)

        _event(controller, op="connected", epoch=2)
        assert _helper_event(helper) == LocalBoundaryMessage("connected", seq=2)
        reconcile = _endpoint_request(endpoint)
        assert (reconcile["channel"], reconcile["kind"]) == (
            "control", "reconcile")
    finally:
        endpoint.close()
        helper.close()


def test_viewer_controller_acks_only_helper_delivered_frames_and_sends_input() -> None:
    controller, endpoint, helper = _controller("viewer")
    try:
        _event(controller, op="connected", epoch=1)
        assert _helper_event(helper).kind == "connected"
        _event(controller, **_received(
            channel="control", kind="announce", seq=1,
            payload=ANNOUNCEMENT.encode()))
        assert _helper_event(helper) == LocalBoundaryMessage(
            "announce", seq=7, payload=ANNOUNCEMENT.encode())

        _event(controller, **_received(
            channel="media", kind="frame_bgrx", seq=1,
            payload=FRAMES[0].encode()))
        delivered = _helper_event(helper)
        assert (delivered.kind, delivered.seq) == ("frame", 1)
        assert not select.select([endpoint], [], [], 0)[0]

        controller._handle_helper(LocalBoundaryMessage("decoder_ack", seq=1))
        ack = _endpoint_request(endpoint)
        assert (ack["kind"], ack["ack"]) == ("media_ack", 1)

        motion = encode_input_event(
            "motion", {"x_fixed": 256, "y_fixed": -128})
        controller._handle_helper(LocalBoundaryMessage(
            "motion", payload=motion))
        outgoing = _endpoint_request(endpoint)
        assert (outgoing["channel"], outgoing["kind"]) == ("input", "motion")

        _event(controller, op="detached", epoch=1, releases=[])
        assert _helper_event(helper) == LocalBoundaryMessage("detached", seq=1)
        with pytest.raises(RemoteAdapterError, match="disabled"):
            controller._handle_helper(LocalBoundaryMessage(
                "key", payload=encode_input_event(
                    "key", {"code": 30, "pressed": True})))
    finally:
        endpoint.close()
        helper.close()


def test_controller_rejects_cross_role_helper_operations_and_bad_events() -> None:
    source, source_endpoint, source_helper = _controller("source")
    viewer, viewer_endpoint, viewer_helper = _controller("viewer")
    try:
        with pytest.raises(RemoteAdapterError, match="not allowed"):
            source._handle_helper(LocalBoundaryMessage("decoder_ack", seq=1))
        with pytest.raises(RemoteAdapterError, match="not allowed"):
            viewer._handle_helper(LocalBoundaryMessage(
                "frame", payload=FRAMES[0].encode()))
        viewer._handle_helper(LocalBoundaryMessage("drop_transport"))
        assert recv_frame(
            viewer_endpoint, maximum=MAX_LOCAL_FRAME_BYTES
        ) == b'{"op":"drop_transport"}'
        with pytest.raises(RemoteAdapterError, match="unsupported"):
            source._handle_endpoint(b'{"op":"mystery"}')
    finally:
        for peer in (source_endpoint, source_helper, viewer_endpoint, viewer_helper):
            peer.close()
