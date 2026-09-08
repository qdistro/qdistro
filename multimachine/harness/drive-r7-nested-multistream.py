#!/usr/bin/env python3
"""Run two isolated nested streams through one viewer qdwin on two VMs."""
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
    "stream_r7_alpha_0123456789",
    "stream_r7_beta_0123456789",
)
SESSION_ID = "viewer-session-r7-multistream"
GENERATION = 73


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
        raise SystemExit("R7 requires a degraded but live steady profile")
    bundle = Path(f"/tmp/mm-live/r7-nested-multistream-{args.profile}")
    bundle.mkdir(parents=True, exist_ok=True)
    backend = QciVMBackend(args.vm_source, args.vm_viewer, REPO)
    for port in (args.base_port, args.base_port + 1):
        backend._ensure_hostfwd(args.vm_source, port)
    backend._ensure_hostfwd(args.vm_source, 3389)

    source_cert, source_key, source_pin = certificate("vm-source")
    viewer_cert, viewer_key, viewer_pin = certificate("vm-viewer")
    authority = Ed25519PrivateKey.generate()
    now = int(time.time())
    grants = []
    secrets = []
    for index, stream in enumerate(STREAMS, 1):
        secret = os.urandom(32)
        secrets.append(secret)
        grants.append(issue_remote_session_grant(
            source_machine="vm-source", viewer_machine="vm-viewer",
            trust_domain_id="owner-machines", generation=GENERATION,
            session_id=SESSION_ID, stream_id=stream,
            key_id=f"adapter-key-r7-{index}", session_secret=secret,
            issued_at=now, handoff_expires_at=now + 300,
            key_expires_at=now + 8 * 60 * 60, allow_input=True,
            source_tls_cert_sha256=source_pin,
            viewer_tls_cert_sha256=viewer_pin, private_key=authority))

    with tempfile.TemporaryDirectory(prefix="qdistro-r7-multistream-") as tmp:
        tmpdir = Path(tmp)
        for vm, role in ((args.vm_source, "source"),
                         (args.vm_viewer, "viewer")):
            stage_product(
                backend, vm, role, args.qdshell, args.qdwin,
                args.popup_binary)
            backend._push(
                vm, REPO / "multimachine/harness/vm/"
                "r7-nested-multistream-peer.py",
                "/tmp/r7-nested-multistream-peer.py", mode=0o755)
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
            cert = source_cert if role == "source" else viewer_cert
            key = source_key if role == "source" else viewer_key
            peer = viewer_cert if role == "source" else source_cert
            for index, (grant, secret) in enumerate(zip(grants, secrets), 1):
                payloads = {
                    "grant.json": json.dumps(grant).encode(),
                    "secret.bin": secret, "cert.pem": cert,
                    "key.pem": key, "peer.pem": peer,
                }
                for name, payload in payloads.items():
                    local = tmpdir / f"{role}-{index}-{name}"
                    local.write_bytes(payload)
                    guest = f"/run/r7-product-{index}-{name}"
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
        for _ in range(40):
            candidate = subprocess.Popen(
                ["sdl-freerdp", "/v:127.0.0.1:3389", "/cert:ignore",
                 "/u:r7-product", "/p:r7-product", "/size:1024x640"],
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
            base_env + "python3 /tmp/r7-nested-multistream-peer.py "
            f"--role source --base-port {args.base_port}")
        viewer_cmd = (
            base_env + "WAYLAND_DISPLAY=r7-viewer "
            "python3 /tmp/r7-nested-multistream-peer.py "
            f"--role viewer --base-port {args.base_port}")
        source_peer = subprocess.Popen(
            [str(vm_exec), args.vm_source, source_cmd],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        viewer_peer = subprocess.Popen(
            [str(vm_exec), args.vm_viewer, viewer_cmd],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        peers = {"source": source_peer, "viewer": viewer_peer}
        wait_guest_file_or_peer_exit(
            backend, args.vm_source, "/run/mm-r7-source/two-ready",
            peers, timeout=120)
        wait_guest_file_or_peer_exit(
            backend, args.vm_viewer, "/run/mm-r7-viewer/two-ready",
            peers, timeout=120)
        backend._vmexec(
            args.vm_viewer, "touch /run/mm-r7-viewer/drop-stream-one")
        source_output, _ = source_peer.communicate(timeout=180)
        viewer_output, _ = viewer_peer.communicate(timeout=180)
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
        "two-distinct-proxies-and-pixel-feeds": (
            viewer.get("two_distinct_proxies") is True
            and viewer.get("two_pixel_feeds") is True),
        "two-independent-qdni-round-trips": (
            source.get("two_qdni_round_trips") is True),
        "two-authority-bound-protected-badges": (
            viewer.get("two_protected_badges") is True
            and viewer.get("both_handles_focusable") is True),
        "sibling-failure-isolated": (
            viewer.get("stream_one_removed_independently") is True
            and viewer.get("stream_two_survived_sibling_failure") is True
            and source.get("stream_one_app_and_supervisor_survived") is True),
        "stream-two-close-source-mediated": (
            source.get("targeted_close_only_stream_two") is True
            and source.get("ignored_close_preserved_both_apps") is True
            and viewer.get("stream_two_close_requested_upstream") is True),
        "source-close-removed-only-stream-two": (
            source.get("source_close_removed_stream_two") is True
            and viewer.get("source_close_removed_stream_two") is True),
    }
    result = {
        "schema": "qdistro-mm-r7-nested-multistream-evidence-v1",
        "profile": args.profile, "netem": asdict(netem),
        "source_vm": args.vm_source, "viewer_vm": args.vm_viewer,
        "streams": list(STREAMS), "assertions": assertions,
        "source": source, "viewer": viewer,
    }
    (bundle / "results.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for name, passed in assertions.items():
        print(f"[{'PASS' if passed else 'FAIL'}] {name}")
    if not all(assertions.values()):
        print(source_output[-6000:])
        print(viewer_output[-6000:])
    return 0 if all(assertions.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
