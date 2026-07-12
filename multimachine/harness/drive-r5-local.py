#!/usr/bin/env python3
"""Stage and evaluate the one-VM R5 nested-local production gate."""
from __future__ import annotations

import argparse
import base64
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from multimachine.harness.capture import load_image  # noqa: E402
from multimachine.harness.vm_backend import QciVMBackend  # noqa: E402


ap = argparse.ArgumentParser()
ap.add_argument("vm")
ap.add_argument("--qdshell", type=Path,
                default=Path("/home/play2/qdistro/qdshell"))
ap.add_argument("--popup-binary", type=Path,
                default=Path("/home/play2/qdistro/qdwin/build-qci/qdwin-popup-probe"))
args = ap.parse_args()

bundle = Path("/tmp/mm-live/r5-local")
bundle.mkdir(parents=True, exist_ok=True)
backend = QciVMBackend(args.vm, args.vm, REPO)


def pull(guest: str, local: Path) -> None:
    encoded = backend._vmexec(args.vm,
                              f"base64 -w0 {shlex.quote(guest)}")
    local.write_bytes(base64.b64decode(encoded.strip(), validate=True))


backend._vmexec(
    args.vm,
    "rm -rf /tmp/qdshell-r5; "
    "cp -a /usr/share/quickshell/qdshell /tmp/qdshell-r5; "
    "mkdir -p /tmp/qdshell-r5/Services/Qdwin")
for relative in (
    "Services/Qdwin/Qdwin.qml",
    "Services/Qdshell/BrokerGate.js",
    "Services/Qdwin/RemoteMachine.js",
):
    backend._push(args.vm, args.qdshell / relative,
                  f"/tmp/qdshell-r5/{relative}")
backend._push(
    args.vm, REPO / "multimachine/harness/vm/r5-nested-local.sh",
    "/tmp/r5-nested-local.sh", mode=0o755)
backend._push(args.vm, args.popup_binary, "/tmp/r5-popup-probe", mode=0o755)

backend._ensure_hostfwd(args.vm, 3389)
display_server = subprocess.Popen(
    ["Xvfb", "-displayfd", "1", "-screen", "0", "1024x640x24",
     "-nolisten", "tcp"],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
assert display_server.stdout is not None
display = ":" + display_server.stdout.readline().strip()
if display == ":":
    raise RuntimeError("Xvfb did not allocate a display")

guest = subprocess.Popen(
    [str(REPO / "scripts/vm/vm-exec"), args.vm,
     "bash /tmp/r5-nested-local.sh"],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
rdp = None
try:
    rdp_env = os.environ.copy()
    rdp_env.update({"DISPLAY": display, "SDL_VIDEODRIVER": "x11"})
    for _ in range(40):
        candidate = subprocess.Popen(
            ["sdl-freerdp", "/v:127.0.0.1:3389", "/cert:ignore",
             "/u:r5-seat", "/p:r5-seat", "/size:1024x640"],
            env=rdp_env, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        time.sleep(0.5)
        if candidate.poll() is None:
            rdp = candidate
            break
    if rdp is None:
        raise RuntimeError("host FreeRDP client could not connect to the guest")

    for _ in range(120):
        ready = backend._vmexec(
            args.vm,
            "test -f /run/mm-r5-local/capture-ready && echo READY",
            timeout=10, check=False)
        if "READY" in ready:
            break
        if guest.poll() is not None:
            break
        time.sleep(0.25)
    else:
        raise RuntimeError("guest did not reach the RDP capture checkpoint")

    shot = bundle / "proxy.png"
    subprocess.run(
        ["import", "-display", display, "-window", "root", str(shot)],
        check=True, timeout=30)
    backend._vmexec(args.vm, "touch /run/mm-r5-local/capture-done")
    output, _ = guest.communicate(timeout=120)
finally:
    if guest.poll() is None:
        guest.terminate()
        try:
            guest.wait(timeout=5)
        except subprocess.TimeoutExpired:
            guest.kill()
    if rdp is not None and rdp.poll() is None:
        rdp.terminate()
        try:
            rdp.wait(timeout=5)
        except subprocess.TimeoutExpired:
            rdp.kill()
    display_server.terminate()
    try:
        display_server.wait(timeout=5)
    except subprocess.TimeoutExpired:
        display_server.kill()
(bundle / "driver.log").write_text(output, encoding="utf-8")
print(output, end="")

for guest, name in (
    ("/run/mm-r5-local/outer.log", "outer.log"),
    ("/run/mm-r5-local/inner.log", "inner.log"),
    ("/run/mm-r5-local/qdshell.log", "qdshell.log"),
    ("/run/mm-r5-local/app.log", "app.log"),
    ("/run/mm-r5-local/proxy.png", "proxy.png"),
):
    try:
        pull(guest, bundle / name)
    except (RuntimeError, ValueError):
        pass

required = [
    "PASS: outer qdwin started with an RDP-backed local seat",
    "PASS: production qdshell owns the outer shell role",
    "PASS: inner qdwin publisher bound qdwin_nested_v1 locally",
    "PASS: production pixelfeed bound a pixel surface to the proxy",
    "PASS: real outer picker routed a per-proxy QDNI button into inner qdwin",
    "PASS: ignored outer close left inner app and proxy alive",
    "PASS: inner owner destruction removed only its outer proxy",
    "PASS: R5 nested-local liveness production gate",
]
results = {line.removeprefix("PASS: "): line in output for line in required}

shot = bundle / "proxy.png"
pixel_ok = False
pixel_detail = "screenshot missing"
if shot.exists():
    image = load_image(shot)
    # Weston RDPGFX is not byte-lossless: solid source colours commonly arrive
    # one or two values away from their SHM-buffer RGB. Keep the geometry oracle
    # exact while allowing that bounded transport conversion.
    parent = np.max(np.abs(
        image.astype(np.int16) - np.array([64, 64, 96])), axis=2) <= 8
    popup = np.max(np.abs(
        image.astype(np.int16) - np.array([255, 0, 96])), axis=2) <= 8
    parent_count = int(parent.sum())
    popup_count = int(popup.sum())
    pixel_ok = parent_count > 85000 and popup_count == 180 * 120
    pixel_detail = f"parent_pixels={parent_count} popup_pixels={popup_count}"
results["decoded framebuffer contains inner parent and exact popup geometry"] = pixel_ok
print(f"[{'PASS' if pixel_ok else 'FAIL'}] R5 pixel oracle: {pixel_detail}")

(bundle / "results.json").write_text(
    json.dumps({"results": results, "pixel_detail": pixel_detail},
               sort_keys=True, indent=2) + "\n",
    encoding="utf-8")
if not all(results.values()):
    raise SystemExit(1)
print(f"R5_LOCAL_OK assertions={len(results)} evidence={bundle}")
