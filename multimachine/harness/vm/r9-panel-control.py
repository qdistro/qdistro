#!/usr/bin/env python3
"""Two-VM adapter for the authenticated peer panel control plane.

The root-owned files and local Unix bridge are test apparatus. Grant
verification, pinned TLS, secret proof, ordered control, and peer-local expiry
all run through the product modules staged under /tmp/mm.
"""
from __future__ import annotations

import argparse
import json
import os
import pwd
import socket
import subprocess
import time
from pathlib import Path

from multimachine.display_carrier import (
    DisplayCarrierBinding,
    DisplayCarrierIdentity,
)
from multimachine.display_panel_agent import (
    PanelActions,
    PanelLeaseSpec,
    PanelLeaseState,
    accept_primary_control,
    connect_peer_control,
)
from multimachine.mm_display_authority import DisplayGrantVerifier
from multimachine.mm_session_launcher import _read_pinned_authority_key


class ViewerPanelActions(PanelActions):
    def __init__(self, state_path: Path):
        self.state_path = state_path
        self._write("safe")

    def _write(self, state: str) -> None:
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"schema": 1, "state": state}, sort_keys=True) + "\n",
            encoding="utf-8")
        os.chmod(temporary, 0o644)
        temporary.replace(self.state_path)

    @staticmethod
    def _stop_remote() -> None:
        subprocess.run(
            ["systemctl", "stop", "mm-r9-rdp"], check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def blank(self, slot_name: str) -> None:
        if slot_name != "rdp-0":
            raise RuntimeError("unexpected panel slot")
        self._stop_remote()
        self._write("reserved")

    def restore(self, slot_name: str) -> None:
        if slot_name != "rdp-0":
            raise RuntimeError("unexpected panel slot")
        self._stop_remote()
        self._write("safe")

    def safe(self, slot_name: str) -> bool:
        if slot_name != "rdp-0":
            return False
        active = subprocess.run(
            ["systemctl", "is-active", "--quiet", "mm-r9-rdp"],
            check=False).returncode == 0
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return not active and state == {"schema": 1, "state": "safe"}


def load_identity(bundle: Path, role: str) -> tuple[DisplayCarrierIdentity,
                                                     PanelLeaseSpec]:
    receipt = json.loads((bundle / "grant.json").read_text(encoding="utf-8"))
    secret = (bundle / "secret").read_bytes()
    authority = _read_pinned_authority_key()
    payload = receipt.get("payload", {})
    machine = payload.get(f"{role}_machine")
    session = payload.get("session_id")
    if not isinstance(machine, str) or not isinstance(session, str):
        raise ValueError("panel grant identity is missing")
    verified = DisplayGrantVerifier.from_public_bytes(
        authority, role=role, local_machine_id=machine).verify(
            receipt, session_id=session, carrier_secret=secret)
    identity = DisplayCarrierIdentity(
        role=role,
        binding=DisplayCarrierBinding.from_verified_payload(verified),
        secret=secret,
        local_certificate_pem=(bundle / "local.crt").read_bytes(),
        local_private_key_pem=(bundle / "local.key").read_bytes(),
        peer_certificate_pem=(bundle / "peer.crt").read_bytes())
    identity.validate()
    return identity, PanelLeaseSpec.from_verified_payload(verified)


def recv_local(client: socket.socket) -> str:
    data = bytearray()
    while b"\n" not in data and len(data) <= 1024:
        chunk = client.recv(256)
        if not chunk:
            break
        data.extend(chunk)
    if b"\n" not in data or len(data) > 1024:
        raise ValueError("local panel command is incomplete")
    request = json.loads(bytes(data.split(b"\n", 1)[0]))
    if (not isinstance(request, dict) or set(request) != {"kind"}
            or request["kind"] not in {
                "reserve", "heartbeat", "release", "status",
            }):
        raise ValueError("local panel command is invalid")
    return request["kind"]


def run_primary(bundle: Path, runtime: Path) -> int:
    identity, _spec = load_identity(bundle, "primary")
    tcp = socket.socket()
    tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    tcp.bind(("0.0.0.0", 3388))
    tcp.listen(1)
    unix_path = runtime / "panel-control.sock"
    unix_path.unlink(missing_ok=True)
    local = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    local.bind(str(unix_path))
    admin = pwd.getpwnam("admin")
    os.chown(unix_path, admin.pw_uid, admin.pw_gid)
    os.chmod(unix_path, 0o660)
    local.listen(4)
    try:
        panel = accept_primary_control(identity, listener=tcp)
        (runtime / "panel-control.ready").touch()
        while True:
            client, _address = local.accept()
            with client:
                try:
                    kind = recv_local(client)
                    result = panel.request(kind)
                    response = {"ok": True, "result": result}
                except Exception as exc:
                    response = {
                        "ok": False,
                        "error": f"{type(exc).__name__}:{exc}",
                    }
                client.sendall(
                    (json.dumps(response, sort_keys=True) + "\n").encode())
                if not response["ok"]:
                    return 1
    finally:
        local.close()
        tcp.close()
        unix_path.unlink(missing_ok=True)


def run_peer(bundle: Path, runtime: Path, primary_host: str) -> int:
    identity, spec = load_identity(bundle, "peer")
    actions = ViewerPanelActions(runtime / "panel-state.json")
    state = PanelLeaseState(spec, actions)
    deadline = time.monotonic() + 15
    while True:
        try:
            connect_peer_control(
                identity, host=primary_host, port=3388, state=state)
            return 0
        except (ConnectionError, OSError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.1)


def run_command(runtime: Path, kind: str) -> int:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(8)
        client.connect(str(runtime / "panel-control.sock"))
        client.sendall((json.dumps({"kind": kind}) + "\n").encode())
        response = client.makefile("r", encoding="utf-8").readline()
    parsed = json.loads(response)
    print(json.dumps(parsed, sort_keys=True))
    return 0 if parsed.get("ok") is True else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--role", choices=("primary", "peer", "command"), required=True)
    ap.add_argument("--bundle", type=Path, required=True)
    ap.add_argument("--runtime", type=Path, required=True)
    ap.add_argument("--primary-host", default="10.0.2.2")
    ap.add_argument(
        "--kind", choices=("reserve", "heartbeat", "release", "status"))
    args = ap.parse_args()
    args.runtime.mkdir(parents=True, exist_ok=True)
    if args.role == "primary":
        return run_primary(args.bundle, args.runtime)
    if args.role == "command":
        if args.kind is None:
            ap.error("--kind is required for command role")
        return run_command(args.runtime, args.kind)
    return run_peer(args.bundle, args.runtime, args.primary_host)


if __name__ == "__main__":
    raise SystemExit(main())
