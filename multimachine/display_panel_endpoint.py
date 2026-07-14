"""Sealed-config service endpoint for one peer-panel lease generation."""
from __future__ import annotations

import argparse
import json
import os
import socket
import struct
import subprocess
from pathlib import Path
from typing import Callable

from .display_panel_agent import (
    PanelActions,
    PanelLeaseState,
    accept_primary_control,
    connect_peer_control,
)
from .mm_display_panel_launcher import DisplayPanelRuntime, parse_panel_config
from .mm_session_launcher import _read_owned_json_fd


LOCAL_COMMAND_LIMIT = 1024
_KINDS = frozenset({"reserve", "heartbeat", "release", "status"})


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


def _recv_local(client: socket.socket) -> str:
    data = bytearray()
    while b"\n" not in data and len(data) <= LOCAL_COMMAND_LIMIT:
        chunk = client.recv(256)
        if not chunk:
            break
        data.extend(chunk)
    if b"\n" not in data or len(data) > LOCAL_COMMAND_LIMIT:
        raise ValueError("local panel command is incomplete")
    try:
        request = json.loads(bytes(data.split(b"\n", 1)[0]))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("local panel command is not JSON") from exc
    if (not isinstance(request, dict) or set(request) != {"kind"}
            or request["kind"] not in _KINDS):
        raise ValueError("local panel command is invalid")
    return request["kind"]


def _peer_uid(client: socket.socket) -> int:
    credentials = client.getsockopt(
        socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", credentials)
    return uid


def _prepare_control_listener(path: str, *, uid: int, gid: int) -> socket.socket:
    endpoint = Path(path)
    parent = endpoint.parent
    parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    info = parent.stat()
    if info.st_uid != 0 or info.st_mode & 0o022:
        raise RuntimeError("panel control runtime directory is unsafe")
    if endpoint.exists() or endpoint.is_symlink():
        raise RuntimeError("panel control socket already exists")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(path)
        os.chown(path, uid, gid)
        os.chmod(path, 0o660)
        listener.listen(4)
        return listener
    except Exception:
        listener.close()
        endpoint.unlink(missing_ok=True)
        raise


def serve_primary_endpoint(runtime: DisplayPanelRuntime) -> int:
    if runtime.listener_fd is None:
        raise RuntimeError("primary panel endpoint has no network listener")
    network = socket.socket(fileno=runtime.listener_fd)
    panel = None
    local = None
    path = runtime.target["control_unix_path"]
    try:
        panel = accept_primary_control(runtime.identity, listener=network)
        local = _prepare_control_listener(
            path, uid=runtime.target["control_uid"],
            gid=runtime.target["control_gid"])
        while True:
            client, _address = local.accept()
            with client:
                if _peer_uid(client) != runtime.target["control_uid"]:
                    response = {"ok": False, "error": "unauthorized-controller"}
                    client.sendall(
                        (json.dumps(response, sort_keys=True) + "\n").encode())
                    continue
                try:
                    kind = _recv_local(client)
                except ValueError as exc:
                    response = {"ok": False, "error": str(exc)}
                    client.sendall(
                        (json.dumps(response, sort_keys=True) + "\n").encode())
                    continue
                try:
                    result = panel.request(kind)
                except Exception as exc:
                    response = {
                        "ok": False,
                        "error": f"{type(exc).__name__}:{exc}",
                    }
                    client.sendall(
                        (json.dumps(response, sort_keys=True) + "\n").encode())
                    return 1
                response = {"ok": True, "result": result}
                client.sendall(
                    (json.dumps(response, sort_keys=True) + "\n").encode())
                if kind == "release":
                    return 0
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


def send_local_command(path: str, kind: str, *, timeout: float = 8.0) -> dict:
    if kind not in _KINDS:
        raise ValueError("local panel command kind is invalid")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(path)
        client.sendall((json.dumps({"kind": kind}) + "\n").encode())
        response = client.makefile("r", encoding="utf-8").readline()
    if not response:
        raise RuntimeError("panel controller returned no response")
    parsed = json.loads(response)
    if not isinstance(parsed, dict) or parsed.get("ok") not in {True, False}:
        raise RuntimeError("panel controller returned an invalid response")
    return parsed


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - subprocess
    ap = argparse.ArgumentParser(prog="qdistro-mm-display-panel")
    ap.add_argument("--config-fd", type=int)
    ap.add_argument("--control-unix-path")
    ap.add_argument("--command", choices=tuple(sorted(_KINDS)))
    args = ap.parse_args(argv)
    if args.command is not None:
        if args.config_fd is not None or args.control_unix_path is None:
            ap.error("command mode requires only --control-unix-path")
        response = send_local_command(args.control_unix_path, args.command)
        print(json.dumps(response, sort_keys=True), flush=True)
        return 0 if response["ok"] else 1
    if args.config_fd is None or args.control_unix_path is not None:
        ap.error("endpoint mode requires only --config-fd")
    runtime = parse_panel_config(
        _read_owned_json_fd(args.config_fd, "display panel config"))
    if runtime.identity.role == "primary":
        return serve_primary_endpoint(runtime)
    return serve_peer_endpoint(runtime)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
