"""Exact role-separated Unix adapters for the installed dock owner."""
from __future__ import annotations

import json
import socket
import threading
from pathlib import Path

import pytest

from multimachine.display_dock_rpc import (
    JsonEndpointClient,
    RpcCarrierFactory,
    RpcPanelEndpoint,
    RpcPrimarySafetyEndpoint,
)
from multimachine.remote_display_slot import ActionKind, HeldInput, SlotAction


def grant() -> dict:
    return {
        "generation": 90,
        "session_id": "dock-90",
        "slot_name": "rdp-0",
    }


class EndpointServer:
    def __init__(self, path: Path, results: dict[str, str]):
        self.path = path
        self.results = results
        self.requests: list[dict] = []
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(path))
        self.listener.listen(8)
        self.stop = threading.Event()
        self.worker = threading.Thread(target=self._serve, daemon=True)
        self.worker.start()

    def _serve(self) -> None:
        self.listener.settimeout(0.05)
        while not self.stop.is_set():
            try:
                client, _address = self.listener.accept()
            except TimeoutError:
                continue
            with client:
                request = json.loads(
                    client.makefile("r", encoding="utf-8").readline())
                self.requests.append(request)
                result = self.results.get(request["action"], "unexpected")
                client.sendall((json.dumps({
                    "ok": result != "unexpected", "result": result,
                }) + "\n").encode())

    def close(self) -> None:
        self.stop.set()
        self.worker.join(timeout=1)
        self.listener.close()


def client(path: Path, role: str) -> JsonEndpointClient:
    return JsonEndpointClient(
        str(path), role=role, path_prefix=str(path.parent) + "/")


def test_role_adapters_emit_only_fixed_actions_and_exact_identity(
        tmp_path: Path) -> None:
    server = EndpointServer(tmp_path / "endpoint.sock", {
        "synthesize-releases": "released",
        "clear-transfers": "cleared",
        "peer-blank-panel": "reserved",
        "heartbeat": "renewed",
        "peer-unblank-panel": "restored",
        "safe": "safe",
        "open": "ready",
        "alive": "alive",
        "close": "closed",
    })
    try:
        safety = RpcPrimarySafetyEndpoint(
            client(server.path, "primary-safety"))
        safety.perform(SlotAction(
            ActionKind.SYNTHESIZE_RELEASES,
            (HeldInput("key", 30),)), grant())
        safety.perform(SlotAction(ActionKind.CLEAR_TRANSFERS), grant())
        assert safety.safe_state_confirmed("rdp-0")

        panel = RpcPanelEndpoint(client(server.path, "peer-panel"))
        panel.perform(SlotAction(ActionKind.PEER_BLANK_PANEL), grant())
        panel.heartbeat(90, grant())
        panel.perform(SlotAction(ActionKind.PEER_UNBLANK_PANEL), grant())
        assert panel.safe_state_confirmed("rdp-0")

        carrier = RpcCarrierFactory(client(server.path, "carrier"))
        session = carrier.start(grant())
        assert session.ready() and session.alive()
        session.close()
        assert carrier.safe("rdp-0")
    finally:
        server.close()

    first = server.requests[0]
    assert set(first) == {
        "schema", "role", "action", "generation", "session_id",
        "slot_name", "releases",
    }
    assert first["role"] == "primary-safety"
    assert first["releases"] == [{"kind": "key", "code": 30}]
    assert all(request["generation"] == 90 for request in server.requests)
    assert all(request["session_id"] == "dock-90"
               for request in server.requests)


def test_wrong_role_and_action_fail_before_broadening_authority(
        tmp_path: Path) -> None:
    server = EndpointServer(tmp_path / "endpoint.sock", {})
    try:
        with pytest.raises(ValueError, match="wrong endpoint role"):
            RpcPanelEndpoint(client(server.path, "carrier"))
        safety = RpcPrimarySafetyEndpoint(
            client(server.path, "primary-safety"))
        with pytest.raises(Exception, match="input/output action"):
            safety.perform(
                SlotAction(ActionKind.PRIMARY_ENABLE_INPUT), grant())
    finally:
        server.close()
    assert server.requests == []
