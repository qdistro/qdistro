"""Peer-local display panel lease and control-protocol tests."""
from __future__ import annotations

import json
import socket
import struct
import threading
import time

import pytest

from multimachine.display_panel_agent import (
    SCHEMA,
    PanelActions,
    PanelControlError,
    PanelLeaseSpec,
    PanelLeaseState,
    PanelPeerProtocol,
    PanelPrimaryClient,
    serve_authenticated_panel,
)


class Clock:
    def __init__(self, value: float = 1000):
        self.value = value

    def __call__(self) -> float:
        return self.value


class Actions(PanelActions):
    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.is_safe = True

    def blank(self, slot_name: str) -> None:
        self.calls.append(("blank", slot_name))
        self.is_safe = False

    def restore(self, slot_name: str) -> None:
        self.calls.append(("restore", slot_name))
        self.is_safe = True

    def safe(self, slot_name: str) -> bool:
        assert slot_name == "rdp-0"
        return self.is_safe


def payload() -> dict:
    return {
        "primary_machine": "primary", "peer_machine": "peer",
        "trust_domain_id": "owner-machines", "generation": 90,
        "session_id": "display-session", "slot_name": "rdp-0",
        "logical_x": 1280, "logical_y": 0, "width": 1280, "height": 800,
        "scale": 1, "allow_input": True,
        "lease_expires_at": 5000, "heartbeat_ms": 1000,
    }


def request(seq: int, kind: str, **changes) -> bytes:
    value = {
        "schema": SCHEMA, "seq": seq, "kind": kind,
        "session_id": "display-session", "generation": 90,
        "slot_name": "rdp-0",
    }
    value.update(changes)
    return json.dumps(value).encode()


def test_verified_grant_becomes_peer_local_lease() -> None:
    spec = PanelLeaseSpec.from_verified_payload(payload())
    assert spec == PanelLeaseSpec(
        session_id="display-session", generation=90, slot_name="rdp-0",
        heartbeat_ms=1000, lease_expires_at=5000)


def test_panel_lease_expires_locally_and_cannot_be_resurrected() -> None:
    clock = Clock()
    actions = Actions()
    state = PanelLeaseState(
        PanelLeaseSpec.from_verified_payload(payload()), actions, clock=clock)
    assert state.reserve() == "reserved"
    clock.value += 0.9
    assert state.heartbeat() == "renewed"
    clock.value += 1.01
    assert state.tick()
    assert state.status() == "safe"
    assert actions.calls == [("blank", "rdp-0"), ("restore", "rdp-0")]
    with pytest.raises(PanelControlError, match="already closed"):
        state.reserve()
    with pytest.raises(PanelControlError, match="not reserved"):
        state.heartbeat()


def test_protocol_rejects_stale_generation_schema_drift_and_replay() -> None:
    actions = Actions()
    protocol = PanelPeerProtocol(PanelLeaseState(
        PanelLeaseSpec.from_verified_payload(payload()), actions,
        clock=Clock()))
    response = json.loads(protocol.handle(request(1, "reserve")))
    assert response == {
        "schema": SCHEMA, "seq": 1, "kind": "reserve",
        "result": "reserved",
    }
    with pytest.raises(PanelControlError, match="sequence"):
        protocol.handle(request(1, "heartbeat"))

    another = PanelPeerProtocol(PanelLeaseState(
        PanelLeaseSpec.from_verified_payload(payload()), Actions(),
        clock=Clock()))
    with pytest.raises(PanelControlError, match="another grant"):
        another.handle(request(1, "reserve", generation=89))
    malformed = json.loads(request(1, "reserve"))
    malformed["outputs"] = []
    with pytest.raises(PanelControlError, match="fields"):
        another.handle(json.dumps(malformed).encode())


def test_authenticated_socket_round_trip_and_disconnect_restore() -> None:
    peer, primary = socket.socketpair()
    actions = Actions()
    spec = PanelLeaseSpec.from_verified_payload(payload())
    state = PanelLeaseState(spec, actions, clock=Clock())
    worker = threading.Thread(
        target=serve_authenticated_panel, args=(peer, state), daemon=True)
    worker.start()
    client = PanelPrimaryClient(primary, spec)
    assert client.request("reserve") == "reserved"
    assert client.request("heartbeat") == "renewed"
    assert client.request("status") == "reserved"
    primary.close()
    worker.join(timeout=1)
    assert not worker.is_alive()
    assert state.status() == "safe"
    assert actions.calls[-1] == ("restore", "rdp-0")


def test_partial_frame_cannot_block_independent_expiry() -> None:
    peer, primary = socket.socketpair()
    actions = Actions()
    spec = PanelLeaseSpec(
        session_id="display-session", generation=90, slot_name="rdp-0",
        heartbeat_ms=50, lease_expires_at=int(time.time()) + 60)
    state = PanelLeaseState(spec, actions)
    state.reserve()
    worker = threading.Thread(
        target=serve_authenticated_panel, args=(peer, state), daemon=True)
    worker.start()
    # Announce a body but never send it. The incremental reader must keep
    # polling the lease rather than blocking in a recv_exact call.
    primary.sendall(struct.pack("!I", 100))
    deadline = time.monotonic() + 1
    while state.reserved and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not state.reserved
    assert actions.calls[-1] == ("restore", "rdp-0")
    primary.close()
    worker.join(timeout=1)


def test_release_is_terminal_and_safe_status_does_not_renew() -> None:
    peer, primary = socket.socketpair()
    actions = Actions()
    spec = PanelLeaseSpec.from_verified_payload(payload())
    worker = threading.Thread(
        target=serve_authenticated_panel,
        args=(peer, PanelLeaseState(spec, actions, clock=Clock())),
        daemon=True)
    worker.start()
    client = PanelPrimaryClient(primary, spec)
    assert client.request("reserve") == "reserved"
    assert client.request("release") == "released"
    assert client.request("status") == "safe"
    primary.close()
    worker.join(timeout=1)
    assert actions.calls == [("blank", "rdp-0"), ("restore", "rdp-0")]
