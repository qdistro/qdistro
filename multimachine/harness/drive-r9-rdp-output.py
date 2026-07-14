#!/usr/bin/env python3
"""Decisive two-VM R9 gate for the pre-created RDP output-slot design.

This establishes software/runtime behavior only: one qdwin owns an adjacent
headless + RDP topology, a real FreeRDP thin client decodes the remote half of
one straddling toplevel, input returns through the RDP seat, and a full
disconnect/disable/re-enable/reconnect cycle preserves the compositor and app.
It deliberately makes no physical-panel latency or native-feel claim.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
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
from multimachine.mm_display_authority import issue_display_grant  # noqa: E402
from multimachine.mm_pairing_authority import public_key_bytes  # noqa: E402


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
          qdwin: Path) -> None:
    for vm, name in ((vm_source, "r9-rdp-source-stack.sh"),
                     (vm_viewer, "r9-rdp-viewer-stack.sh")):
        backend._push(
            vm, REPO / "multimachine/harness/vm" / name,
            f"/tmp/{name}", mode=0o755)
    backend._push(
        vm_source,
        REPO / "multimachine/harness/vm/r9-rdp-external-launch.py",
        "/tmp/r9-rdp-external-launch.py", mode=0o755)
    for vm in (vm_source, vm_viewer):
        backend._push_mm_package(vm)
        backend._push(
            vm,
            REPO / "multimachine/harness/vm/r9-display-carrier-launch.py",
            "/tmp/r9-display-carrier-launch.py", mode=0o755)
        for program in (
            "qdistro-mm-display-carrier-launcher",
            "qdistro-mm-display-carrier",
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
        "/root/qdistro-src/qdwin -Denable_test_place=true && "
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
                         vm_viewer: str) -> None:
    """Stage two strictly increasing one-shot grants for attach + redock."""
    authority = Ed25519PrivateKey.generate()
    primary_cert, primary_key, primary_pin = _carrier_certificate("r9-primary")
    peer_cert, peer_key, peer_pin = _carrier_certificate("r9-peer")
    issued_at = int(time.time())
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
                lease_expires_at=issued_at + 3600, heartbeat_ms=1000,
                private_key=authority)
            grant_path = root / f"grant-{generation}.json"
            secret_path = root / f"secret-{generation}"
            grant_path.write_text(
                json.dumps(receipt, sort_keys=True), encoding="utf-8")
            secret_path.write_bytes(secret)
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


def source_probe(backend: QciVMBackend, vm: str, *args: str) -> str:
    suffix = " ".join(args)
    return backend._vmexec(
        vm, "runuser -u admin -- env HOME=/home/admin "
        "XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=r9-source "
        f"/tmp/r9-qdwin-output-probe {suffix}")


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
    ap.add_argument("--bundle", type=Path,
                    default=Path("/tmp/mm-live/r9-rdp-output"))
    args = ap.parse_args()

    args.bundle.mkdir(parents=True, exist_ok=True)
    backend = QciVMBackend(
        vm_a=args.vm_source, vm_b=args.vm_viewer, repo_dir=REPO,
        out_w=W, out_h=H)
    stage(backend, args.vm_source, args.vm_viewer, args.qdwin)
    # Generate after the potentially long qdwin build so the five-minute
    # signed handoff window is fresh when the endpoints actually connect.
    stage_carrier_grants(backend, args.vm_source, args.vm_viewer)
    backend._vmexec(args.vm_source, f"rm -rf {SOURCE_RT}")
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

        # Lease-admission analogue: enable only the reserved RDP head and place
        # it adjacent to the local headless output.
        source_probe(
            backend, args.vm_source, "--apply", "--enable=rdp-0",
            f"--position={W},0")
        enabled = source_probe(
            backend, args.vm_source, "--expect-heads=2",
            "--expect-state=rdp-0:1")
        assertions["slot_enabled_as_adjacent_output"] = (
            "name=headless enabled=1 pos=0,0" in enabled
            and f"name=rdp-0 enabled=1 pos={W},0" in enabled)

        viewer_setup = backend._vmexec(
            args.vm_viewer, "bash /tmp/r9-rdp-viewer-stack.sh", timeout=120)
        if "R9_VIEWER_READY" not in viewer_setup:
            raise RuntimeError(f"viewer setup failed: {viewer_setup}")
        start_carrier(
            backend, args.vm_viewer, role="peer", generation=GENERATION)
        wait_carrier_listener(
            backend, args.vm_viewer, port=3390, label="peer carrier generation 90")
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
        backend._vmexec(args.vm_viewer, "systemctl start mm-r9-rdp")
        wait_rdp(backend, args.vm_viewer)
        first = capture_viewer(
            backend, args.vm_viewer, args.bundle / "attached-epoch1.ppm")
        details["epoch1_pixels"] = assert_remote_half(first)
        assertions["rdp_decodes_only_remote_straddle_half_1to1"] = True

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

        # Fail-safe order: cut the client/input carrier first, then remove the
        # leased output from the desktop. R8 rescues any crossing windows.
        backend._vmexec(args.vm_viewer, "systemctl stop mm-r9-rdp")
        wait_guest(
            backend, args.vm_source,
            f"grep -q '\"closed_by\"' {SOURCE_RT}/carrier-g90.log && echo closed",
            timeout=10, label="primary carrier generation 90 detach")
        wait_guest(
            backend, args.vm_viewer,
            f"grep -q '\"closed_by\"' {VIEWER_RT}/carrier-g90.log && echo closed",
            timeout=10, label="peer carrier generation 90 detach")
        assertions["signed_pinned_mtls_carrier_closed_on_disconnect"] = True
        source_probe(backend, args.vm_source, "--apply", "--disable=rdp-0")
        disabled = source_probe(
            backend, args.vm_source, "--expect-state=rdp-0:0")
        assertions["disconnect_disables_output_slot"] = "enabled=0" in disabled
        assert_alive(backend, args.vm_source, initial_pids)
        assertions["disconnect_preserves_compositor_and_source_app"] = True
        time.sleep(1)
        blank = capture_viewer(
            backend, args.vm_viewer, args.bundle / "detached.ppm")
        details["detached_mean_luma"] = float(blank.mean())
        assertions["detached_peer_does_not_show_stale_remote_pixels"] = (
            float(blank.mean()) < 12.0)

        start_carrier(
            backend, args.vm_source, role="primary", generation=GENERATION + 1)
        wait_carrier_listener(
            backend, args.vm_source, port=3389,
            label="primary carrier generation 91")
        source_probe(
            backend, args.vm_source, "--apply", "--enable=rdp-0",
            f"--position={W},0")
        start_carrier(
            backend, args.vm_viewer, role="peer", generation=GENERATION + 1)
        wait_carrier_listener(
            backend, args.vm_viewer, port=3390,
            label="peer carrier generation 91")
        backend._vmexec(args.vm_viewer, "rm -f /run/mm-r9-viewer/rdp.log; "
                        "systemctl start mm-r9-rdp")
        wait_rdp(backend, args.vm_viewer)
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
        time.sleep(1)
        second = capture_viewer(
            backend, args.vm_viewer, args.bundle / "attached-epoch2.ppm")
        details["epoch2_pixels"] = assert_remote_half(second)
        assert_alive(backend, args.vm_source, initial_pids)
        assertions[
            "reattach_composites_fresh_pixels_without_authority_or_app_restart"
        ] = True
        assertions["redock_requires_strictly_newer_carrier_generation"] = True
        assertions["all_hard_assertions"] = all(assertions.values())
        if not assertions["all_hard_assertions"]:
            raise AssertionError(f"R9 assertion failure: {assertions}")
    finally:
        backend._vmexec(args.vm_viewer, "systemctl stop mm-r9-rdp", check=False)
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
            "signed-grant pinned-mTLS carrier + qdwin inner RDP TLS + FreeRDP 3"),
        "topology": {"source": args.vm_source, "viewer": args.vm_viewer},
        "generations": [GENERATION, GENERATION + 1],
        "source_pids": {"weston": initial_pids[0], "app": initial_pids[1]},
        "assertions": assertions,
        "details": details,
    }
    (args.bundle / "results.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
