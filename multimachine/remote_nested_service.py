"""Local source/viewer state around one authenticated R6 endpoint.

The network endpoint owns cryptography, replay windows, and transport epochs.
These classes own the product-facing boundary: source capture metadata, local
decoder acknowledgement, QDNI semantics, blank-on-detach, and source-owned
lifetime.  They are deliberately independent of any particular Wayland or
PipeWire implementation so the C helpers can use the same contract.
"""
from __future__ import annotations

import base64
import binascii
import json
import select
import socket
from dataclasses import dataclass
from typing import Mapping

from .remote_adapter import FlowControlError, RemoteAdapterError, ReplayError
from .remote_adapter_transport import (
    MAX_LOCAL_FRAME_BYTES,
    recv_frame,
    send_frame,
)
from .remote_nested_protocol import (
    INPUT_KINDS,
    LOCAL_HEADER,
    LocalBoundaryMessage,
    RawFrame,
    StreamAnnouncement,
    decode_local_announcement,
    decode_local_input_event,
    decode_input_event,
    encode_local_announcement,
    encode_local_input_event,
    encode_input_event,
)


MAX_LOCAL_DECODE_PENDING = 2
MAX_BOUNDARY_FRAME_BYTES = 3 * 1024 * 1024 + LOCAL_HEADER.size
_CLOSED_FIELDS = frozenset({"source_revision"})


class _ControllerShutdown(Exception):
    pass


@dataclass(frozen=True)
class EndpointRequest:
    channel: str
    kind: str
    payload: bytes = b""
    ack: int = 0


@dataclass(frozen=True)
class InputInjection:
    kind: str
    event: Mapping
    synthetic: bool = False


@dataclass(frozen=True)
class DecoderFrame:
    seq: int
    frame: RawFrame


def _positive_integer(value: object, *, label: str) -> int:
    if (not isinstance(value, int) or isinstance(value, bool) or value <= 0):
        raise RemoteAdapterError(f"{label} must be a positive integer")
    return value


def _source_closed_payload(source_revision: int) -> bytes:
    _positive_integer(source_revision, label="source_revision")
    return json.dumps(
        {"source_revision": source_revision},
        separators=(",", ":")).encode("ascii")


def _decode_source_closed(payload: bytes) -> int:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RemoteAdapterError("source_closed payload is not JSON") from exc
    if not isinstance(value, Mapping) or set(value) != _CLOSED_FIELDS:
        raise RemoteAdapterError(
            "source_closed fields do not match schema")
    return _positive_integer(
        value["source_revision"], label="source_revision")


class SourceNestedState:
    """Validate local capture/input state before it reaches the endpoint."""

    def __init__(self) -> None:
        self.epoch = 0
        self.connected = False
        self.source_revision = 0
        self.announcement: StreamAnnouncement | None = None
        self.closed = False

    def transport_connected(self, epoch: int) -> None:
        epoch = _positive_integer(epoch, label="connection epoch")
        if epoch <= self.epoch:
            raise ReplayError("connection epoch is stale")
        self.epoch = epoch
        self.connected = True

    def announce(self, announcement: StreamAnnouncement) -> EndpointRequest:
        if self.closed:
            raise RemoteAdapterError("source cannot announce after source closed")
        if not self.connected:
            raise RemoteAdapterError("source cannot announce while detached")
        announcement.validate()
        if announcement.source_revision <= self.source_revision:
            raise ReplayError("source announcement revision is stale")
        self.source_revision = announcement.source_revision
        self.announcement = announcement
        return EndpointRequest(
            channel="control", kind="announce",
            payload=announcement.encode())

    def reconcile(self) -> EndpointRequest:
        if not self.connected or self.closed or self.announcement is None:
            raise RemoteAdapterError("source has no live stream to reconcile")
        return EndpointRequest(
            channel="control", kind="reconcile",
            payload=self.announcement.encode())

    def capture_frame(self, frame: RawFrame) -> EndpointRequest:
        if self.closed:
            raise RemoteAdapterError("source cannot send a frame after source closed")
        if not self.connected or self.announcement is None:
            raise RemoteAdapterError("source cannot send a frame while detached")
        frame.validate()
        if not frame.matches(self.announcement):
            raise RemoteAdapterError(
                "captured frame does not match announced geometry")
        return EndpointRequest(
            channel="media", kind="frame_bgrx", payload=frame.encode())

    def receive_input(self, kind: str, payload: bytes) -> InputInjection:
        if self.closed:
            raise RemoteAdapterError("source input is disabled after source closed")
        if not self.connected:
            raise RemoteAdapterError("source input is disabled while detached")
        return InputInjection(kind=kind, event=decode_input_event(kind, payload))

    def receive_close_request(self, source_revision: int) -> int:
        if self.closed or not self.connected or self.announcement is None:
            raise RemoteAdapterError(
                "source close request arrived without a live stream")
        source_revision = _positive_integer(
            source_revision, label="source_revision")
        if source_revision != self.source_revision:
            raise ReplayError("close request source revision is stale")
        return source_revision

    def transport_detached(
            self, epoch: int, releases: list[Mapping]) -> tuple[InputInjection, ...]:
        if epoch != self.epoch or not self.connected:
            raise ReplayError("detach epoch is stale or not attached")
        injections = []
        for release in releases:
            if (not isinstance(release, Mapping)
                    or set(release) != {"kind", "code"}
                    or release["kind"] not in {"key", "button"}):
                raise RemoteAdapterError("synthetic release fields are invalid")
            event = decode_input_event(
                release["kind"], encode_input_event(
                    release["kind"], {
                        "code": release["code"], "pressed": False,
                    }))
            injections.append(InputInjection(
                kind=release["kind"], event=event, synthetic=True))
        self.connected = False
        return tuple(injections)

    def source_closed(self, source_revision: int) -> EndpointRequest:
        if not self.connected or self.closed:
            raise RemoteAdapterError("source cannot close while detached or closed")
        source_revision = _positive_integer(
            source_revision, label="source_revision")
        if source_revision <= self.source_revision:
            raise ReplayError("source close revision is stale")
        self.source_revision = source_revision
        self.closed = True
        self.announcement = None
        return EndpointRequest(
            channel="control", kind="source_closed",
            payload=_source_closed_payload(source_revision))


class ViewerNestedState:
    """Own viewer blanking, decoder backpressure, and input attachment."""

    def __init__(self) -> None:
        self.epoch = 0
        self.connected = False
        self.attached = False
        self.source_alive = False
        self.source_revision = 0
        self.announcement: StreamAnnouncement | None = None
        self._last_media_seq = 0
        self._decoder_pending: set[int] = set()
        self._decoder_acked = 0

    @property
    def decoder_pending(self) -> frozenset[int]:
        return frozenset(self._decoder_pending)

    def transport_connected(self, epoch: int) -> None:
        epoch = _positive_integer(epoch, label="connection epoch")
        if epoch <= self.epoch:
            raise ReplayError("connection epoch is stale")
        self.epoch = epoch
        self.connected = True
        self.attached = False
        self._last_media_seq = 0
        self._decoder_pending.clear()
        self._decoder_acked = 0

    def receive_control(self, kind: str,
                        payload: bytes) -> StreamAnnouncement | None:
        if not self.connected:
            raise RemoteAdapterError("viewer control arrived while detached")
        if kind in {"announce", "reconcile"}:
            announcement = StreamAnnouncement.decode(payload)
            if kind == "announce" and self.source_revision:
                raise ReplayError("duplicate source announcement")
            if announcement.source_revision < self.source_revision:
                raise ReplayError("source reconciliation revision is stale")
            if (announcement.source_revision == self.source_revision
                    and self.announcement is not None
                    and announcement != self.announcement):
                raise ReplayError(
                    "source reconciliation changed without a new revision")
            self.source_revision = announcement.source_revision
            self.announcement = announcement
            self.source_alive = True
            self.attached = True
            return announcement
        if kind == "source_closed":
            revision = _decode_source_closed(payload)
            if revision <= self.source_revision:
                raise ReplayError("source close revision is stale")
            self.source_revision = revision
            self.source_alive = False
            self.attached = False
            self.announcement = None
            self._decoder_pending.clear()
            return None
        raise RemoteAdapterError("unsupported nested control kind")

    def receive_media(self, seq: int, payload: bytes) -> DecoderFrame:
        if not self.connected or not self.attached or self.announcement is None:
            raise RemoteAdapterError(
                "viewer media arrived without a live attachment")
        seq = _positive_integer(seq, label="media sequence")
        if seq != self._last_media_seq + 1:
            raise ReplayError("local decoder media sequence is not contiguous")
        if len(self._decoder_pending) >= MAX_LOCAL_DECODE_PENDING:
            raise FlowControlError("local decoder window is full")
        frame = RawFrame.decode(payload)
        if not frame.matches(self.announcement):
            raise RemoteAdapterError(
                "remote frame does not match announced geometry")
        self._last_media_seq = seq
        self._decoder_pending.add(seq)
        return DecoderFrame(seq=seq, frame=frame)

    def decoder_acknowledged(self, seq: int) -> EndpointRequest:
        seq = _positive_integer(seq, label="decoder acknowledgement")
        if (seq <= self._decoder_acked or seq > self._last_media_seq
                or seq not in self._decoder_pending):
            raise FlowControlError(
                "decoder acknowledgement is stale or undelivered")
        self._decoder_acked = seq
        self._decoder_pending = {
            pending for pending in self._decoder_pending if pending > seq}
        return EndpointRequest(
            channel="control", kind="media_ack", ack=seq)

    def send_input(self, kind: str, event: Mapping) -> EndpointRequest:
        if not self.connected or not self.attached or not self.source_alive:
            raise RemoteAdapterError("viewer input is disabled while detached")
        return EndpointRequest(
            channel="input", kind=kind,
            payload=encode_input_event(kind, event))

    def request_close(self, source_revision: int) -> EndpointRequest:
        if not self.connected or not self.attached or not self.source_alive:
            raise RemoteAdapterError("viewer close is disabled while detached")
        source_revision = _positive_integer(
            source_revision, label="source_revision")
        if source_revision != self.source_revision:
            raise ReplayError("viewer close source revision is stale")
        return EndpointRequest(
            channel="control", kind="close_request",
            payload=_source_closed_payload(source_revision))

    def transport_detached(self, epoch: int) -> None:
        if epoch != self.epoch or not self.connected:
            raise ReplayError("detach epoch is stale or not attached")
        self.connected = False
        self.attached = False
        self._decoder_pending.clear()


def _b64decode(value: object, *, label: str) -> bytes:
    if not isinstance(value, str) or "=" in value:
        raise RemoteAdapterError(f"{label} must be unpadded base64url")
    try:
        return base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as exc:
        raise RemoteAdapterError(
            f"{label} must be unpadded base64url") from exc


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


class RemoteNestedController:
    """Multiplex one endpoint socket and one local C-helper socket."""

    def __init__(self, *, role: str, endpoint: socket.socket,
                 helper: socket.socket):
        if role not in {"source", "viewer"}:
            raise RemoteAdapterError("nested controller role is invalid")
        if not isinstance(endpoint, socket.socket) or not isinstance(helper, socket.socket):
            raise RemoteAdapterError("nested controller sockets are invalid")
        self.role = role
        self.endpoint = endpoint
        self.helper = helper
        self.state = (SourceNestedState() if role == "source"
                      else ViewerNestedState())
        self._media_sent = 0
        self._media_acked = 0
        self._pending_frame: RawFrame | None = None
        self._closing_after_send = False

    def run(self) -> None:
        try:
            try:
                while True:
                    readable, _, _ = select.select(
                        [self.endpoint, self.helper], [], [])
                    if self.endpoint in readable:
                        payload = recv_frame(
                            self.endpoint, maximum=MAX_LOCAL_FRAME_BYTES)
                        if payload is None:
                            return
                        self._handle_endpoint(payload)
                    if self.helper in readable:
                        payload = recv_frame(
                            self.helper, maximum=MAX_BOUNDARY_FRAME_BYTES)
                        if payload is None:
                            return
                        self._handle_helper(LocalBoundaryMessage.decode(payload))
            except _ControllerShutdown:
                return
        finally:
            self.endpoint.close()
            self.helper.close()

    def _send_endpoint(self, request: EndpointRequest) -> None:
        doc = {
            "op": "send", "channel": request.channel,
            "kind": request.kind, "ack": request.ack,
            "payload": _b64encode(request.payload),
        }
        send_frame(
            self.endpoint,
            json.dumps(doc, separators=(",", ":")).encode("ascii"),
            maximum=MAX_LOCAL_FRAME_BYTES)

    def _send_helper(self, message: LocalBoundaryMessage) -> None:
        send_frame(
            self.helper, message.encode(), maximum=MAX_BOUNDARY_FRAME_BYTES)

    def _drop_transport(self, message: LocalBoundaryMessage) -> bool:
        if message.kind != "drop_transport":
            return False
        if message.seq or message.payload:
            raise RemoteAdapterError(
                "drop_transport helper message must be empty")
        send_frame(
            self.endpoint, b'{"op":"drop_transport"}',
            maximum=MAX_LOCAL_FRAME_BYTES)
        return True

    def _handle_endpoint(self, payload: bytes) -> None:
        try:
            event = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RemoteAdapterError("endpoint event is not JSON") from exc
        if not isinstance(event, Mapping) or not isinstance(event.get("op"), str):
            raise RemoteAdapterError("endpoint event is invalid")
        op = event["op"]
        if op == "connected":
            epoch = _positive_integer(event.get("epoch"), label="connection epoch")
            self.state.transport_connected(epoch)
            self._media_sent = self._media_acked = 0
            self._pending_frame = None
            self._send_helper(LocalBoundaryMessage("connected", seq=epoch))
            if (self.role == "source"
                    and isinstance(self.state, SourceNestedState)
                    and self.state.announcement is not None):
                self._send_endpoint(self.state.reconcile())
            return
        if op == "detached":
            epoch = _positive_integer(event.get("epoch"), label="detach epoch")
            releases = event.get("releases")
            if not isinstance(releases, list):
                raise RemoteAdapterError("endpoint detach releases are invalid")
            if self.role == "source":
                assert isinstance(self.state, SourceNestedState)
                for injection in self.state.transport_detached(epoch, releases):
                    self._send_helper(LocalBoundaryMessage(
                        injection.kind,
                        payload=encode_local_input_event(
                            injection.kind, injection.event)))
            else:
                assert isinstance(self.state, ViewerNestedState)
                if releases:
                    raise RemoteAdapterError(
                        "viewer endpoint must not report held input")
                self.state.transport_detached(epoch)
            self._media_sent = self._media_acked = 0
            self._pending_frame = None
            self._send_helper(LocalBoundaryMessage("detached", seq=epoch))
            return
        if op == "sent":
            if set(event) != {"op", "channel", "seq"}:
                raise RemoteAdapterError("endpoint sent event fields are invalid")
            if self._closing_after_send and event["channel"] == "control":
                raise _ControllerShutdown
            return
        if op in {"transport_drop_requested"}:
            return
        if op == "error":
            raise RemoteAdapterError(
                f"remote endpoint failed: {event.get('message', '')}")
        if op != "received":
            raise RemoteAdapterError("unsupported endpoint event")
        required = {"op", "channel", "kind", "seq", "ack", "payload"}
        if set(event) != required:
            raise RemoteAdapterError("endpoint received event fields are invalid")
        channel = event["channel"]
        kind = event["kind"]
        message_payload = _b64decode(event["payload"], label="endpoint payload")
        if self.role == "source":
            self._handle_source_received(
                channel, kind, event["seq"], event["ack"], message_payload)
        else:
            self._handle_viewer_received(
                channel, kind, event["seq"], event["ack"], message_payload)

    def _handle_source_received(self, channel: object, kind: object,
                                seq: object, ack: object,
                                payload: bytes) -> None:
        assert isinstance(self.state, SourceNestedState)
        _positive_integer(seq, label="received sequence")
        if channel == "input" and kind in INPUT_KINDS:
            injection = self.state.receive_input(kind, payload)
            self._send_helper(LocalBoundaryMessage(
                injection.kind,
                payload=encode_local_input_event(
                    injection.kind, injection.event)))
            return
        if channel == "control" and kind == "close_request":
            revision = self.state.receive_close_request(
                _decode_source_closed(payload))
            self._send_helper(LocalBoundaryMessage(
                "close_request", seq=revision))
            return
        if channel == "control" and kind == "media_ack":
            ack = _positive_integer(ack, label="media acknowledgement")
            if ack <= self._media_acked or ack > self._media_sent:
                raise FlowControlError(
                    "source controller media acknowledgement is invalid")
            self._media_acked = ack
            self._send_helper(LocalBoundaryMessage("media_ack", seq=ack))
            self._flush_pending_frame()
            return
        raise RemoteAdapterError("source received unsupported remote message")

    def _handle_viewer_received(self, channel: object, kind: object,
                                seq: object, ack: object,
                                payload: bytes) -> None:
        del ack
        assert isinstance(self.state, ViewerNestedState)
        if channel == "control" and kind in {
                "announce", "reconcile", "source_closed"}:
            announcement = self.state.receive_control(kind, payload)
            if announcement is None:
                self._send_helper(LocalBoundaryMessage(
                    "source_closed", seq=self.state.source_revision))
            else:
                self._send_helper(LocalBoundaryMessage(
                    "announce", seq=announcement.source_revision,
                    payload=encode_local_announcement(announcement)))
            return
        if channel == "media" and kind == "frame_bgrx":
            frame = self.state.receive_media(seq, payload)
            self._send_helper(LocalBoundaryMessage(
                "frame", seq=frame.seq, payload=frame.frame.encode()))
            return
        raise RemoteAdapterError("viewer received unsupported remote message")

    def _handle_helper(self, message: LocalBoundaryMessage) -> None:
        if message.kind == "shutdown":
            if message.seq or message.payload:
                raise RemoteAdapterError(
                    "shutdown helper message must be empty")
            raise _ControllerShutdown
        if self.role == "source":
            self._handle_source_helper(message)
        else:
            self._handle_viewer_helper(message)

    def _handle_source_helper(self, message: LocalBoundaryMessage) -> None:
        assert isinstance(self.state, SourceNestedState)
        if self._drop_transport(message):
            return
        if message.kind == "announce":
            self._send_endpoint(self.state.announce(
                decode_local_announcement(message.payload)))
            return
        if message.kind == "frame":
            frame = RawFrame.decode(message.payload)
            if not self.state.connected:
                # A frame already committed to the local socket can race the
                # endpoint's detach notification. It belongs to the expired
                # media epoch and must not revive or fail the detached source.
                return
            if self._media_sent - self._media_acked >= MAX_LOCAL_DECODE_PENDING:
                # Producer never blocks the app/compositor: retain only the
                # newest complete frame until the decoder advances its window.
                self._pending_frame = frame
            else:
                self._send_source_frame(frame)
            return
        if message.kind == "source_closed":
            if message.payload:
                raise RemoteAdapterError("source_closed helper payload must be empty")
            self._send_endpoint(self.state.source_closed(message.seq))
            self._closing_after_send = True
            return
        raise RemoteAdapterError("source helper message is not allowed")

    def _send_source_frame(self, frame: RawFrame) -> None:
        assert isinstance(self.state, SourceNestedState)
        self._send_endpoint(self.state.capture_frame(frame))
        self._media_sent += 1

    def _flush_pending_frame(self) -> None:
        if (self._pending_frame is not None
                and self._media_sent - self._media_acked
                < MAX_LOCAL_DECODE_PENDING):
            frame = self._pending_frame
            self._pending_frame = None
            self._send_source_frame(frame)

    def _handle_viewer_helper(self, message: LocalBoundaryMessage) -> None:
        assert isinstance(self.state, ViewerNestedState)
        if self._drop_transport(message):
            return
        if message.kind == "decoder_ack":
            if message.payload:
                raise RemoteAdapterError("decoder_ack helper payload must be empty")
            self._send_endpoint(self.state.decoder_acknowledged(message.seq))
            return
        if message.kind == "close_request":
            if message.payload:
                raise RemoteAdapterError(
                    "close_request helper payload must be empty")
            self._send_endpoint(self.state.request_close(message.seq))
            return
        if message.kind in INPUT_KINDS:
            event = decode_local_input_event(message.kind, message.payload)
            self._send_endpoint(self.state.send_input(message.kind, event))
            return
        raise RemoteAdapterError("viewer helper message is not allowed")


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - subprocess
    import argparse

    ap = argparse.ArgumentParser(prog="qdistro-mm-remote-nested-controller")
    ap.add_argument("--role", choices=("source", "viewer"), required=True)
    ap.add_argument("--endpoint-fd", type=int, required=True)
    ap.add_argument("--helper-fd", type=int, required=True)
    args = ap.parse_args(argv)
    controller = RemoteNestedController(
        role=args.role,
        endpoint=socket.socket(fileno=args.endpoint_fd),
        helper=socket.socket(fileno=args.helper_fd))
    controller.run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
