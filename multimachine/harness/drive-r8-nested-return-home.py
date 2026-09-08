#!/usr/bin/env python3
"""Run R8 two-stream detach, source return-home, and clean remount on two VMs."""
from __future__ import annotations

import argparse
import json
import os
import runpy
import shlex
import subprocess
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

REPO = Path(__file__).resolve().parents[2]
COMMON = runpy.run_path(str(REPO / "multimachine/harness/drive-r6-nested-product.py"))
certificate = COMMON["certificate"]
parse_result = COMMON["parse_result"]
stage_product = COMMON["stage_product"]
wait_guest_file = COMMON["wait_guest_file"]
wait_guest_file_or_peer_exit = COMMON["wait_guest_file_or_peer_exit"]

from multimachine.harness.netem import profile  # noqa: E402
from multimachine.harness.vm_backend import QciVMBackend  # noqa: E402
from multimachine.mm_pairing_authority import public_key_bytes  # noqa: E402
from multimachine.mm_remote_session_authority import (  # noqa: E402
    issue_remote_session_grant,
)


STREAMS = (
    "stream_r8_alpha_0123456789",
    "stream_r8_beta_0123456789",
)
SESSION_ID = "viewer-session-r8-return-home"
GENERATIONS = (74, 75)


def touch(backend: QciVMBackend, vm: str, path: str) -> None:
    backend._vmexec(vm, f"touch {shlex.quote(path)}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("vm_source")
    parser.add_argument("vm_viewer")
    parser.add_argument("--base-port", type=int, default=15443)
    parser.add_argument("--profile", default="wifi-good")
    parser.add_argument("--qdshell", type=Path,
                        default=Path("/home/play2/qdistro/qdshell"))
    parser.add_argument("--qdwin", type=Path,
                        default=Path("/home/play2/qdistro/qdwin"))
    parser.add_argument("--popup-binary", type=Path,
                        default=Path("/home/play2/qdistro/qdwin/build-qci/"
                                     "qdwin-popup-probe"))
    args = parser.parse_args()
    netem = profile(args.profile)
    if netem.hard_drop:
        raise SystemExit("R8 requires a degraded but live steady profile")
    bundle = Path(f"/tmp/mm-live/r8-nested-return-home-{args.profile}")
    bundle.mkdir(parents=True, exist_ok=True)
    backend = QciVMBackend(args.vm_source, args.vm_viewer, REPO)
    for port in (args.base_port, args.base_port + 1):
        backend._ensure_hostfwd(args.vm_source, port)
    backend._ensure_hostfwd(args.vm_source, 3389)

    source_cert, source_key, source_pin = certificate("vm-source")
    viewer_cert, viewer_key, viewer_pin = certificate("vm-viewer")
    authority = Ed25519PrivateKey.generate()

    with tempfile.TemporaryDirectory(prefix="qdistro-r8-return-home-") as tmp:
        tmpdir = Path(tmp)
        for vm, role in ((args.vm_source, "source"),
                         (args.vm_viewer, "viewer")):
            stage_product(
                backend, vm, role, args.qdshell, args.qdwin,
                args.popup_binary)
            backend._push(
                vm, REPO / "multimachine/harness/vm/"
                "r8-nested-return-home-peer.py",
                "/tmp/r8-nested-return-home-peer.py", mode=0o755)
            stack = f"r7-nested-{role}-stack.sh"
            backend._push(
                vm, REPO / "multimachine/harness/vm" / stack,
                f"/tmp/{stack}", mode=0o755)
            backend._vmexec(vm, "mkdir -p /etc/qdistro/multimachine")
            pin = tmpdir / f"{role}-authority.pub"
            pin.write_bytes(public_key_bytes(authority.public_key()))
            backend._push(
                vm, pin,
                "/etc/qdistro/multimachine/pairing-authority.ed25519.pub")

        # Mint only after the comparatively expensive product staging so the
        # second-generation five-minute handoff is still fresh at remount.
        now = int(time.time())
        grants: dict[tuple[int, int], tuple[dict, bytes]] = {}
        for phase, generation in enumerate(GENERATIONS, 1):
            for index, stream in enumerate(STREAMS, 1):
                secret = os.urandom(32)
                grant = issue_remote_session_grant(
                    source_machine="vm-source", viewer_machine="vm-viewer",
                    trust_domain_id="owner-machines", generation=generation,
                    session_id=SESSION_ID, stream_id=stream,
                    key_id=f"adapter-key-r8-{phase}-{index}",
                    session_secret=secret, issued_at=now,
                    handoff_expires_at=now + 300,
                    key_expires_at=now + 8 * 60 * 60, allow_input=True,
                    source_tls_cert_sha256=source_pin,
                    viewer_tls_cert_sha256=viewer_pin,
                    private_key=authority)
                grants[(phase, index)] = (grant, secret)

        for vm, role in ((args.vm_source, "source"),
                         (args.vm_viewer, "viewer")):
            cert = source_cert if role == "source" else viewer_cert
            key = source_key if role == "source" else viewer_key
            peer = viewer_cert if role == "source" else source_cert
            for (phase, index), (grant, secret) in grants.items():
                payloads = {
                    "grant.json": json.dumps(grant).encode(),
                    "secret.bin": secret, "cert.pem": cert,
                    "key.pem": key, "peer.pem": peer,
                }
                for name, payload in payloads.items():
                    local = tmpdir / f"{role}-{phase}-{index}-{name}"
                    local.write_bytes(payload)
                    guest = f"/run/r8-product-{phase}-{index}-{name}"
                    backend._push(vm, local, guest, mode=0o600)
                    backend._vmexec(
                        vm, f"chown admin:admin {shlex.quote(guest)}")

    vm_exec = REPO / "scripts/vm/vm-exec"
    source_stack = subprocess.Popen(
        [str(vm_exec), args.vm_source,
         "bash /tmp/r7-nested-source-stack.sh"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    viewer_stack = subprocess.Popen(
        [str(vm_exec), args.vm_viewer,
         "bash /tmp/r7-nested-viewer-stack.sh"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    display_server = subprocess.Popen(
        ["Xvfb", "-displayfd", "1", "-screen", "0", "1024x640x24",
         "-nolisten", "tcp"], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True)
    assert display_server.stdout is not None
    display = ":" + display_server.stdout.readline().strip()
    rdp = None
    source_peer = viewer_peer = None
    source_output = viewer_output = ""
    netem_devs: dict[str, str] = {}
    try:
        rdp_env = os.environ.copy()
        rdp_env.update({"DISPLAY": display, "SDL_VIDEODRIVER": "x11"})
        for _attempt in range(40):
            candidate = subprocess.Popen(
                ["sdl-freerdp", "/v:127.0.0.1:3389", "/cert:ignore",
                 "/u:r8-product", "/p:r8-product", "/size:1024x640"],
                env=rdp_env, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
            time.sleep(0.5)
            if candidate.poll() is None:
                rdp = candidate
                break
        if rdp is None:
            raise RuntimeError("FreeRDP could not connect to source qdwin")
        wait_guest_file(backend, args.vm_source, "/run/mm-r7-source/ready")
        wait_guest_file(backend, args.vm_viewer, "/run/mm-r7-viewer/ready")

        netem_devs = {
            vm: backend._guest_link_dev(vm)
            for vm in (args.vm_source, args.vm_viewer)
        }
        for vm, dev in netem_devs.items():
            backend._vmexec(
                vm, shlex.join(netem.tc_del(dev)) + " 2>/dev/null || true")
            backend._vmexec(vm, shlex.join(netem.tc_add(dev)))

        base_env = (
            "runuser -u admin -- env HOME=/home/admin "
            "XDG_RUNTIME_DIR=/run/user/1000 PYTHONPATH=/tmp/mm ")
        source_cmd = (
            base_env + "python3 /tmp/r8-nested-return-home-peer.py "
            f"--role source --base-port {args.base_port}")
        viewer_cmd = (
            base_env + "WAYLAND_DISPLAY=r7-viewer "
            "python3 /tmp/r8-nested-return-home-peer.py "
            f"--role viewer --base-port {args.base_port}")
        source_peer = subprocess.Popen(
            [str(vm_exec), args.vm_source, source_cmd],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        viewer_peer = subprocess.Popen(
            [str(vm_exec), args.vm_viewer, viewer_cmd],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        peers = {"source": source_peer, "viewer": viewer_peer}

        for vm, marker in (
                (args.vm_source, "/run/mm-r7-source/r8-phase-one-ready"),
                (args.vm_viewer, "/run/mm-r7-viewer/r8-phase-one-ready")):
            wait_guest_file_or_peer_exit(backend, vm, marker, peers, timeout=150)

        touch(backend, args.vm_viewer, "/run/mm-r7-viewer/r8-detach")
        touch(backend, args.vm_source, "/run/mm-r7-source/r8-detach")
        for vm, marker in (
                (args.vm_source, "/run/mm-r7-source/r8-detached-ready"),
                (args.vm_viewer, "/run/mm-r7-viewer/r8-detached-ready")):
            wait_guest_file_or_peer_exit(backend, vm, marker, peers, timeout=120)

        touch(backend, args.vm_source, "/run/mm-r7-source/r8-remount")
        touch(backend, args.vm_viewer, "/run/mm-r7-viewer/r8-remount")
        for vm, marker in (
                (args.vm_source, "/run/mm-r7-source/r8-remounted-ready"),
                (args.vm_viewer, "/run/mm-r7-viewer/r8-remounted-ready")):
            wait_guest_file_or_peer_exit(backend, vm, marker, peers, timeout=150)

        touch(backend, args.vm_source, "/run/mm-r7-source/r8-finish")
        touch(backend, args.vm_viewer, "/run/mm-r7-viewer/r8-finish")
        source_output, _ = source_peer.communicate(timeout=60)
        viewer_output, _ = viewer_peer.communicate(timeout=60)
    finally:
        for vm, dev in netem_devs.items():
            backend._vmexec(
                vm, shlex.join(netem.tc_del(dev)) + " 2>/dev/null || true",
                check=False)
        for vm, path in (
                (args.vm_source, "/run/mm-r7-source/stop"),
                (args.vm_viewer, "/run/mm-r7-viewer/stop")):
            backend._vmexec(vm, f"touch {path}", check=False)
        for process in (source_peer, viewer_peer, source_stack, viewer_stack):
            if process is not None and process.poll() is None:
                process.kill()
        if rdp is not None and rdp.poll() is None:
            rdp.terminate()
        display_server.terminate()
        for process, name in ((source_stack, "source-stack.log"),
                              (viewer_stack, "viewer-stack.log")):
            try:
                output, _ = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                output, _ = process.communicate()
            (bundle / name).write_text(output, encoding="utf-8")
        for process, name, captured in (
                (source_peer, "source-peer.log", source_output),
                (viewer_peer, "viewer-peer.log", viewer_output)):
            if process is None:
                continue
            output = captured
            if not output:
                try:
                    output, _ = process.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    output, _ = process.communicate()
            (bundle / name).write_text(output, encoding="utf-8")

    assert source_peer is not None and viewer_peer is not None
    source = parse_result(source_output) if source_peer.returncode == 0 else {}
    viewer = parse_result(viewer_output) if viewer_peer.returncode == 0 else {}
    assertions = {
        "source-registry-tree-clean": source_peer.returncode == 0,
        "viewer-registry-tree-clean": viewer_peer.returncode == 0,
        "initial-two-stream-product-attachment": (
            source.get("initial_sessions_connected") is True
            and viewer.get("initial_two_proxies_and_pixels") is True),
        "detach-removes-all-shared-gui-attachments": (
            source.get("detach_set_exact") is True
            and viewer.get("detach_set_exact") is True
            and viewer.get("viewer_attachments_empty_after_detach") is True),
        "source-apps-and-pids-survive-return-home": (
            source.get("source_apps_survived_detach") is True
            and source.get("source_pids_unchanged") is True
            and source.get("local_windows_focusable_after_detach") is True),
        "remount-uses-fresh-authority-generation": (
            source.get("fresh_generation_sessions_connected") is True
            and viewer.get("fresh_generation_identities") is True),
        "remount-restores-pixels-and-qdni": (
            source.get("remount_qdni_round_trips") is True
            and viewer.get("fresh_two_proxies_and_pixels") is True),
        "remount-has-exactly-two-no-phantoms": (
            source.get("remount_registry_exact") is True
            and viewer.get("remount_registry_exact") is True
            and viewer.get("no_phantom_duplicates") is True),
        "remount-protected-badges-exact": (
            viewer.get("protected_badges_exact_after_remount") is True),
    }
    result = {
        "schema": "qdistro-mm-r8-nested-return-home-evidence-v1",
        "profile": args.profile, "netem": asdict(netem),
        "source_vm": args.vm_source, "viewer_vm": args.vm_viewer,
        "streams": list(STREAMS), "generations": list(GENERATIONS),
        "assertions": assertions, "source": source, "viewer": viewer,
    }
    (bundle / "results.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for name, passed in assertions.items():
        print(f"[{'PASS' if passed else 'FAIL'}] {name}")
    if not all(assertions.values()):
        print(source_output[-8000:])
        print(viewer_output[-8000:])
    return 0 if all(assertions.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
