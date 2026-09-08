"""Installed daemon shape and fail-closed startup helper tests."""
from __future__ import annotations

import os
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

import multimachine.mm_display_dock_daemon as daemon
from multimachine.display_dock_service import DockServiceError


REPO = Path(__file__).resolve().parents[2]
UNIT = REPO / "multimachine/qdistro-mm-display-dock.service"


def test_systemd_unit_has_fixed_owner_state_and_no_direct_network() -> None:
    text = UNIT.read_text(encoding="utf-8")
    assert "User=admin" in text
    assert "RuntimeDirectory=qdistro-mm-display-dock" in text
    assert "StateDirectory=qdistro-mm-display-dock" in text
    assert "ConditionPathExists=/etc/qdistro/multimachine/display-dock.conf" in text
    assert "RestrictAddressFamilies=AF_UNIX AF_NETLINK" in text
    assert "AF_INET" not in text
    assert "--local-machine-id ${QD_MM_MACHINE_ID}" in text


def test_recovery_grant_uses_durable_projection_or_safe_baseline() -> None:
    baseline = daemon.recovery_grant(None, slot_name="rdp-0")
    assert baseline["allow_input"] is False
    assert baseline["slot_name"] == "rdp-0"
    assert baseline["lease_expires_at"] > int(daemon.time.time())
    durable = {**baseline, "generation": 90, "session_id": "dock-90"}
    assert daemon.recovery_grant({
        "generation": 90, "recovery_grant": durable,
    }, slot_name="rdp-0") == durable


class _RecoveryClient:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error

    def request(self, action, grant):
        assert action == "recover"
        assert grant["slot_name"] == "rdp-0"
        if self.error is not None:
            raise self.error
        return self.result


def test_absent_fixed_endpoint_is_safe_only_for_disabled_baseline() -> None:
    grant = daemon.recovery_grant(None, slot_name="rdp-0")
    missing = _RecoveryClient(error=FileNotFoundError())
    refused = _RecoveryClient(error=ConnectionRefusedError())
    assert daemon.recover_fixed_endpoint(
        missing, grant, allow_absent=True) is True
    assert daemon.recover_fixed_endpoint(
        refused, grant, allow_absent=True) is True
    assert daemon.recover_fixed_endpoint(
        missing, grant, allow_absent=False) is False
    assert daemon.recover_fixed_endpoint(
        _RecoveryClient(result="unsafe"), grant,
        allow_absent=True) is False


def test_control_listener_replaces_only_owned_stale_socket(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    path = tmp_path / "control.sock"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path))
    stale.close()
    listener = daemon.prepare_control_listener(
        str(path), controller_uid=os.geteuid())
    try:
        assert path.stat().st_mode & 0o777 == 0o600
    finally:
        listener.close()
        path.unlink()

    path.write_text("not a socket", encoding="utf-8")
    with pytest.raises(DockServiceError, match="control path is unsafe"):
        daemon.prepare_control_listener(str(path), controller_uid=0)


def test_shell_pid_is_taken_only_from_fixed_user_unit(monkeypatch) -> None:
    monkeypatch.setattr(daemon.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout="4242\n"))
    assert daemon.resolve_shell_pid() == 4242
    with pytest.raises(ValueError, match="must be qdshell"):
        daemon.resolve_shell_pid("attacker.service")

    monkeypatch.setattr(daemon.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=1, stdout="0\n"))
    with pytest.raises(DockServiceError, match="not running"):
        daemon.resolve_shell_pid()
