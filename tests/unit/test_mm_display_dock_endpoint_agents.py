"""Fixed peer-panel and carrier-supervisor agent contract tests."""
from __future__ import annotations

import json
import socket
import time
from types import SimpleNamespace

import pytest

import multimachine.display_carrier_endpoint as carrier_endpoint
from multimachine.display_panel_agent import PanelControlError
from multimachine.display_dock_rpc import (
    DisplayDockRpcError,
    SCHEMA,
    receive_endpoint_request,
)
from multimachine.display_panel_endpoint import PrimaryPanelAgent


def _request(**changes) -> dict:
    value = {
        "schema": SCHEMA,
        "role": "peer-panel",
        "action": "peer-blank-panel",
        "generation": 90,
        "session_id": "dock-90",
        "slot_name": "rdp-0",
        "releases": [],
    }
    value.update(changes)
    return value


def _parse(request: dict) -> str:
    client, server = socket.socketpair()
    try:
        client.sendall((json.dumps(request) + "\n").encode())
        return receive_endpoint_request(
            server, role="peer-panel", generation=90,
            session_id="dock-90", slot_name="rdp-0",
            actions=frozenset({"peer-blank-panel", "safe"}))
    finally:
        client.close()
        server.close()


def test_endpoint_request_is_exact_and_sealed_generation_bound() -> None:
    assert _parse(_request()) == "peer-blank-panel"
    for change in (
        {"role": "carrier"},
        {"generation": 91},
        {"session_id": "dock-91"},
        {"slot_name": "rdp-1"},
        {"action": "open"},
        {"releases": [{"kind": "key", "code": 30}]},
        {"unexpected": True},
    ):
        with pytest.raises(DisplayDockRpcError, match="sealed authority"):
            _parse(_request(**change))


class FakePanel:
    def __init__(self, responses: dict[str, list[str]]):
        self.responses = responses
        self.calls: list[str] = []
        self.closed = False

    def request(self, action: str) -> str:
        self.calls.append(action)
        return self.responses[action].pop(0)

    def close(self) -> None:
        self.closed = True


def test_panel_agent_maps_only_reserved_lease_lifecycle_and_caches_safe() -> None:
    panel = FakePanel({
        "reserve": ["reserved"],
        "heartbeat": ["renewed"],
        "release": ["released"],
        "status": [],
    })
    agent = PrimaryPanelAgent(panel, heartbeat_seconds=8)
    assert agent.perform("peer-blank-panel") == "reserved"
    assert agent.perform("heartbeat") == "renewed"
    assert agent.perform("peer-unblank-panel") == "restored"
    assert agent.perform("safe") == "safe"
    assert agent.perform("recover") == "safe"
    assert panel.calls == ["reserve", "heartbeat", "release"]


def test_panel_agent_recovery_releases_a_reserved_peer() -> None:
    panel = FakePanel({
        "status": ["reserved"],
        "release": ["released"],
    })
    agent = PrimaryPanelAgent(panel, heartbeat_seconds=8)
    assert agent.perform("recover") == "safe"
    assert panel.calls == ["status", "release"]


def test_panel_disconnect_is_safe_only_after_peer_owned_deadline() -> None:
    now = [100.0]
    panel = FakePanel({
        "reserve": ["reserved"],
        "heartbeat": ["renewed"],
    })
    agent = PrimaryPanelAgent(
        panel, heartbeat_seconds=8, clock=lambda: now[0])
    assert agent.perform("peer-blank-panel") == "reserved"
    agent.fail_closed(TimeoutError("stalled peer"))
    assert panel.closed
    assert agent.perform("safe") == "unsafe"
    now[0] = 108.0
    assert agent.perform("safe") == "safe"
    assert agent.perform("peer-unblank-panel") == "restored"


def test_panel_graceful_eof_proves_peer_finally_restored_ownership() -> None:
    panel = FakePanel({"reserve": ["reserved"]})
    agent = PrimaryPanelAgent(panel, heartbeat_seconds=8)
    assert agent.perform("peer-blank-panel") == "reserved"
    assert agent.fail_closed(PanelControlError("panel peer disconnected"))
    assert agent.perform("safe") == "safe"
    assert agent.perform("peer-unblank-panel") == "restored"


def test_carrier_agent_reports_ready_alive_close_and_idempotent_recovery(
        monkeypatch) -> None:
    def fake_serve(_identity, *, listener, backend_unix_path, on_ready,
                   stop_requested):
        assert listener == "listener"
        assert backend_unix_path == "/run/qdistro/rdp.sock"
        on_ready()
        while not stop_requested():
            time.sleep(0.001)

    monkeypatch.setattr(carrier_endpoint, "serve_primary_once", fake_serve)
    runtime = SimpleNamespace(
        identity=object(), target={"backend_unix_path": "/run/qdistro/rdp.sock"})
    agent = carrier_endpoint.PrimaryCarrierAgent(runtime, "listener")

    assert agent.perform("safe") == "safe"
    assert agent.perform("open") == "ready"
    assert agent.perform("alive") == "alive"
    assert agent.perform("close") == "closed"
    assert agent.perform("safe") == "safe"
    assert agent.perform("recover") == "safe"
