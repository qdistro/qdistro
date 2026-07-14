"""Authenticated peer-local panel lease for one RDP display generation.

The RDP carrier is intentionally opaque and one-shot, so panel ownership uses
its own long-lived control connection. Both ends reuse the display grant's
exact TLS certificate pins and separately delivered secret proof. The peer is
the enforcement point: it blanks/reserves locally, expires without a primary,
and restores its local desktop on timeout, malformed control, or disconnect.
"""
from __future__ import annotations

import json
import select
import socket
import struct
import time
from dataclasses import dataclass
from typing import Callable, Mapping, Protocol

from .display_carrier import (
    DisplayCarrierIdentity,
    wrap_authenticated_tls,
)
from .remote_display_slot import ActiveDisplayGrant


SCHEMA = "qdistro-mm-panel-control-v1"
MAX_FRAME_BYTES = 4096
_REQUEST_FIELDS = frozenset({
    "schema", "seq", "kind", "session_id", "generation", "slot_name",
})
_KINDS = frozenset({"reserve", "heartbeat", "release", "status"})


class PanelControlError(RuntimeError):
    """Panel authority, sequencing, framing, or local action failed."""


class PanelActions(Protocol):
    def blank(self, slot_name: str) -> None: ...
    def restore(self, slot_name: str) -> None: ...
    def safe(self, slot_name: str) -> bool: ...


@dataclass(frozen=True)
class PanelLeaseSpec:
    session_id: str
    generation: int
    slot_name: str
    heartbeat_ms: int
    lease_expires_at: int

    @classmethod
    def from_verified_payload(cls, payload: Mapping) -> "PanelLeaseSpec":
        # Reuse the display state machine's strict grant field validation, then
        # retain only facts the peer-local lease enforcement needs.
        grant = ActiveDisplayGrant.from_verified_payload(payload)
        return cls(
            session_id=grant.session_id, generation=grant.generation,
            slot_name=grant.slot_name, heartbeat_ms=grant.heartbeat_ms,
            lease_expires_at=grant.lease_expires_at)


class PanelLeaseState:
    """Pure peer-local lease state with independent fail-safe restoration."""

    def __init__(self, spec: PanelLeaseSpec, actions: PanelActions, *,
                 clock: Callable[[], float] = time.time):
        self.spec = spec
        self.actions = actions
        self.clock = clock
        self.reserved = False
        self.deadline: float | None = None
        self.terminal = False

    def _expired(self) -> bool:
        now = self.clock()
        return (now >= self.spec.lease_expires_at
                or (self.deadline is not None and now > self.deadline))

    def _renew_deadline(self) -> None:
        self.deadline = min(
            self.clock() + self.spec.heartbeat_ms / 1000.0,
            float(self.spec.lease_expires_at))

    def reserve(self) -> str:
        if self.terminal:
            raise PanelControlError("panel generation is already closed")
        if self.clock() >= self.spec.lease_expires_at:
            raise PanelControlError("panel grant lease has expired")
        if not self.reserved:
            self.actions.blank(self.spec.slot_name)
            self.reserved = True
        self._renew_deadline()
        return "reserved"

    def heartbeat(self) -> str:
        if self.terminal or not self.reserved:
            raise PanelControlError("panel is not reserved")
        if self._expired():
            self.expire()
            raise PanelControlError("panel lease heartbeat arrived too late")
        self._renew_deadline()
        return "renewed"

    def release(self) -> str:
        if self.reserved:
            self.actions.restore(self.spec.slot_name)
        self.reserved = False
        self.deadline = None
        self.terminal = True
        return "released"

    def expire(self) -> bool:
        if not self.reserved:
            return False
        self.actions.restore(self.spec.slot_name)
        self.reserved = False
        self.deadline = None
        self.terminal = True
        return True

    def tick(self) -> bool:
        return self.expire() if self.reserved and self._expired() else False

    def status(self) -> str:
        self.tick()
        if self.reserved:
            return "reserved"
        return "safe" if self.actions.safe(self.spec.slot_name) else "unsafe"

    def poll_timeout(self) -> float:
        if not self.reserved or self.deadline is None:
            return 1.0
        return max(0.0, min(1.0, self.deadline - self.clock(),
                            self.spec.lease_expires_at - self.clock()))


def _canonical(value: Mapping) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("ascii")


def _frame(payload: bytes) -> bytes:
    if not payload or len(payload) > MAX_FRAME_BYTES:
        raise PanelControlError("panel control frame size is invalid")
    return struct.pack("!I", len(payload)) + payload


class _FrameReader:
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, payload: bytes) -> list[bytes]:
        self.buffer.extend(payload)
        frames: list[bytes] = []
        while len(self.buffer) >= 4:
            size = struct.unpack("!I", self.buffer[:4])[0]
            if size == 0 or size > MAX_FRAME_BYTES:
                raise PanelControlError("panel control frame size is invalid")
            if len(self.buffer) < 4 + size:
                break
            frames.append(bytes(self.buffer[4:4 + size]))
            del self.buffer[:4 + size]
        return frames


class PanelPeerProtocol:
    """Validate one authenticated primary's strictly ordered requests."""

    def __init__(self, state: PanelLeaseState):
        self.state = state
        self.last_seq = 0

    def handle(self, payload: bytes) -> bytes:
        try:
            request = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PanelControlError("panel request is not JSON") from exc
        if not isinstance(request, Mapping) or set(request) != _REQUEST_FIELDS:
            raise PanelControlError("panel request fields do not match schema")
        spec = self.state.spec
        if (request["schema"] != SCHEMA
                or request["session_id"] != spec.session_id
                or request["generation"] != spec.generation
                or request["slot_name"] != spec.slot_name):
            raise PanelControlError("panel request targets another grant")
        seq = request["seq"]
        if (not isinstance(seq, int) or isinstance(seq, bool)
                or seq != self.last_seq + 1):
            raise PanelControlError("panel request sequence is invalid")
        kind = request["kind"]
        if kind not in _KINDS:
            raise PanelControlError("panel request kind is invalid")
        self.last_seq = seq
        result = {
            "reserve": self.state.reserve,
            "heartbeat": self.state.heartbeat,
            "release": self.state.release,
            "status": self.state.status,
        }[kind]()
        return _canonical({
            "schema": SCHEMA, "seq": seq, "kind": kind, "result": result,
        })


def serve_authenticated_panel(sock: socket.socket, state: PanelLeaseState) -> None:
    """Serve until EOF/error; every exit path restores a reserved panel."""
    reader = _FrameReader()
    protocol = PanelPeerProtocol(state)
    try:
        while True:
            readable, _, _ = select.select(
                [sock], [], [], state.poll_timeout())
            if not readable:
                state.tick()
                continue
            payload = sock.recv(4096)
            if not payload:
                return
            for request in reader.feed(payload):
                sock.sendall(_frame(protocol.handle(request)))
    finally:
        state.expire()


class PanelPrimaryClient:
    """Synchronous controller-side client over an authenticated TLS socket."""

    def __init__(self, sock: socket.socket, spec: PanelLeaseSpec):
        self.sock = sock
        self.spec = spec
        self.seq = 0
        self.reader = _FrameReader()

    def request(self, kind: str) -> str:
        if kind not in _KINDS:
            raise PanelControlError("panel request kind is invalid")
        self.seq += 1
        request = _canonical({
            "schema": SCHEMA, "seq": self.seq, "kind": kind,
            "session_id": self.spec.session_id,
            "generation": self.spec.generation,
            "slot_name": self.spec.slot_name,
        })
        self.sock.sendall(_frame(request))
        while True:
            payload = self.sock.recv(4096)
            if not payload:
                raise PanelControlError("panel peer disconnected")
            frames = self.reader.feed(payload)
            if not frames:
                continue
            if len(frames) != 1:
                raise PanelControlError("panel peer returned extra responses")
            try:
                response = json.loads(frames[0])
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise PanelControlError("panel response is not JSON") from exc
            expected = {"schema", "seq", "kind", "result"}
            if (not isinstance(response, Mapping) or set(response) != expected
                    or response["schema"] != SCHEMA
                    or response["seq"] != self.seq
                    or response["kind"] != kind
                    or response["result"] not in {
                        "reserved", "renewed", "released", "safe", "unsafe",
                    }):
                raise PanelControlError("panel response is invalid")
            return response["result"]


def accept_primary_control(identity: DisplayCarrierIdentity, *,
                           listener: socket.socket) -> PanelPrimaryClient:
    """Accept and authenticate the peer's outbound control connection."""
    if identity.role != "primary":
        raise PanelControlError("primary panel control needs primary identity")
    raw, _address = listener.accept()
    tls = wrap_authenticated_tls(
        raw, role="primary", binding=identity.binding, secret=identity.secret,
        local_certificate_pem=identity.local_certificate_pem,
        local_private_key_pem=identity.local_private_key_pem,
        peer_certificate_pem=identity.peer_certificate_pem,
        used_peer_nonces=set())
    tls.settimeout(5.0)
    return PanelPrimaryClient(tls, PanelLeaseSpec(
        session_id=identity.binding.session_id,
        generation=identity.binding.generation,
        slot_name=identity.binding.slot_name,
        heartbeat_ms=1,  # Primary requests do not enforce the peer deadline.
        lease_expires_at=identity.binding.lease_expires_at))


def connect_peer_control(identity: DisplayCarrierIdentity, *, host: str,
                         port: int, state: PanelLeaseState) -> None:
    """Connect outward, authenticate the primary, and enforce locally."""
    if identity.role != "peer":
        raise PanelControlError("peer panel control needs peer identity")
    raw = socket.create_connection((host, port), timeout=15)
    tls = wrap_authenticated_tls(
        raw, role="peer", binding=identity.binding, secret=identity.secret,
        local_certificate_pem=identity.local_certificate_pem,
        local_private_key_pem=identity.local_private_key_pem,
        peer_certificate_pem=identity.peer_certificate_pem)
    tls.settimeout(None)
    serve_authenticated_panel(tls, state)
