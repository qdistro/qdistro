#!/usr/bin/env python3
"""Run the supervised R6 nested product tree across two preserved VMs."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from multimachine.harness.netem import profile  # noqa: E402
from multimachine.harness.vm_backend import QciVMBackend  # noqa: E402
from multimachine.mm_pairing_authority import public_key_bytes  # noqa: E402
from multimachine.mm_remote_session_authority import (  # noqa: E402
    issue_remote_session_grant,
)


def certificate(common_name: str) -> tuple[bytes, bytes, str]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(UTC)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
            .add_extension(x509.ExtendedKeyUsage([
                ExtendedKeyUsageOID.SERVER_AUTH,
                ExtendedKeyUsageOID.CLIENT_AUTH,
            ]), False)
            .sign(key, hashes.SHA256()))
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption())
    pin = hashlib.sha256(
        cert.public_bytes(serialization.Encoding.DER)).hexdigest()
    return cert_pem, key_pem, pin


def wait_guest_file(backend: QciVMBackend, vm: str, path: str,
                    timeout: float = 40) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if backend._vmexec(
                vm, f"test -e {shlex.quote(path)} && echo ready",
                check=False).strip() == "ready":
            return
        time.sleep(0.2)
    raise RuntimeError(f"guest {vm} did not create {path}")


def parse_result(output: str) -> dict:
    for line in reversed(output.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("role") in {"source", "viewer"}:
            return value
    return {}


def stage_product(backend: QciVMBackend, vm: str, role: str,
                  qdshell: Path, qdwin: Path, popup: Path) -> None:
    backend._push_mm_package(vm)
    for name in (
        "qdistro-mm-remote-session-launcher", "qdistro-mm-remote-adapter",
        "qdistro-mm-remote-nested-controller",
        "qdistro-mm-remote-nested-session",
    ):
        backend._push(
            vm, REPO / "multimachine" / name,
            f"/tmp/mm/multimachine/{name}", mode=0o755)
    backend._push(
        vm, REPO / "multimachine/harness/vm/r6-nested-product-peer.py",
        "/tmp/r6-nested-product-peer.py", mode=0o755)
    stack = f"r6-nested-{role}-stack.sh"
    backend._push(
        vm, REPO / "multimachine/harness/vm" / stack,
        f"/tmp/{stack}", mode=0o755)
    backend._vmexec(
        vm, "rm -rf /tmp/qdshell-r5; "
            "cp -a /usr/share/quickshell/qdshell /tmp/qdshell-r5; "
            "mkdir -p /tmp/qdshell-r5/Services/Qdwin")
    for relative in (
        "Services/Qdwin/Qdwin.qml", "Services/Qdshell/BrokerGate.js",
        "Services/Qdwin/RemoteMachine.js",
    ):
        backend._push(vm, qdshell / relative, f"/tmp/qdshell-r5/{relative}")
    if role == "source":
        backend._push(vm, popup, "/tmp/r5-popup-probe", mode=0o755)

    daemon_files = (
        "daemons/meson.build",
        "daemons/nested-pixelfeed/mm-remote-frame-protocol.h",
        "daemons/nested-pixelfeed/mm-remote-frame.c",
        "daemons/nested-pixelfeed/pw-target-resolver.h",
        "daemons/nested-pixelfeed/pw-target-resolver.c",
        "daemons/nested-pixelfeed/qdistro-mm-remote-source-helper.c",
        "daemons/nested-pixelfeed/qdistro-mm-remote-pixelfeed.c",
        "daemons/nested-pixelfeed/qdistro-mm-remote-viewer-helper.c",
    )
    for relative in daemon_files:
        backend._push(
            vm, REPO / relative,
            "/root/qdistro-src/qdistro/" + relative)
    targets = ("qdistro-mm-remote-source-helper" if role == "source" else
               "qdistro-mm-remote-pixelfeed qdistro-mm-remote-viewer-helper")
    backend._vmexec(
        vm, f"ninja -C /root/qdistro-src/qdistro/daemons/build {targets}")
    if role == "source":
        backend._vmexec(
            vm, "install -m 0755 /root/qdistro-src/qdistro/daemons/build/"
                "qdistro-mm-remote-source-helper /usr/bin/"
                "qdistro-mm-remote-source-helper")
    else:
        backend._push(
            vm, qdwin / "qdwin/qdwin-logic.c",
            "/root/qdistro-src/qdwin/qdwin/qdwin-logic.c")
        backend._vmexec(vm, "rm -rf /root/qdistro-src/qdwin/build-r6")
        backend._vmexec(
            vm, "meson setup /root/qdistro-src/qdwin/build-r6 "
                "/root/qdistro-src/qdwin")
        backend._vmexec(
            vm, "ninja -C /root/qdistro-src/qdwin/build-r6 qdwin-shell.so")
        backend._vmexec(
            vm, "install -m 0755 /root/qdistro-src/qdwin/build-r6/"
                "qdwin-shell.so /usr/lib64/weston/qdwin-shell.so")
        backend._vmexec(
            vm, "install -m 0755 /root/qdistro-src/qdistro/daemons/build/"
                "qdistro-mm-remote-pixelfeed /usr/bin/"
                "qdistro-mm-remote-pixelfeed; "
                "install -m 0755 /root/qdistro-src/qdistro/daemons/build/"
                "qdistro-mm-remote-viewer-helper /usr/bin/"
                "qdistro-mm-remote-viewer-helper")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("vm_source")
    ap.add_argument("vm_viewer")
    ap.add_argument("--port", type=int, default=15443)
    ap.add_argument("--profile", default="wifi-good")
    ap.add_argument("--qdshell", type=Path,
                    default=Path("/home/play2/qdistro/qdshell"))
    ap.add_argument("--qdwin", type=Path,
                    default=Path("/home/play2/qdistro/qdwin"))
    ap.add_argument("--popup-binary", type=Path,
                    default=Path("/home/play2/qdistro/qdwin/build-qci/"
                                 "qdwin-popup-probe"))
    args = ap.parse_args()
    netem = profile(args.profile)
    if netem.hard_drop:
        raise SystemExit("use a degraded steady profile; disconnect is injected")
    bundle = Path(f"/tmp/mm-live/r6-nested-product-{args.profile}")
    bundle.mkdir(parents=True, exist_ok=True)
    backend = QciVMBackend(args.vm_source, args.vm_viewer, REPO)
    backend._ensure_hostfwd(args.vm_source, args.port)
    backend._ensure_hostfwd(args.vm_source, 3389)

    source_cert, source_key, source_pin = certificate("vm-source")
    viewer_cert, viewer_key, viewer_pin = certificate("vm-viewer")
    secret = os.urandom(32)
    authority = Ed25519PrivateKey.generate()
    now = int(time.time())
    receipt = issue_remote_session_grant(
        source_machine="vm-source", viewer_machine="vm-viewer",
        trust_domain_id="owner-machines", generation=72,
        session_id="viewer-session-r6-product",
        stream_id="stream_r6_product_0123456789",
        key_id="adapter-key-r6-product", session_secret=secret,
        issued_at=now, handoff_expires_at=now + 300,
        key_expires_at=now + 8 * 60 * 60, allow_input=True,
        source_tls_cert_sha256=source_pin,
        viewer_tls_cert_sha256=viewer_pin, private_key=authority)

    with tempfile.TemporaryDirectory(prefix="qdistro-r6-product-") as tmp:
        tmpdir = Path(tmp)
        common = {"grant.json": json.dumps(receipt).encode(), "secret.bin": secret}
        per_role = {
            "source": {**common, "cert.pem": source_cert,
                       "key.pem": source_key, "peer.pem": viewer_cert},
            "viewer": {**common, "cert.pem": viewer_cert,
                       "key.pem": viewer_key, "peer.pem": source_cert},
        }
        for vm, role in ((args.vm_source, "source"),
                         (args.vm_viewer, "viewer")):
            stage_product(
                backend, vm, role, args.qdshell, args.qdwin,
                args.popup_binary)
            backend._vmexec(vm, "mkdir -p /etc/qdistro/multimachine")
            pin = tmpdir / f"{role}-authority.pub"
            pin.write_bytes(public_key_bytes(authority.public_key()))
            backend._push(
                vm, pin,
                "/etc/qdistro/multimachine/pairing-authority.ed25519.pub")
            for name, payload in per_role[role].items():
                local = tmpdir / f"{role}-{name}"
                local.write_bytes(payload)
                guest = f"/run/r6-product-{name}"
                backend._push(vm, local, guest, mode=0o600)
                backend._vmexec(vm, f"chown admin:admin {shlex.quote(guest)}")

    vm_exec = REPO / "scripts/vm/vm-exec"
    source_stack = subprocess.Popen(
        [str(vm_exec), args.vm_source, "bash /tmp/r6-nested-source-stack.sh"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    viewer_stack = subprocess.Popen(
        [str(vm_exec), args.vm_viewer, "bash /tmp/r6-nested-viewer-stack.sh"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    display_server = subprocess.Popen(
        ["Xvfb", "-displayfd", "1", "-screen", "0", "1024x640x24",
         "-nolisten", "tcp"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert display_server.stdout is not None
    display = ":" + display_server.stdout.readline().strip()
    rdp = None
    source_peer = viewer_peer = None
    source_output = viewer_output = ""
    netem_devs: dict[str, str] = {}
    try:
        rdp_env = os.environ.copy()
        rdp_env.update({"DISPLAY": display, "SDL_VIDEODRIVER": "x11"})
        for _ in range(40):
            candidate = subprocess.Popen(
                ["sdl-freerdp", "/v:127.0.0.1:3389", "/cert:ignore",
                 "/u:r6-product", "/p:r6-product", "/size:1024x640"],
                env=rdp_env, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
            time.sleep(0.5)
            if candidate.poll() is None:
                rdp = candidate
                break
        if rdp is None:
            raise RuntimeError("FreeRDP could not connect to source qdwin")
        wait_guest_file(backend, args.vm_source, "/run/mm-r6-source/ready")
        wait_guest_file(backend, args.vm_viewer, "/run/mm-r6-viewer/ready")

        netem_devs = {
            vm: backend._guest_link_dev(vm)
            for vm in (args.vm_source, args.vm_viewer)
        }
        for vm, dev in netem_devs.items():
            backend._vmexec(
                vm, shlex.join(netem.tc_del(dev)) + " 2>/dev/null || true")
            backend._vmexec(vm, shlex.join(netem.tc_add(dev)))

        base = (
            f"--port {args.port} --grant /run/r6-product-grant.json "
            "--secret /run/r6-product-secret.bin "
            "--cert /run/r6-product-cert.pem --key /run/r6-product-key.pem "
            "--peer-cert /run/r6-product-peer.pem")
        source_cmd = (
            "runuser -u admin -- env HOME=/home/admin "
            "XDG_RUNTIME_DIR=/run/user/1000 PYTHONPATH=/tmp/mm "
            "python3 /tmp/r6-nested-product-peer.py --role source "
            f"--host 0.0.0.0 {base}")
        viewer_cmd = (
            "runuser -u admin -- env HOME=/home/admin "
            "XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=r6-viewer "
            "PYTHONPATH=/tmp/mm python3 /tmp/r6-nested-product-peer.py "
            f"--role viewer --host 10.0.2.2 {base}")
        source_peer = subprocess.Popen(
            [str(vm_exec), args.vm_source, source_cmd],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        viewer_peer = subprocess.Popen(
            [str(vm_exec), args.vm_viewer, viewer_cmd],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        wait_guest_file(
            backend, args.vm_source,
            "/run/mm-r6-source/transport-drop-ready", timeout=90)
        dropped = backend._vmexec(
            args.vm_source,
            f"ss -K sport = :{args.port}; "
            "touch /run/mm-r6-source/transport-dropped")
        if "SOCK_DESTROY" in dropped and "Operation not permitted" in dropped:
            raise RuntimeError(f"transport drop failed: {dropped}")
        source_output, _ = source_peer.communicate(timeout=150)
        viewer_output, _ = viewer_peer.communicate(timeout=150)
    finally:
        for vm, dev in netem_devs.items():
            backend._vmexec(
                vm, shlex.join(netem.tc_del(dev)) + " 2>/dev/null || true",
                check=False)
        for vm, path in (
                (args.vm_source, "/run/mm-r6-source/stop"),
                (args.vm_viewer, "/run/mm-r6-viewer/stop")):
            backend._vmexec(vm, f"touch {path}", check=False)
        for process in (source_peer, viewer_peer, source_stack, viewer_stack):
            if process is not None and process.poll() is None:
                process.kill()
        if rdp is not None and rdp.poll() is None:
            rdp.terminate()
        display_server.terminate()
        for process in (source_stack, viewer_stack):
            try:
                output, _ = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                output, _ = process.communicate()
            (bundle / ("source-stack.log" if process is source_stack else
                       "viewer-stack.log")).write_text(output, encoding="utf-8")

    assert source_peer is not None and viewer_peer is not None
    (bundle / "source-peer.log").write_text(source_output, encoding="utf-8")
    (bundle / "viewer-peer.log").write_text(viewer_output, encoding="utf-8")
    source = parse_result(source_output) if source_peer.returncode == 0 else {}
    viewer = parse_result(viewer_output) if viewer_peer.returncode == 0 else {}
    assertions = {
        "source-product-tree-clean": source_peer.returncode == 0,
        "viewer-product-tree-clean": viewer_peer.returncode == 0,
        "real-viewer-proxy-bound-pixels": viewer.get("proxy_bound_pixels") is True,
        "real-qdni-input-round-trip": source.get("input_round_trip") is True,
        "disconnect-preserved-source-app": source.get("app_survived_detach") is True,
        "disconnect-preserved-viewer-proxy": viewer.get("detach_preserved_proxy") is True,
        "epoch-2-cached-pixels-decoder-acked": (
            source.get("epoch_2_cached_frame_ack") is True
            and viewer.get("epoch_2_media_received") is True),
        "viewer-close-was-source-mediated": (
            source.get("viewer_close_was_source_mediated") is True
            and source.get("ignored_close_preserved_app") is True),
        "source-close-removed-both-proxies": (
            source.get("source_close_removed_proxy") is True
            and viewer.get("source_close_removed_proxy") is True),
    }
    result = {
        "schema": "qdistro-mm-r6-nested-product-evidence-v1",
        "profile": args.profile, "netem": asdict(netem),
        "source_vm": args.vm_source, "viewer_vm": args.vm_viewer,
        "assertions": assertions, "source": source, "viewer": viewer,
    }
    (bundle / "results.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for name, passed in assertions.items():
        print(f"[{'PASS' if passed else 'FAIL'}] {name}")
    if not all(assertions.values()):
        print(source_output[-4000:])
        print(viewer_output[-4000:])
    return 0 if all(assertions.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
