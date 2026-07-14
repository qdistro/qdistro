"""Sealed-config service endpoint for one peer-panel lease generation."""
from __future__ import annotations

import argparse
import signal
import socket
import subprocess
import time
from pathlib import Path
from typing import Callable

from .display_panel_agent import (
    PanelActions,
    PanelControlError,
    PanelLeaseState,
    accept_primary_control,
    connect_peer_control,
)
from .display_dock_rpc import (
    DisplayDockRpcError,
    peer_uid,
    prepare_endpoint_listener,
    receive_endpoint_request,
    send_endpoint_response,
)
from .mm_display_panel_launcher import DisplayPanelRuntime, parse_panel_config
from .mm_session_launcher import _read_owned_json_fd


_ACTIONS = frozenset({
    "peer-blank-panel", "heartbeat", "peer-unblank-panel", "safe", "recover",
})


def _exit_on_signal(_signum, _frame) -> None:
    raise SystemExit(0)


class PrimaryPanelAgent:
    """Map the fixed dock role to one authenticated peer-panel channel."""

    def __init__(self, panel, *, heartbeat_seconds: float,
                 clock: Callable[[], float] = time.monotonic):
        self.panel = panel
        self.heartbeat_seconds = heartbeat_seconds
        self.clock = clock
        self._safe = False
        self.deadline: float | None = None
        self.disconnected = False

    def _deadline_safe(self) -> bool:
        if (self.disconnected and self.deadline is not None
                and self.clock() >= self.deadline):
            self._safe = True
        return self._safe

    def fail_closed(self, error: BaseException) -> bool:
        self.panel.close()
        self.disconnected = True
        # The peer emits EOF only after serve_authenticated_panel's finally
        # block restored local ownership. A timeout/reset is not equivalent;
        # it remains unsafe until the peer-owned heartbeat deadline.
        if (isinstance(error, PanelControlError)
                and str(error) == "panel peer disconnected"):
            self._safe = True
        self._deadline_safe()
        return self._safe

    def perform(self, action: str) -> str:
        if action == "peer-blank-panel":
            result = self.panel.request("reserve")
            self._safe = False
            self.disconnected = False
            if result == "reserved":
                self.deadline = self.clock() + self.heartbeat_seconds
            return result
        if action == "heartbeat":
            result = self.panel.request("heartbeat")
            if result == "renewed":
                self.deadline = self.clock() + self.heartbeat_seconds
            return result
        if action == "peer-unblank-panel":
            released = (self._deadline_safe()
                        or self.panel.request("release") == "released")
            self._safe = released
            return "restored" if released else "unsafe"
        if action == "recover":
            if not self._deadline_safe() and not self.disconnected:
                status = self.panel.request("status")
                if status == "reserved":
                    self._safe = self.panel.request("release") == "released"
                else:
                    self._safe = status == "safe"
            return "safe" if self._deadline_safe() else "unsafe"
        if not self._deadline_safe() and not self.disconnected:
            self._safe = self.panel.request("status") == "safe"
        return "safe" if self._deadline_safe() else "unsafe"


def _systemctl(action: str, unit: str) -> int:
    return subprocess.run(
        ["systemctl", action, unit], check=False, timeout=15,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode


class SystemdPanelActions(PanelActions):
    """Atomically switch one panel between local and remote service owners."""

    def __init__(self, *, slot_name: str, local_unit: str, remote_unit: str,
                 run: Callable[[str, str], int] = _systemctl):
        self.slot_name = slot_name
        self.local_unit = local_unit
        self.remote_unit = remote_unit
        self.run = run

    def _check_slot(self, slot_name: str) -> None:
        if slot_name != self.slot_name:
            raise RuntimeError("panel action targets another slot")

    def _active(self, unit: str) -> bool:
        return self.run("is-active", unit) == 0

    def blank(self, slot_name: str) -> None:
        self._check_slot(slot_name)
        if not self._active(self.local_unit):
            raise RuntimeError("local panel owner is not active")
        try:
            if self.run("stop", self.remote_unit) != 0:
                raise RuntimeError("cannot stop remote panel owner")
            if self.run("stop", self.local_unit) != 0:
                raise RuntimeError("cannot stop local panel owner")
            if self._active(self.local_unit) or self._active(self.remote_unit):
                raise RuntimeError(
                    "panel owners remained active after reservation")
        except Exception:
            # A failed systemd job can still have changed state.  Reservation
            # is not committed yet, so restore defensively here rather than
            # relying on PanelLeaseState.expire().
            self.run("stop", self.remote_unit)
            self.run("start", self.local_unit)
            raise

    def restore(self, slot_name: str) -> None:
        self._check_slot(slot_name)
        remote_result = self.run("stop", self.remote_unit)
        local_result = self.run("start", self.local_unit)
        if remote_result != 0 or local_result != 0:
            raise RuntimeError("panel restore jobs failed")
        if not self.safe(slot_name):
            raise RuntimeError("local panel ownership was not restored")

    def safe(self, slot_name: str) -> bool:
        self._check_slot(slot_name)
        return (self._active(self.local_unit)
                and not self._active(self.remote_unit))


def serve_primary_endpoint(runtime: DisplayPanelRuntime) -> int:
    if runtime.listener_fd is None:
        raise RuntimeError("primary panel endpoint has no network listener")
    network = socket.socket(fileno=runtime.listener_fd)
    panel = None
    local = None
    path = runtime.target["control_unix_path"]
    try:
        panel = accept_primary_control(runtime.identity, listener=network)
        local = prepare_endpoint_listener(
            path, uid=runtime.target["control_uid"],
            gid=runtime.target["control_gid"])
        agent = PrimaryPanelAgent(
            panel, heartbeat_seconds=runtime.lease.heartbeat_ms / 1000.0)
        while True:
            client, _address = local.accept()
            with client:
                client.settimeout(10)
                if peer_uid(client) != runtime.target["control_uid"]:
                    send_endpoint_response(
                        client, ok=False, result="unauthorized-controller")
                    continue
                try:
                    action = receive_endpoint_request(
                        client, role="peer-panel",
                        generation=runtime.lease.generation,
                        session_id=runtime.identity.binding.session_id,
                        slot_name=runtime.lease.slot_name,
                        actions=_ACTIONS)
                except DisplayDockRpcError:
                    send_endpoint_response(
                        client, ok=False, result="invalid-request")
                    continue
                try:
                    result = agent.perform(action)
                    expected = {
                        "peer-blank-panel": "reserved",
                        "heartbeat": "renewed",
                        "peer-unblank-panel": "restored",
                        "safe": "safe",
                        "recover": "safe",
                    }[action]
                    if result != expected:
                        send_endpoint_response(client, ok=False, result=result)
                    else:
                        send_endpoint_response(client, ok=True, result=result)
                except Exception as exc:
                    restored = agent.fail_closed(exc)
                    if restored and action in {
                        "peer-unblank-panel", "safe", "recover",
                    }:
                        result = ("restored" if action == "peer-unblank-panel"
                                  else "safe")
                        send_endpoint_response(client, ok=True, result=result)
                    else:
                        send_endpoint_response(
                            client, ok=False, result="panel-transition-failed")
    finally:
        if panel is not None:
            panel.close()
        if local is not None:
            local.close()
        Path(path).unlink(missing_ok=True)
        network.close()


def serve_peer_endpoint(runtime: DisplayPanelRuntime) -> int:
    actions = SystemdPanelActions(
        slot_name=runtime.lease.slot_name,
        local_unit=runtime.target["local_unit"],
        remote_unit=runtime.target["remote_unit"])
    state = PanelLeaseState(runtime.lease, actions)
    connect_peer_control(
        runtime.identity, host=runtime.target["primary_host"],
        port=runtime.target["primary_port"], state=state)
    return 0


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - subprocess
    ap = argparse.ArgumentParser(prog="qdistro-mm-display-panel")
    ap.add_argument("--config-fd", type=int, required=True)
    args = ap.parse_args(argv)
    runtime = parse_panel_config(
        _read_owned_json_fd(args.config_fd, "display panel config"))
    signal.signal(signal.SIGTERM, _exit_on_signal)
    if runtime.identity.role == "primary":
        return serve_primary_endpoint(runtime)
    return serve_peer_endpoint(runtime)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
