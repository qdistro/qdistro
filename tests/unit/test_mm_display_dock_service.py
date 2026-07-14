"""Durable, authenticated R9 display dock-service control tests."""
from __future__ import annotations

import json
import os
import socket
import threading
from pathlib import Path

import pytest

from multimachine.display_dock_service import (
    REQUEST_SCHEMA,
    DisplayDockServiceCore,
    DockServiceError,
    SecureStatusStore,
    encode_secret,
    serve_control_socket,
)
from multimachine.display_dock_session import DockSessionStatus


SECRET = bytes(range(32))


class FakeOwner:
    def __init__(self) -> None:
        self.phase = "disabled"
        self.generation = 0
        self.session_id = None
        self.events: list[str] = []
        self.poll_changed = False
        self.reset_safe = True

    def status(self) -> DockSessionStatus:
        return DockSessionStatus(
            slot_name="rdp-0", phase=self.phase,
            generation=self.generation, session_id=self.session_id,
            next_heartbeat=None)

    def attach(self, payload) -> None:
        self.events.append("attach")
        self.phase = "active"
        self.generation = payload["generation"]
        self.session_id = payload["session_id"]

    def detach(self, generation: int) -> None:
        if generation != self.generation:
            raise RuntimeError("stale generation")
        self.events.append("detach")
        self.phase = "disabled"
        self.session_id = None

    def reset_failed_safe(self) -> None:
        self.events.append("reset")
        if not self.reset_safe:
            raise RuntimeError("unsafe endpoints")
        self.phase = "disabled"
        self.session_id = None

    def restore_last_generation(self, generation: int) -> None:
        self.events.append("restore-generation")
        self.generation = max(self.generation, generation)

    def poll(self) -> bool:
        changed = self.poll_changed
        self.poll_changed = False
        return changed

    def shutdown(self) -> None:
        self.events.append("shutdown")
        if self.phase == "active":
            self.phase = "disabled"
            self.session_id = None


def status_store(tmp_path: Path) -> SecureStatusStore:
    tmp_path.chmod(0o700)
    return SecureStatusStore(tmp_path / "dock-status.json")


def verifier(receipt, session_id, secret):
    assert receipt == {"signed": "receipt"}
    assert secret == SECRET
    return {
        "primary_machine": "laptop",
        "peer_machine": "workstation",
        "trust_domain_id": "owner-machines",
        "generation": 90,
        "session_id": session_id,
        "slot_name": "rdp-0",
        "logical_x": 1280,
        "logical_y": 0,
        "width": 1280,
        "height": 800,
        "scale": 1,
        "allow_input": True,
        "lease_expires_at": 1000,
        "heartbeat_ms": 1000,
    }


def core(tmp_path: Path, owner: FakeOwner | None = None,
         recover=lambda _record: True) -> DisplayDockServiceCore:
    return DisplayDockServiceCore(
        owner=owner or FakeOwner(), controller_uid=os.geteuid(),
        verify_attach=verifier, store=status_store(tmp_path), recover=recover,
        clock=lambda: 100.0)


def request(command: str, suffix: str = "0", **fields) -> dict:
    return {
        "schema": REQUEST_SCHEMA,
        "request_id": suffix * 32,
        "command": command,
        **fields,
    }


def test_attach_is_verified_and_persists_only_public_status(tmp_path: Path) -> None:
    service = core(tmp_path)
    service.start()
    response = service.handle(request(
        "attach", receipt={"signed": "receipt"}, session_id="dock-90",
        carrier_secret=encode_secret(SECRET)), peer_uid=os.geteuid())
    assert response["ok"] is True
    assert response["status"]["phase"] == "active"
    persisted = (tmp_path / "dock-status.json").read_text(encoding="utf-8")
    assert "carrier_secret" not in persisted
    assert "signed" not in persisted
    assert "dock-90" in persisted
    assert (tmp_path / "dock-status.json").stat().st_mode & 0o777 == 0o600


def test_controller_uid_exact_fields_and_replay_fail_closed(tmp_path: Path) -> None:
    service = core(tmp_path)
    service.start()
    status = request("status")
    assert service.handle(status, peer_uid=os.geteuid() + 1) == {
        "ok": False, "error": "unauthorized-controller"}
    assert service.handle(status, peer_uid=os.geteuid())["ok"] is True
    replay = service.handle(status, peer_uid=os.geteuid())
    assert replay["ok"] is False and "replayed" in replay["error"]
    malformed = request("detach", suffix="1", generation=1, extra=True)
    denied = service.handle(malformed, peer_uid=os.geteuid())
    assert denied["ok"] is False and "fields" in denied["error"]


def test_failed_attach_does_not_persist_a_false_active_recovery_record(
        tmp_path: Path) -> None:
    service = DisplayDockServiceCore(
        owner=FakeOwner(), controller_uid=os.geteuid(),
        verify_attach=lambda *_args: (_ for _ in ()).throw(
            RuntimeError("verification failed")),
        store=status_store(tmp_path), recover=lambda _record: True,
        clock=lambda: 100.0)
    service.start()
    response = service.handle(request(
        "attach", receipt={"signed": "receipt"}, session_id="dock-90",
        carrier_secret=encode_secret(SECRET)), peer_uid=os.geteuid())
    assert response["ok"] is False
    persisted = service.store.load()
    assert persisted is not None
    assert persisted["phase"] == "disabled"
    assert persisted["recovery_grant"] is None


def test_startup_requires_explicit_recovery_of_non_disabled_record(
        tmp_path: Path) -> None:
    store = status_store(tmp_path)
    store.save({
        "schema": "qdistro-mm-display-dock-status-v1",
        "slot_name": "rdp-0", "phase": "active", "generation": 89,
        "session_id": "old", "next_heartbeat": 1.0,
        "accepting": True, "stop_requested": False, "last_error": None,
        "updated_at": 90.0,
        "recovery_grant": {
            "primary_machine": "laptop", "peer_machine": "workstation",
            "trust_domain_id": "owner-machines", "generation": 89,
            "session_id": "old", "slot_name": "rdp-0",
            "logical_x": 1280, "logical_y": 0, "width": 1280,
            "height": 800, "scale": 1, "allow_input": True,
            "lease_expires_at": 1000, "heartbeat_ms": 1000,
        },
    })
    recovered: list[dict] = []
    service = core(tmp_path, recover=lambda record: (
        recovered.append(dict(record)) or True))
    service.start()
    assert recovered[0]["generation"] == 89
    assert service.public_status()["accepting"] is True

    # Recreate the interrupted record and prove an unconfirmed cleanup never
    # opens the controller socket for commands.
    store.save(recovered[0])
    blocked = core(tmp_path, recover=lambda _record: False)
    with pytest.raises(DockServiceError, match="not confirmed"):
        blocked.start()
    assert blocked.public_status()["accepting"] is False


def test_shutdown_converges_failed_safe_only_after_safe_reset(
        tmp_path: Path) -> None:
    owner = FakeOwner()
    service = core(tmp_path, owner=owner)
    service.start()
    owner.phase = "failed-safe"
    service.converge_shutdown()
    assert owner.events[-2:] == ["shutdown", "reset"]
    assert owner.phase == "disabled"

    owner.phase = "failed-safe"
    owner.reset_safe = False
    with pytest.raises(RuntimeError, match="unsafe endpoints"):
        service.converge_shutdown()
    assert service.public_status()["last_error"] == "RuntimeError"


def test_socket_server_uses_peer_credentials_and_shutdown_command(
        tmp_path: Path) -> None:
    owner = FakeOwner()
    service = core(tmp_path, owner=owner)
    path = tmp_path / "dock.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(4)
    stop = threading.Event()
    worker = threading.Thread(
        target=serve_control_socket, args=(service, listener, stop), daemon=True)
    worker.start()
    for _attempt in range(100):
        if service.public_status()["accepting"]:
            break
        stop.wait(0.01)

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(path))
        client.sendall((json.dumps(request("shutdown", suffix="f")) + "\n").encode())
        response = json.loads(client.makefile("r", encoding="utf-8").readline())
    worker.join(timeout=2)
    listener.close()
    assert response["ok"] is True
    assert not worker.is_alive()
    assert owner.status().phase == "disabled"


def test_status_store_rejects_symlink(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    target = tmp_path / "target"
    target.write_text("{}", encoding="utf-8")
    (tmp_path / "dock-status.json").symlink_to(target)
    with pytest.raises(DockServiceError, match="unsafe"):
        status_store(tmp_path).load()
