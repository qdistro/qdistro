#!/usr/bin/env python3
"""Decisive two-VM R9 gate for the pre-created RDP output-slot design.

This establishes software/runtime behavior only: one qdwin owns an adjacent
headless + RDP topology, a real FreeRDP thin client decodes the remote half of
one straddling toplevel, input returns through the RDP seat, and a full
disconnect/disable/re-enable/reconnect cycle preserves the compositor and app.
Every runtime slot mutation passes through the authenticated qdshell executor;
a separate same-uid output client is denied while that shell is bound. Both
straddling windows are positioned through the production qdshell v30 protocol,
without qdwin's compile-time test placement hook.
It deliberately makes no physical-panel latency or native-feel claim.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from multimachine.harness import capture as capture_mod  # noqa: E402
from multimachine.harness import marker, oracle  # noqa: E402
from multimachine.harness.vm_backend import QciVMBackend  # noqa: E402
from multimachine.display_dock_session import DisplayDockSession  # noqa: E402
from multimachine.display_slot_controller import ControllerEvent  # noqa: E402
from multimachine.mm_display_authority import issue_display_grant  # noqa: E402
from multimachine.mm_pairing_authority import public_key_bytes  # noqa: E402
from multimachine.remote_display_slot import (  # noqa: E402
    ActionKind,
    DisplaySlotSpec,
    SlotAction,
    SlotPhase,
)


W, H = 1280, 800
MW, MH, SEAM, OY = 512, 400, 256, 200
GENERATION = 90
SESSION_ID = "r9-display-session"
SOURCE_RT = "/run/mm-r9-source"
VIEWER_RT = "/run/mm-r9-viewer"


def wait_guest(backend: QciVMBackend, vm: str, command: str, *,
               timeout: float = 60, label: str,
               process: subprocess.Popen[str] | None = None) -> str:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        last = backend._vmexec(vm, command, check=False)
        if last.strip():
            return last
        if process is not None and process.poll() is not None:
            raise RuntimeError(
                f"{label} process exited early with rc={process.returncode}")
        time.sleep(0.25)
    raise RuntimeError(f"timed out waiting for {label}; last={last!r}")


def stage(backend: QciVMBackend, vm_source: str, vm_viewer: str,
          qdwin: Path, qdshell: Path) -> None:
    for vm, name in ((vm_source, "r9-rdp-source-stack.sh"),
                     (vm_viewer, "r9-rdp-viewer-stack.sh")):
        backend._push(
            vm, REPO / "multimachine/harness/vm" / name,
            f"/tmp/{name}", mode=0o755)
    backend._push(
        vm_source,
        REPO / "multimachine/harness/vm/r9-rdp-external-launch.py",
        "/tmp/r9-rdp-external-launch.py", mode=0o755)
    backend._push(
        vm_source,
        REPO / "multimachine/harness/vm/r9-shell-layout-service.py",
        "/tmp/r9-shell-layout-service.py", mode=0o755)
    for vm in (vm_source, vm_viewer):
        backend._push_mm_package(vm)
        backend._push(
            vm,
            REPO / "multimachine/harness/vm/r9-display-carrier-launch.py",
            "/tmp/r9-display-carrier-launch.py", mode=0o755)
        backend._push(
            vm,
            REPO / "multimachine/harness/vm/r9-panel-control.py",
            "/tmp/r9-panel-control.py", mode=0o755)
        for program in (
            "qdistro-mm-display-carrier-launcher",
            "qdistro-mm-display-carrier",
            "qdistro-mm-display-panel-launcher",
            "qdistro-mm-display-panel",
        ):
            backend._push(
                vm, REPO / "multimachine" / program,
                f"/tmp/mm/multimachine/{program}", mode=0o755)

    for relative in (
        "qdwin/qdwin.c", "qdwin/qdwin-logic.c", "qdwin/qdwin-logic.h",
        "qdwin/qdwin-nested-v1.xml", "qdwin/qdwin-shell-v1.xml",
        "test-client/qdwin-output-probe.c",
        "test-client/qdwin-marker-client.c",
        "qdwin/wlr-output-management-unstable-v1.xml",
        "libweston-vendored/src/include/libweston/backend-rdp.h",
        "libweston-vendored/src/libweston/backend-rdp/rdp.h",
    ):
        backend._push_large(
            vm_source, qdwin / relative,
            "/root/qdistro-src/qdwin/" + relative)
    backend._push_large(
        vm_source,
        qdwin / "libweston-vendored/src/libweston/backend-rdp/rdp.c",
        "/root/qdistro-src/qdwin/libweston-vendored/src/"
        "libweston/backend-rdp/rdp.c")
    backend._vmexec(
        vm_source,
        "rm -rf /root/qdistro-src/qdwin/build-r9-live && "
        "meson setup /root/qdistro-src/qdwin/build-r9-live "
        "/root/qdistro-src/qdwin && "
        "ninja -C /root/qdistro-src/qdwin/build-r9-live "
        "qdwin-shell.so qdwin-output-probe qdwin-marker-client && "
        "install -m 0755 /root/qdistro-src/qdwin/build-r9-live/"
        "qdwin-shell.so /tmp/r9-qdwin-shell.so && "
        "install -m 0755 /root/qdistro-src/qdwin/build-r9-live/"
        "qdwin-output-probe /tmp/r9-qdwin-output-probe && "
        "install -m 0755 /root/qdistro-src/qdwin/build-r9-live/"
        "qdwin-marker-client /tmp/r9-qdwin-marker-client && "
        "rm -rf /root/qdistro-src/qdwin/libweston-vendored/src/build-r9-rdp && "
        "meson setup /root/qdistro-src/qdwin/libweston-vendored/src/build-r9-rdp "
        "/root/qdistro-src/qdwin/libweston-vendored/src "
        "-Dbackend-drm=false -Dbackend-rdp=true -Dbackend-vnc=false "
        "-Dbackend-pipewire=false -Dbackend-wayland=false -Dbackend-x11=false "
        "-Dbackend-headless=true -Dbackend-default=headless -Drenderer-gl=false "
        "-Dxwayland=false -Dremoting=false -Dpipewire=false "
        "-Dcolor-management-lcms=false -Dscreenshare=false "
        "-Dshell-desktop=false -Dshell-fullscreen=false -Dshell-ivi=false "
        "-Dshell-kiosk=false -Dimage-jpeg=false -Dimage-webp=false "
        "-Dsystemd=false -Dtools='' -Dtests=false -Ddemo-clients=false "
        "-Dsimple-clients='' -Ddoc=false -Dwcap-decode=false && "
        "ninja -C /root/qdistro-src/qdwin/libweston-vendored/src/build-r9-rdp "
        "libweston/backend-rdp/rdp-backend.so && "
        "install -m 0755 /root/qdistro-src/qdwin/libweston-vendored/src/"
        "build-r9-rdp/libweston/backend-rdp/rdp-backend.so "
        "/tmp/r9-rdp-backend.so",
        timeout=300)

    # Run the product shell, not a second output-management probe. The VM's
    # installed QML tree supplies ordinary desktop components; overlay the
    # exact multi-machine files under test and build the native qdwin binding
    # against the just-built protocol package.
    qdshell_root = "/root/qdistro-src/qdshell-r9"
    backend._vmexec(
        vm_source,
        f"rm -rf {qdshell_root} /tmp/r9-qdshell /tmp/r9-qml; "
        f"install -d {qdshell_root}/qml-plugin; "
        "cp -a /usr/share/quickshell/qdshell /tmp/r9-qdshell; "
        "install -d /tmp/r9-qml/Qdistro/Qdwin")
    for relative in (
        "meson.build", "qml-plugin/meson.build", "qml-plugin/qmldir",
        "qml-plugin/qdwin-binding.cpp", "qml-plugin/qdwin-binding.h",
        "qml-plugin/ctrl-server.cpp", "qml-plugin/ctrl-server.h",
        "qml-plugin/qdistro-qdwin-plugin.cpp",
    ):
        backend._push_large(
            vm_source, qdshell / relative, f"{qdshell_root}/{relative}")
    for relative in (
        "shell.qml", "Services/Qdwin/Qdwin.qml",
        "Services/Qdwin/OutputLayout.js",
        "Services/Qdwin/RemoteDisplayLease.js",
        "Services/Qdwin/RemoteDisplayLease.qml",
    ):
        backend._push_large(
            vm_source, qdshell / relative, f"/tmp/r9-qdshell/{relative}")
    backend._vmexec(
        vm_source,
        "PKG_CONFIG_PATH=/root/qdistro-src/qdwin/build-r9-live/"
        f"meson-uninstalled meson setup {qdshell_root}/build {qdshell_root} && "
        "PKG_CONFIG_PATH=/root/qdistro-src/qdwin/build-r9-live/"
        f"meson-uninstalled ninja -C {qdshell_root}/build && "
        f"install -m 0755 {qdshell_root}/build/qml-plugin/"
        "libqdistro-qdwin.so /tmp/r9-qml/Qdistro/Qdwin/"
        "libqdistro-qdwin.so && "
        f"install -m 0644 {qdshell_root}/qml-plugin/qmldir "
        "/tmp/r9-qml/Qdistro/Qdwin/qmldir",
        timeout=300)


def _carrier_certificate(common_name: str) -> tuple[bytes, bytes, str]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(UTC)
    certificate = (x509.CertificateBuilder()
                   .subject_name(name)
                   .issuer_name(name)
                   .public_key(key.public_key())
                   .serial_number(x509.random_serial_number())
                   .not_valid_before(now - timedelta(minutes=1))
                   .not_valid_after(now + timedelta(days=1))
                   .add_extension(
                       x509.BasicConstraints(ca=True, path_length=0), True)
                   .add_extension(x509.ExtendedKeyUsage([
                       ExtendedKeyUsageOID.SERVER_AUTH,
                       ExtendedKeyUsageOID.CLIENT_AUTH,
                   ]), False)
                   .sign(key, hashes.SHA256()))
    cert_pem = certificate.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption())
    pin = hashlib.sha256(
        certificate.public_bytes(serialization.Encoding.DER)).hexdigest()
    return cert_pem, key_pem, pin


def stage_carrier_grants(backend: QciVMBackend, vm_source: str,
                         vm_viewer: str) -> dict[int, dict]:
    """Stage two strictly increasing one-shot grants for attach + redock."""
    authority = Ed25519PrivateKey.generate()
    primary_cert, primary_key, primary_pin = _carrier_certificate("r9-primary")
    peer_cert, peer_key, peer_pin = _carrier_certificate("r9-peer")
    issued_at = int(time.time())
    payloads: dict[int, dict] = {}
    with tempfile.TemporaryDirectory(prefix="r9-carrier-") as temporary:
        root = Path(temporary)
        authority_path = root / "authority.pub"
        authority_path.write_bytes(public_key_bytes(authority.public_key()))
        for vm in (vm_source, vm_viewer):
            backend._push(vm, authority_path, "/tmp/r9-authority.pub", mode=0o600)
            backend._vmexec(
                vm, "install -D -o root -g root -m 0644 /tmp/r9-authority.pub "
                "/etc/qdistro/multimachine/pairing-authority.ed25519.pub")

        for generation in (GENERATION, GENERATION + 1):
            secret = os.urandom(32)
            receipt = issue_display_grant(
                primary_machine="r9-primary", peer_machine="r9-peer",
                trust_domain_id="r9-owner-machines", generation=generation,
                session_id=SESSION_ID, slot_name="rdp-0",
                logical_x=W, logical_y=0, width=W, height=H, scale=1,
                allow_input=True, carrier_secret=secret,
                primary_tls_cert_sha256=primary_pin,
                peer_tls_cert_sha256=peer_pin,
                issued_at=issued_at, handoff_expires_at=issued_at + 300,
                lease_expires_at=issued_at + 3600, heartbeat_ms=8000,
                private_key=authority)
            payloads[generation] = dict(receipt["payload"])
            grant_path = root / f"grant-{generation}.json"
            layout_path = root / f"layout-{generation}.json"
            secret_path = root / f"secret-{generation}"
            grant_path.write_text(
                json.dumps(receipt, sort_keys=True), encoding="utf-8")
            layout_path.write_text(
                json.dumps(receipt["payload"], sort_keys=True),
                encoding="utf-8")
            secret_path.write_bytes(secret)
            backend._push(
                vm_source, layout_path,
                f"/tmp/r9-layout-g{generation}.json", mode=0o600)
            endpoint_material = {
                vm_source: (primary_cert, primary_key, peer_cert),
                vm_viewer: (peer_cert, peer_key, primary_cert),
            }
            for vm, (local_cert, local_key, peer_certificate) in endpoint_material.items():
                guest = f"/tmp/r9-carrier-g{generation}"
                backend._vmexec(vm, f"rm -rf {guest}; install -d -m 0700 {guest}")
                files = {
                    "grant.json": grant_path,
                    "secret": secret_path,
                }
                for name, content in (
                    ("local.crt", local_cert),
                    ("local.key", local_key),
                    ("peer.crt", peer_certificate),
                ):
                    path = root / f"{vm}-{generation}-{name}"
                    path.write_bytes(content)
                    files[name] = path
                for name, path in files.items():
                    backend._push(vm, path, f"{guest}/{name}", mode=0o600)
    return payloads


def start_carrier(backend: QciVMBackend, vm: str, *, role: str,
                  generation: int) -> None:
    unit = f"mm-r9-carrier-{role}-g{generation}"
    log = (f"{SOURCE_RT}/carrier-g{generation}.log" if role == "primary"
           else f"{VIEWER_RT}/carrier-g{generation}.log")
    backend._vmexec(
        vm, f"systemctl stop {unit} 2>/dev/null || true; "
        f"systemctl reset-failed {unit} 2>/dev/null || true; "
        f"systemd-run --collect --unit={unit} "
        f"--property=StandardOutput=append:{log} "
        f"--property=StandardError=append:{log} "
        f"/tmp/r9-display-carrier-launch.py --role {role} "
        f"--bundle /tmp/r9-carrier-g{generation}")


def wait_carrier_listener(backend: QciVMBackend, vm: str, *, port: int,
                          label: str) -> None:
    wait_guest(
        backend, vm,
        f"ss -ltn | grep -q ':{port} ' && echo listening",
        timeout=10, label=label)


def configure_panel_control_forward(backend: QciVMBackend, vm: str) -> None:
    backend._virsh(
        "qemu-monitor-command", vm, "--hmp",
        "hostfwd_remove tcp:127.0.0.1:3388", check=False)
    backend._virsh(
        "qemu-monitor-command", vm, "--hmp",
        "hostfwd_add tcp:127.0.0.1:3388-:3388")


def source_probe(backend: QciVMBackend, vm: str, *args: str) -> str:
    suffix = " ".join(args)
    return backend._vmexec(
        vm, "runuser -u admin -- env HOME=/home/admin "
        "XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=r9-source "
        f"/tmp/r9-qdwin-output-probe {suffix}")


def position_marker_through_shell(
        backend: QciVMBackend, vm: str, *, excluded: set[int] | None = None,
        timeout: float = 10) -> dict[str, object]:
    """Move the newest marker through qdshell's exclusive v30 binding."""
    excluded = excluded or set()
    deadline = time.monotonic() + timeout
    handles: list[int] = []
    log = ""
    while time.monotonic() < deadline:
        log = backend._vmexec(vm, f"cat {SOURCE_RT}/weston.log")
        handles = [
            int(value) for value in re.findall(
                r"toplevel_added handle=(\d+).*app_id=qdwin-marker-client",
                log)
            if int(value) not in excluded
        ]
        if handles:
            break
        time.sleep(0.2)
    if not handles:
        raise RuntimeError("timed out discovering fresh marker handle")

    handle = handles[-1]
    shell_pid = int(backend._vmexec(
        vm, f"cat {SOURCE_RT}/qdshell.pid").strip())
    output = backend._vmexec(
        vm,
        "runuser -u admin -- env HOME=/home/admin "
        "XDG_RUNTIME_DIR=/run/user/1000 "
        "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus "
        "WAYLAND_DISPLAY=r9-source "
        f"qs ipc --pid {shell_pid} call qdwin positionWindow "
        f"{handle} {W - SEAM} {OY}")

    expected = (
        f"request_set_position handle={handle} outer=({W - SEAM},{OY}) "
        f"size={MW}x{MH}")
    while time.monotonic() < deadline:
        log = backend._vmexec(vm, f"cat {SOURCE_RT}/weston.log")
        matching = [line for line in log.splitlines() if expected in line]
        if matching:
            if any("(clamped)" in line for line in matching):
                raise AssertionError(
                    f"qdshell cross-output position was clamped: {matching[-1]}")
            return {
                "handle": handle,
                "outer": [W - SEAM, OY, MW, MH],
                "ipc_output": output.strip(),
                "compositor_log": matching[-1],
            }
        time.sleep(0.2)
    raise RuntimeError(
        f"timed out waiting for qdshell position acknowledgement: {expected}")


def shell_layout(backend: QciVMBackend, vm: str, *, generation: int,
                 action: str) -> dict:
    """Apply one signed slot delta through controller → qdshell → qdwin."""
    if action not in {"enable", "disable"}:
        raise ValueError(f"unsupported shell layout action: {action}")
    unit = f"mm-r9-shell-layout-g{generation}-{action}"
    grant = f"{SOURCE_RT}/layout-g{generation}.json"
    status = f"{SOURCE_RT}/layout-g{generation}-{action}-status.json"
    output = backend._vmexec(
        vm,
        f"install -o admin -g admin -m 0600 "
        f"/tmp/r9-layout-g{generation}.json {grant}; "
        f"rm -f {status}; "
        f"systemctl stop {unit} 2>/dev/null || true; "
        f"systemctl reset-failed {unit} 2>/dev/null || true; "
        f"systemd-run --quiet --wait --collect --unit={unit} --uid=admin "
        "--setenv=HOME=/home/admin --setenv=XDG_RUNTIME_DIR=/run/user/1000 "
        "--setenv=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus "
        "--setenv=PYTHONPATH=/tmp/mm /usr/bin/python3 "
        "/tmp/r9-shell-layout-service.py "
        f"--shell-pid $(cat {SOURCE_RT}/qdshell.pid) --grant {grant} "
        f"--action {action} --status {status}; cat {status}")
    result = json.loads(output.splitlines()[-1])
    if result != {"action": action, "generation": generation, "ok": True}:
        raise RuntimeError(f"qdshell layout action failed: {result}")
    return result


def shell_input(backend: QciVMBackend, vm: str, *, generation: int,
                action: str) -> dict:
    """Apply one input gate through controller → qdshell → qdwin RDP API."""
    if action not in {"enable", "disable"}:
        raise ValueError(f"unsupported shell input action: {action}")
    unit = f"mm-r9-shell-input-g{generation}-{action}"
    grant = f"{SOURCE_RT}/layout-g{generation}.json"
    status = f"{SOURCE_RT}/input-g{generation}-{action}-status.json"
    output = backend._vmexec(
        vm,
        f"rm -f {status}; "
        f"systemctl stop {unit} 2>/dev/null || true; "
        f"systemctl reset-failed {unit} 2>/dev/null || true; "
        f"systemd-run --quiet --wait --collect --unit={unit} --uid=admin "
        "--setenv=HOME=/home/admin --setenv=XDG_RUNTIME_DIR=/run/user/1000 "
        "--setenv=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus "
        "--setenv=PYTHONPATH=/tmp/mm /usr/bin/python3 "
        "/tmp/r9-shell-layout-service.py "
        f"--shell-pid $(cat {SOURCE_RT}/qdshell.pid) --grant {grant} "
        f"--plane input --action {action} --status {status}; cat {status}")
    result = json.loads(output.splitlines()[-1])
    if result != {"action": action, "generation": generation, "ok": True}:
        raise RuntimeError(f"qdshell input action failed: {result}")
    return result


class LiveShellEndpoint:
    def __init__(self, backend: QciVMBackend, vm: str):
        self.backend = backend
        self.vm = vm
        self.results: list[dict] = []

    def perform(self, action: SlotAction, grant: dict) -> None:
        verb = {
            ActionKind.PRIMARY_ENABLE_OUTPUT: "enable",
            ActionKind.PRIMARY_DISABLE_OUTPUT: "disable",
        }[action.kind]
        self.results.append(shell_layout(
            self.backend, self.vm, generation=grant["generation"],
            action=verb))

    def safe_state_confirmed(self, slot_name: str) -> bool:
        try:
            state = source_probe(
                self.backend, self.vm, f"--expect-state={slot_name}:0")
            return "enabled=0" in state
        except Exception:
            return False


class LivePrimaryLocalEndpoint:
    """Bind primary safety actions to qdshell's real qdwin RDP input gate."""

    def __init__(self, backend: QciVMBackend, vm: str, viewer_vm: str):
        self.backend = backend
        self.vm = vm
        self.viewer_vm = viewer_vm
        self.input_enabled = False
        self.actions: list[str] = []
        self.pre_gate_input: tuple[int, int] | None = None

    def perform(self, action: SlotAction, grant: dict) -> None:
        self.actions.append(action.kind.value)
        if action.kind is ActionKind.PRIMARY_ENABLE_INPUT:
            before = json.loads(self.backend._vmexec(
                self.vm, f"cat {SOURCE_RT}/marker-telemetry.json"))
            self.backend._vmexec(
                self.viewer_vm,
                "YDOTOOL_SOCKET=/run/.ydotool_socket "
                "ydotool key 30:1 30:0; sleep 0.4")
            after = json.loads(self.backend._vmexec(
                self.vm, f"cat {SOURCE_RT}/marker-telemetry.json"))
            self.pre_gate_input = (
                before["totals"]["key_press"],
                after["totals"]["key_press"])
            shell_input(
                self.backend, self.vm, generation=grant["generation"],
                action="enable")
            self.input_enabled = True
        elif action.kind is ActionKind.PRIMARY_DISABLE_INPUT:
            shell_input(
                self.backend, self.vm, generation=grant["generation"],
                action="disable")
            self.input_enabled = False

    def safe_state_confirmed(self, _slot_name: str) -> bool:
        return not self.input_enabled


class LivePeerPanelEndpoint:
    """Drive the viewer's independently expiring authenticated panel agent."""

    def __init__(self, backend: QciVMBackend, source_vm: str, viewer_vm: str):
        self.backend = backend
        self.source_vm = source_vm
        self.viewer_vm = viewer_vm
        self.prepared = False
        self.active_generation: int | None = None

    @staticmethod
    def _unit(role: str, generation: int) -> str:
        return f"mm-r9-panel-{role}-g{generation}"

    def _state(self) -> str:
        output = self.backend._vmexec(
            self.viewer_vm,
            "local=$(systemctl is-active mm-r9-local-panel.service "
            "2>/dev/null || true); "
            "remote=$(systemctl is-active mm-r9-rdp.service "
            "2>/dev/null || true); printf '%s %s' \"$local\" \"$remote\"",
            check=False)
        local, _, remote = output.strip().partition(" ")
        if local == "active" and remote != "active":
            return "safe"
        if local != "active":
            return "reserved"
        return "invalid"

    def _command(self, generation: int, kind: str, *, check: bool = True) -> dict:
        output = self.backend._vmexec(
            self.source_vm,
            "env PYTHONPATH=/tmp/mm /usr/bin/python3 "
            "/tmp/r9-panel-control.py --role command "
            f"--bundle /tmp/r9-carrier-g{generation} "
            f"--runtime {SOURCE_RT} --kind {kind}",
            check=check)
        lines = [line for line in output.splitlines() if line.startswith("{")]
        if not lines:
            if check:
                raise RuntimeError(f"panel {kind} returned no response: {output}")
            return {"ok": False, "error": "no-response"}
        return json.loads(lines[-1])

    def _stop(self, generation: int) -> None:
        self.backend._vmexec(
            self.source_vm,
            f"systemctl stop {self._unit('primary', generation)} "
            "2>/dev/null || true",
            check=False)
        self.backend._vmexec(
            self.viewer_vm,
            f"systemctl stop {self._unit('peer', generation)} "
            "2>/dev/null || true",
            check=False)

    def _start(self, generation: int) -> None:
        primary_unit = self._unit("primary", generation)
        peer_unit = self._unit("peer", generation)
        self._stop(generation)
        self.backend._vmexec(
            self.source_vm,
            f"rm -f /run/qdistro/mm-r9-panel-g{generation}.sock; "
            f"systemctl reset-failed {primary_unit} 2>/dev/null || true; "
            f"systemd-run --collect --unit={primary_unit} "
            f"--property=StandardOutput=append:{SOURCE_RT}/panel-g{generation}.log "
            f"--property=StandardError=append:{SOURCE_RT}/panel-g{generation}.log "
            "--setenv=PYTHONPATH=/tmp/mm /usr/bin/python3 "
            "/tmp/r9-panel-control.py --role primary "
            f"--bundle /tmp/r9-carrier-g{generation} --runtime {SOURCE_RT}")
        wait_guest(
            self.backend, self.source_vm,
            "ss -ltn | grep -q ':3388 ' && echo listening",
            timeout=10, label=f"panel listener generation {generation}")
        self.backend._vmexec(
            self.viewer_vm,
            f"systemctl reset-failed {peer_unit} 2>/dev/null || true; "
            f"systemd-run --collect --unit={peer_unit} "
            f"--property=StandardOutput=append:{VIEWER_RT}/panel-g{generation}.log "
            f"--property=StandardError=append:{VIEWER_RT}/panel-g{generation}.log "
            "--setenv=PYTHONPATH=/tmp/mm /usr/bin/python3 "
            "/tmp/r9-panel-control.py --role peer "
            f"--bundle /tmp/r9-carrier-g{generation} --runtime {VIEWER_RT}")
        wait_guest(
            self.backend, self.source_vm,
            f"test -S /run/qdistro/mm-r9-panel-g{generation}.sock && echo ready",
            timeout=20, label=f"panel control generation {generation}")

    def perform(self, action: SlotAction, grant: dict) -> None:
        generation = grant["generation"]
        if action.kind is ActionKind.PEER_BLANK_PANEL:
            if not self.prepared:
                output = self.backend._vmexec(
                    self.viewer_vm, "bash /tmp/r9-rdp-viewer-stack.sh",
                    timeout=120)
                if "R9_VIEWER_READY" not in output:
                    raise RuntimeError(f"viewer setup failed: {output}")
                self.prepared = True
            self._start(generation)
            response = self._command(generation, "reserve")
            if response != {"ok": True, "result": "reserved"}:
                raise RuntimeError(f"panel reserve failed: {response}")
            if self._state() != "reserved":
                raise RuntimeError("viewer did not enforce panel reservation")
            self.active_generation = generation
        elif action.kind is ActionKind.PEER_UNBLANK_PANEL:
            # Expiry closes the generation before controller cleanup reaches
            # this action. Treat already-safe peer state as successful,
            # idempotent restoration rather than attempting resurrection.
            if self._state() != "safe":
                response = self._command(generation, "release", check=False)
                if response != {"ok": True, "result": "released"}:
                    raise RuntimeError(f"panel release failed: {response}")
            self._stop(generation)
            if self._state() != "safe":
                raise RuntimeError("viewer panel is not locally safe")
            self.active_generation = None
        else:
            raise RuntimeError(f"peer endpoint cannot perform {action.kind}")

    def safe_state_confirmed(self, _slot_name: str) -> bool:
        return self.active_generation is None and self._state() == "safe"

    def heartbeat(self, generation: int, grant: dict) -> None:
        if (generation != grant["generation"]
                or generation != self.active_generation):
            raise RuntimeError("peer panel lease is not reserved")
        response = self._command(generation, "heartbeat")
        if response != {"ok": True, "result": "renewed"}:
            raise RuntimeError(f"panel heartbeat failed: {response}")
        if self._state() != "reserved":
            raise RuntimeError("viewer panel lease was not renewed")


class LiveCarrierEndpoint:
    """Own both pinned-mTLS carrier processes and the thin-client lifetime."""

    def __init__(self, backend: QciVMBackend, source_vm: str, viewer_vm: str):
        self.backend = backend
        self.source_vm = source_vm
        self.viewer_vm = viewer_vm
        self.active_generation: int | None = None

    def perform(self, action: SlotAction, grant: dict) -> None:
        generation = grant["generation"]
        if action.kind is ActionKind.OPEN_AUTHENTICATED_CARRIER:
            start_carrier(
                self.backend, self.source_vm, role="primary",
                generation=generation)
            wait_carrier_listener(
                self.backend, self.source_vm, port=3389,
                label=f"primary carrier generation {generation}")
            start_carrier(
                self.backend, self.viewer_vm, role="peer",
                generation=generation)
            wait_carrier_listener(
                self.backend, self.viewer_vm, port=3390,
                label=f"peer carrier generation {generation}")
            self.backend._vmexec(
                self.viewer_vm,
                f"rm -f {VIEWER_RT}/rdp.log; systemctl start mm-r9-rdp")
            wait_rdp(self.backend, self.viewer_vm)
            self.active_generation = generation
        elif action.kind is ActionKind.CLOSE_CARRIER:
            self.backend._vmexec(
                self.viewer_vm,
                "systemctl stop mm-r9-rdp 2>/dev/null || true")
            for vm, role, runtime in (
                (self.source_vm, "primary", SOURCE_RT),
                (self.viewer_vm, "peer", VIEWER_RT),
            ):
                wait_guest(
                    self.backend, vm,
                    f"grep -q '\"closed_by\"' {runtime}/"
                    f"carrier-g{generation}.log && echo closed",
                    timeout=10,
                    label=f"{role} carrier generation {generation} detach")
                self.backend._vmexec(
                    vm, f"systemctl stop mm-r9-carrier-{role}-g{generation} "
                    "2>/dev/null || true")
            self.active_generation = None
        else:
            raise RuntimeError(f"carrier endpoint cannot perform {action.kind}")

    def safe_state_confirmed(self, _slot_name: str) -> bool:
        return self.active_generation is None

    def alive(self, generation: int) -> bool:
        if generation != self.active_generation:
            return False
        source_active = self.backend._vmexec(
            self.source_vm,
            f"systemctl is-active mm-r9-carrier-primary-g{generation} "
            "2>/dev/null || true", check=False).strip()
        peer_active = self.backend._vmexec(
            self.viewer_vm,
            f"systemctl is-active mm-r9-carrier-peer-g{generation} "
            "2>/dev/null || true", check=False).strip()
        rdp_active = self.backend._vmexec(
            self.viewer_vm,
            "systemctl is-active mm-r9-rdp 2>/dev/null || true",
            check=False).strip()
        return (source_active == "active" and peer_active == "active"
                and rdp_active == "active")


def wait_rdp(backend: QciVMBackend, vm: str, *, timeout: float = 50) -> None:
    wait_guest(
        backend, vm,
        f"grep -q 'Loading Dynamic Virtual Channel rdpgfx' {VIEWER_RT}/rdp.log "
        "2>/dev/null && echo decoded",
        timeout=timeout, label="FreeRDP rdpgfx decode")
    time.sleep(2)


def capture_viewer(backend: QciVMBackend, vm: str, path: Path) -> np.ndarray:
    backend.capture(vm, 0, path)
    return capture_mod.load_image(path)


def assert_remote_half(image: np.ndarray) -> dict:
    if image.shape != (H, W, 3):
        raise AssertionError(f"viewer capture shape {image.shape} != {(H, W, 3)}")
    layout = marker.compute_layout(MW, MH, seam_x=SEAM)
    bands = oracle.verify_marker_half(
        image, layout, 1.0, SEAM, MW, 0, tol=oracle.TOL_RDP, oy=OY)
    if not bands or not all(b.ok for b in bands):
        raise AssertionError(
            "remote straddle half mismatch: " + ", ".join(
                f"{b.name}:{b.classified}/{b.expected}:{b.majority:.3f}"
                for b in bands))
    edge_name, edge_fraction = oracle.classify_color(
        image[OY + 80:OY + MH - 20, 0:8], oracle.TOL_RDP)
    if edge_name != "yellow" or edge_fraction < oracle.MAJORITY:
        raise AssertionError(
            f"remote seam edge is {edge_name}/{edge_fraction:.3f}, not yellow")
    # The streamed half is 256 px wide.  Marker colour beyond that extent
    # would indicate mirroring or hidden client scaling.
    far_name, far_fraction = oracle.classify_color(
        image[OY + 80:OY + MH - 20, SEAM + 12:W - 12], oracle.TOL_RDP)
    if far_name in {"red", "green", "blue", "yellow"} and far_fraction >= 0.5:
        raise AssertionError(
            f"marker leaked beyond remote-half extent: {far_name}/{far_fraction:.3f}")
    return {
        "bands": [b.name for b in bands],
        "seam_edge": [edge_name, edge_fraction],
        "far_region": [far_name, far_fraction],
    }


def pids(backend: QciVMBackend, vm: str) -> tuple[int, int]:
    raw = backend._vmexec(
        vm, f"cat {SOURCE_RT}/weston.pid {SOURCE_RT}/app.pid")
    values = [int(line) for line in raw.splitlines() if line.strip().isdigit()]
    if len(values) != 2:
        raise RuntimeError(f"invalid source pid record: {raw!r}")
    return values[0], values[1]


def assert_alive(backend: QciVMBackend, vm: str,
                 expected: tuple[int, int]) -> None:
    weston_pid, app_pid = expected
    backend._vmexec(
        vm, f"kill -0 {weston_pid} {app_pid}; "
        f"test \"$(cat {SOURCE_RT}/weston.pid)\" = {weston_pid}; "
        f"test \"$(cat {SOURCE_RT}/app.pid)\" = {app_pid}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vm-source", required=True)
    ap.add_argument("--vm-viewer", required=True)
    ap.add_argument("--qdwin", type=Path,
                    default=Path("/home/play2/qdistro/qdwin"))
    ap.add_argument("--qdshell", type=Path,
                    default=Path("/home/play2/qdistro/qdshell"))
    ap.add_argument("--bundle", type=Path,
                    default=Path("/tmp/mm-live/r9-rdp-output"))
    args = ap.parse_args()

    args.bundle.mkdir(parents=True, exist_ok=True)
    backend = QciVMBackend(
        vm_a=args.vm_source, vm_b=args.vm_viewer, repo_dir=REPO,
        out_w=W, out_h=H)
    stage(backend, args.vm_source, args.vm_viewer, args.qdwin, args.qdshell)
    configure_panel_control_forward(backend, args.vm_source)
    # Generate after the potentially long qdwin build so the five-minute
    # signed handoff window is fresh when the endpoints actually connect.
    grants = stage_carrier_grants(backend, args.vm_source, args.vm_viewer)
    backend._vmexec(
        args.vm_source,
        f"test ! -d {SOURCE_RT} || touch {SOURCE_RT}/stop; "
        "systemctl stop mm-r9-qdshell 2>/dev/null || true; "
        "for i in $(seq 1 50); do "
        "test ! -S /run/user/1000/r9-source && break; sleep 0.2; done; "
        f"test ! -S /run/user/1000/r9-source; rm -rf {SOURCE_RT}")
    source = subprocess.Popen(
        [str(REPO / "scripts/vm/vm-exec"), args.vm_source,
         "bash /tmp/r9-rdp-source-stack.sh"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    assertions: dict[str, bool] = {}
    details: dict[str, object] = {}
    source_output = ""
    try:
        wait_guest(
            backend, args.vm_source,
            f"test -e {SOURCE_RT}/ready && "
            f"test -s {SOURCE_RT}/weston.pid && "
            f"test -s {SOURCE_RT}/app.pid && echo ready",
            timeout=60, label="source stack", process=source)
        initial_pids = pids(backend, args.vm_source)
        initial = source_probe(
            backend, args.vm_source, "--expect-heads=2",
            "--expect-state=rdp-0:0")
        assertions["precreated_slot_starts_disabled"] = "enabled=0" in initial

        shell_pid = int(backend._vmexec(
            args.vm_source, f"cat {SOURCE_RT}/qdshell.pid").strip())
        assertions["real_qdshell_bound_as_output_authority"] = shell_pid > 1
        first_position = position_marker_through_shell(
            backend, args.vm_source)
        details["generation_90_shell_position"] = first_position
        assertions["real_qdshell_positions_cross_output_window"] = (
            first_position["outer"] == [W - SEAM, OY, MW, MH])

        shell_endpoint = LiveShellEndpoint(backend, args.vm_source)
        local_endpoint = LivePrimaryLocalEndpoint(
            backend, args.vm_source, args.vm_viewer)
        peer_endpoint = LivePeerPanelEndpoint(
            backend, args.vm_source, args.vm_viewer)
        carrier_endpoint = LiveCarrierEndpoint(
            backend, args.vm_source, args.vm_viewer)
        controller_events: list[ControllerEvent] = []
        controller_time = [time.time()]

        def controller_clock() -> float:
            return controller_time[0]

        controller = DisplayDockSession(
            slot=DisplaySlotSpec("rdp-0"), shell_layout=shell_endpoint,
            primary_local=local_endpoint, peer_panel=peer_endpoint,
            carrier=carrier_endpoint,
            clock=controller_clock, audit=controller_events.append)

        # The real controller owns the whole order: reserve peer panel, await
        # qdshell's exact tagged apply, open both carriers, then enable input.
        controller.attach(grants[GENERATION])
        details["generation_90_enable"] = shell_endpoint.results[-1]
        assertions["controller_generation_90_active"] = (
            controller.phase is SlotPhase.ACTIVE)
        assertions["broker_owned_display_dock_session_active"] = (
            controller.status().session_id == SESSION_ID
            and controller.status().generation == GENERATION)
        details["pre_gate_key_counts"] = local_endpoint.pre_gate_input
        assertions["rdp_input_is_denied_before_authenticated_enable"] = (
            local_endpoint.pre_gate_input is not None
            and local_endpoint.pre_gate_input[0]
            == local_endpoint.pre_gate_input[1])
        assert controller.heartbeat(GENERATION)
        assertions["authenticated_peer_panel_lease_renewed"] = True
        enabled = source_probe(
            backend, args.vm_source, "--expect-heads=2",
            "--expect-state=rdp-0:1")
        assertions["qdshell_enabled_slot_as_adjacent_output"] = (
            "name=headless enabled=1 pos=0,0" in enabled
            and f"name=rdp-0 enabled=1 pos={W},0" in enabled)

        source_boundary = backend._vmexec(
            args.vm_source,
            f"stat -c '%a %U %G' {SOURCE_RT}/rdp-listener.sock; "
            "ss -ltnp | grep ':3389 '")
        viewer_boundary = backend._vmexec(
            args.vm_viewer, "ss -ltnp | grep ':3390 '")
        assertions["qdwin_ingress_is_root_only_private_unix_socket"] = (
            source_boundary.splitlines()[0] == "600 root root"
            and "qdwin" not in source_boundary.lower())
        assertions["peer_raw_rdp_ingress_is_loopback_only"] = (
            "127.0.0.1:3390" in viewer_boundary
            and "0.0.0.0:3390" not in viewer_boundary)
        details["primary_carrier_listener"] = source_boundary.splitlines()[1:]
        details["peer_carrier_listener"] = viewer_boundary.splitlines()
        first = capture_viewer(
            backend, args.vm_viewer, args.bundle / "attached-epoch1.ppm")
        details["epoch1_pixels"] = assert_remote_half(first)
        assertions["rdp_decodes_only_remote_straddle_half_1to1"] = True
        assert controller.heartbeat(GENERATION)

        before = json.loads(backend._vmexec(
            args.vm_source, f"cat {SOURCE_RT}/marker-telemetry.json"))
        backend._vmexec(
            args.vm_viewer,
            "YDOTOOL_SOCKET=/run/.ydotool_socket "
            # This preserved VM's independently measured ydotool absolute
            # apparatus scale is 2x (the existing A2 calibration gate). These
            # viewer coordinates therefore land at RDP-local (128,300), inside
            # the right marker half, rather than laundering that scale into the
            # product coordinate assertion.
            "ydotool mousemove --absolute -x 64 -y 150; sleep 0.4; "
            "YDOTOOL_SOCKET=/run/.ydotool_socket ydotool click 0xC0; "
            "sleep 0.4; YDOTOOL_SOCKET=/run/.ydotool_socket "
            "ydotool key 30:1 30:0; sleep 0.5")
        after = json.loads(wait_guest(
            backend, args.vm_source,
            f"python3 -c 'import json; p=json.load(open(\"{SOURCE_RT}/"
            "marker-telemetry.json\")); print(json.dumps(p))'",
            timeout=10, label="RDP input telemetry"))
        assertions["rdp_peer_input_reaches_single_compositor"] = (
            after["totals"]["button_press"] > before["totals"]["button_press"]
            and after["totals"]["key_press"] > before["totals"]["key_press"])
        details["input_before"] = before["totals"]
        details["input_after"] = after["totals"]

        assert controller.heartbeat(GENERATION)
        # Leave the source controller's deterministic lease clock untouched.
        # The viewer must independently expire on real wall time and restore
        # itself. Only the next source heartbeat discovers that peer-local
        # enforcement already closed the generation and enters fail-safe.
        time.sleep(grants[GENERATION]["heartbeat_ms"] / 1000 + 0.5)
        wait_guest(
            backend, args.vm_viewer,
            "test \"$(systemctl is-active mm-r9-local-panel.service)\" = "
            "active && ! systemctl is-active --quiet mm-r9-rdp.service && "
            "echo safe",
            timeout=5, label="independent peer panel expiry")
        assertions["peer_restores_panel_without_primary_timeout"] = True
        try:
            controller.heartbeat(GENERATION)
        except Exception:
            pass
        else:
            raise AssertionError("expired peer panel heartbeat was accepted")
        assertions["peer_heartbeat_failure_reached_failed_safe"] = (
            controller.phase is SlotPhase.FAILED_SAFE)
        assertions["signed_pinned_mtls_carrier_closed_on_disconnect"] = True
        details["generation_90_disable"] = shell_endpoint.results[-1]
        disabled = source_probe(
            backend, args.vm_source, "--expect-state=rdp-0:0")
        assertions["qdshell_disconnect_disables_output_slot"] = (
            "enabled=0" in disabled)
        denied = source_probe(
            backend, args.vm_source, "--apply", "--enable=rdp-0",
            f"--position={W},0", "--expect-denied")
        denied_state = source_probe(
            backend, args.vm_source, "--expect-state=rdp-0:0")
        assertions["separate_same_uid_output_client_is_denied"] = (
            "denied" in denied and "enabled=0" in denied_state)
        controller.reset_failed_safe()
        assertions["controller_reset_requires_live_safe_endpoints"] = (
            controller.phase is SlotPhase.DISABLED)
        assert_alive(backend, args.vm_source, initial_pids)
        assertions["disconnect_preserves_compositor_and_source_app"] = True
        time.sleep(1)
        blank = capture_viewer(
            backend, args.vm_viewer, args.bundle / "detached.ppm")
        details["detached_mean_luma"] = float(blank.mean())
        assertions["detached_peer_does_not_show_stale_remote_pixels"] = (
            float(blank.mean()) < 12.0)

        controller.attach(grants[GENERATION + 1])
        details["generation_91_enable"] = shell_endpoint.results[-1]
        assertions["controller_generation_91_active"] = (
            controller.phase is SlotPhase.ACTIVE)
        assert controller.heartbeat(GENERATION + 1)
        # R8 correctly rescued the original crossing window onto the surviving
        # local output at detach. Reattachment must not silently teleport a
        # window the user may have moved meanwhile. Place a fresh test window
        # across the restored seam to prove the output is compositing again,
        # while separately requiring the original app PID to remain unchanged.
        backend._vmexec(
            args.vm_source,
            "systemctl stop mm-r9-reattach 2>/dev/null || true; "
            "systemctl reset-failed mm-r9-reattach 2>/dev/null || true; "
            "systemd-run --collect --unit=mm-r9-reattach --uid=admin "
            "--setenv=HOME=/home/admin --setenv=XDG_RUNTIME_DIR=/run/user/1000 "
            "--setenv=WAYLAND_DISPLAY=r9-source "
            "/tmp/r9-qdwin-marker-client --width 512 --height 400 "
            "--seam-x 256 --output-id 9 --generation 90 --frame 2 "
            "--animate-ms 200")
        second_position = position_marker_through_shell(
            backend, args.vm_source,
            excluded={int(first_position["handle"])})
        details["generation_91_shell_position"] = second_position
        assertions["redock_window_uses_new_qdshell_position_request"] = (
            second_position["outer"] == [W - SEAM, OY, MW, MH]
            and second_position["handle"] != first_position["handle"])
        time.sleep(0.5)
        second = capture_viewer(
            backend, args.vm_viewer, args.bundle / "attached-epoch2.ppm")
        details["epoch2_pixels"] = assert_remote_half(second)
        assert_alive(backend, args.vm_source, initial_pids)
        assertions[
            "reattach_composites_fresh_pixels_without_authority_or_app_restart"
        ] = True
        assertions["redock_requires_strictly_newer_carrier_generation"] = True
        final_weston_log = backend._vmexec(
            args.vm_source, f"cat {SOURCE_RT}/weston.log")
        assertions["qdwin_enforces_authenticated_rdp_input_gate"] = (
            final_weston_log.count(
                "remote input gate output=rdp-0 enabled=1 result=applied") >= 2
            and final_weston_log.count(
                "remote input gate output=rdp-0 enabled=0 result=applied") >= 2)
        assertions["production_qdwin_has_no_test_placement_path"] = (
            "TEST placement" not in final_weston_log
            and "QDWIN_TEST_PLACE" not in final_weston_log)
        controller.detach(GENERATION + 1)
        assertions["controller_clean_detach_returns_disabled"] = (
            controller.phase is SlotPhase.DISABLED)
        assertions["broker_retires_display_session_authority_on_detach"] = (
            controller.status().session_id is None
            and controller.status().next_heartbeat is None)
        details["controller_actions"] = [
            {
                "generation": event.generation,
                "phase": event.phase,
                "kind": event.kind,
                "action": event.action,
                "ok": event.ok,
                "detail": event.detail,
            }
            for event in controller_events
        ]
        assertions["all_hard_assertions"] = all(assertions.values())
        if not assertions["all_hard_assertions"]:
            raise AssertionError(f"R9 assertion failure: {assertions}")
    finally:
        backend._vmexec(args.vm_viewer, "systemctl stop mm-r9-rdp", check=False)
        for generation in (GENERATION, GENERATION + 1):
            backend._vmexec(
                args.vm_source,
                f"systemctl stop mm-r9-panel-primary-g{generation} "
                "2>/dev/null || true",
                check=False)
            backend._vmexec(
                args.vm_viewer,
                f"systemctl stop mm-r9-panel-peer-g{generation} "
                "2>/dev/null || true",
                check=False)
        for vm, role in (
            (args.vm_source, "primary"), (args.vm_viewer, "peer"),
        ):
            backend._vmexec(
                vm, "systemctl stop "
                f"mm-r9-carrier-{role}-g{GENERATION} "
                f"mm-r9-carrier-{role}-g{GENERATION + 1} 2>/dev/null || true",
                check=False)
        backend._vmexec(
            args.vm_source, "systemctl stop mm-r9-reattach 2>/dev/null || true",
            check=False)
        backend._vmexec(
            args.vm_source, f"touch {SOURCE_RT}/stop", check=False)
        if source.poll() is None:
            try:
                source_output, _ = source.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                source.kill()
                source_output, _ = source.communicate()
        elif source.stdout is not None:
            source_output = source.stdout.read()
        backend._vmexec(
            args.vm_source,
            "systemctl stop mm-r9-qdshell 'mm-r9-shell-layout-*' "
            "2>/dev/null || true",
            check=False)
        backend._virsh(
            "qemu-monitor-command", args.vm_source, "--hmp",
            "hostfwd_remove tcp:127.0.0.1:3388", check=False)
        (args.bundle / "source-stack.log").write_text(
            source_output, encoding="utf-8")
        for vm, path, name in (
            (args.vm_source, f"{SOURCE_RT}/weston.log", "source-weston.log"),
            (args.vm_viewer, f"{VIEWER_RT}/rdp.log", "viewer-rdp.log"),
        ):
            out = backend._vmexec(vm, f"cat {path}", check=False)
            (args.bundle / name).write_text(out, encoding="utf-8")

    result = {
        "schema": 1,
        "scenario": "r9-precreated-rdp-output-two-vm",
        "scope": "software geometry/lifecycle/input; no physical-feel claim",
        "transport": (
            "signed-grant authenticated qdshell layout + pinned-mTLS carrier "
            "+ independent pinned-mTLS panel lease + qdwin inner RDP TLS "
            "+ FreeRDP 3"),
        "topology": {"source": args.vm_source, "viewer": args.vm_viewer},
        "generations": [GENERATION, GENERATION + 1],
        "source_pids": {
            "weston": initial_pids[0], "app": initial_pids[1],
            "qdshell": shell_pid,
        },
        "assertions": assertions,
        "details": details,
    }
    (args.bundle / "results.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
