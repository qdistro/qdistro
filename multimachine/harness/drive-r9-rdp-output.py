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
import json
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from multimachine.harness import capture as capture_mod  # noqa: E402
from multimachine.harness import marker, oracle  # noqa: E402
from multimachine.harness.vm_backend import QciVMBackend  # noqa: E402


W, H = 1280, 800
MW, MH, SEAM, OY = 512, 400, 256, 200
GENERATION = 90
SOURCE_RT = "/run/mm-r9-source"
VIEWER_RT = "/run/mm-r9-viewer"


def wait_guest(backend: QciVMBackend, vm: str, command: str, *,
               timeout: float = 60, label: str) -> str:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        last = backend._vmexec(vm, command, check=False)
        if last.strip():
            return last
        time.sleep(0.25)
    raise RuntimeError(f"timed out waiting for {label}; last={last!r}")


def stage(backend: QciVMBackend, vm_source: str, vm_viewer: str,
          qdwin: Path) -> None:
    for vm, name in ((vm_source, "r9-rdp-source-stack.sh"),
                     (vm_viewer, "r9-rdp-viewer-stack.sh")):
        backend._push(
            vm, REPO / "multimachine/harness/vm" / name,
            f"/tmp/{name}", mode=0o755)

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
        "qdwin-marker-client /tmp/r9-qdwin-marker-client",
        timeout=300)


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
            timeout=60, label="source stack")
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

        source_probe(
            backend, args.vm_source, "--apply", "--enable=rdp-0",
            f"--position={W},0")
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
        assertions["all_hard_assertions"] = all(assertions.values())
        if not assertions["all_hard_assertions"]:
            raise AssertionError(f"R9 assertion failure: {assertions}")
    finally:
        backend._vmexec(args.vm_viewer, "systemctl stop mm-r9-rdp", check=False)
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
        "transport": "Weston RDP backend + FreeRDP 3",
        "topology": {"source": args.vm_source, "viewer": args.vm_viewer},
        "generation": GENERATION,
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
