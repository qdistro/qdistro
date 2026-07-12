"""Live render/golden runner: marker client -> real compositor -> capture -> oracle.

This is the render/golden layer (09 layer 3) exercised against a *real*
compositor + *real* capture path, not the synthetic numpy renderer. It spawns
stock headless weston (pixman, no GPU, no VM), runs the C marker client
fullscreen so its surface fills the output at (0,0), captures with
``weston-screenshooter``, and runs the deterministic oracle on the decoded PNG —
then writes an evidence bundle.

It is host-runnable with no VM lock (avoids the shared-host
``/tmp/qdistro-vm.lock`` contention) by giving each run a unique wayland socket.
It validates the marker client + oracle + capture against live composited pixels.

NOTE (09 guardrail): a pass here means the marker renders + captures + decodes
correctly through a real compositor on this host. It is **not** remote-output or
A5 proof, and stock weston is not qdwin — the qdwin placement-policy (A1) probe
is a separate step that needs qdwin with two outputs.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import marker as M
from . import oracle as O
from .capture import load_image
from .evidence import Capture, CaptureClass, EvidenceBundle, OracleRecord, Topology


def find_marker_binary() -> str | None:
    env = os.environ.get("QDWIN_MARKER_CLIENT")
    if env and Path(env).exists():
        return env
    cands = ["/tmp/mm-build/qdwin-marker-client"]
    cands += [str(p) for p in Path("/home/play2/qdistro/qdwin").glob(
        "build*/qdwin-marker-client")]
    for c in cands:
        if Path(c).exists():
            return c
    return shutil.which("qdwin-marker-client")


@dataclass
class WestonHeadless:
    """A stock headless weston (pixman) on a unique socket; context manager."""

    width: int = 1280
    height: int = 480
    workdir: Path = Path("/tmp/mm-live")
    socket: str = ""
    _proc: subprocess.Popen | None = None

    def __enter__(self) -> "WestonHeadless":
        self.workdir = Path(self.workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        # unique socket avoids shared-host collisions (memory: free port/socket).
        self.socket = self.socket or f"mm-live-{os.getpid()}-{int(time.time())}"
        xdg = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/xdg-runtime-{os.getuid()}"
        Path(xdg).mkdir(parents=True, exist_ok=True)
        os.chmod(xdg, 0o700)
        ini = self.workdir / "weston.ini"
        ini.write_text("[core]\nshell=desktop\nidle-time=0\n"
                       "require-input=false\nrequire-outputs=any\nrenderer=pixman\n")
        env = {**os.environ, "XDG_RUNTIME_DIR": xdg}
        self._proc = subprocess.Popen(
            ["weston", "--backend=headless", "--renderer=pixman",
             f"--width={self.width}", f"--height={self.height}", "--debug",
             f"--socket={self.socket}", f"--config={ini}",
             f"--log={self.workdir / 'weston.log'}"],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # wait for the socket to appear.
        sock_path = Path(xdg) / self.socket
        for _ in range(100):
            if sock_path.exists():
                break
            time.sleep(0.05)
        time.sleep(0.5)  # let desktop-shell settle
        self.xdg = xdg
        return self

    def __exit__(self, *exc) -> None:
        if self._proc:
            self._proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self._proc.wait(timeout=5)
            if self._proc.poll() is None:
                self._proc.kill()

    def env(self) -> dict:
        return {**os.environ, "XDG_RUNTIME_DIR": self.xdg,
                "WAYLAND_DISPLAY": self.socket}


def run_render_golden(
    *, width: int = 1280, height: int = 480, seam_x: int | None = None,
    output_id: int = 1, generation: int = 5, frame: int = 42,
    bundle_dir: Path | str = "/tmp/mm-render-golden",
    marker_binary: str | None = None, tol: int = O.TOL_LOSSLESS,
) -> tuple[O.OracleResult, EvidenceBundle]:
    """Spawn weston, render the marker fullscreen, capture, run the oracle.

    Returns (oracle result, evidence bundle). Raises if weston/marker/
    screenshooter is missing or the capture cannot be produced.
    """
    marker_binary = marker_binary or find_marker_binary()
    if not marker_binary:
        raise RuntimeError("qdwin-marker-client not found (set QDWIN_MARKER_CLIENT)")
    if not shutil.which("weston") or not shutil.which("weston-screenshooter"):
        raise RuntimeError("weston / weston-screenshooter not on PATH")

    bundle = EvidenceBundle.create(
        bundle_dir, scenario="render-golden", step="static",
        generation=generation,
        topology=Topology(vms=["host-weston"], netem_profile="n/a",
                          description="stock headless weston, pixman, 1 output"))
    cap_dir = bundle.root / "captures"

    with WestonHeadless(width=width, height=height,
                        workdir=Path(bundle_dir) / "weston") as w:
        env = w.env()
        marker = subprocess.Popen(
            [marker_binary, "--width", str(width), "--height", str(height),
             *(["--seam-x", str(seam_x)] if seam_x is not None else []),
             "--output-id", str(output_id), "--generation", str(generation),
             "--frame", str(frame), "--fullscreen"], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            time.sleep(1.5)  # let it map + paint
            # weston-screenshooter writes wayland-screenshot-*.png in CWD.
            subprocess.run(["weston-screenshooter"], env=env, cwd=cap_dir,
                           check=True, capture_output=True, timeout=20)
        finally:
            marker.terminate()
            with contextlib.suppress(Exception):
                marker.wait(timeout=5)

    shots = sorted(cap_dir.glob("wayland-screenshot-*.png"))
    if not shots:
        raise RuntimeError("weston-screenshooter produced no capture")
    shot = shots[-1]

    img = load_image(shot)
    layout = M.compute_layout(width, height, seam_x=seam_x)
    res = O.evaluate(img, layout, 1.0, tol=tol, active_generation=generation,
                     expect_output_id=output_id)

    bundle.manifest.captures.append(Capture(
        path=str(shot.relative_to(bundle.root)),
        capture_class=CaptureClass.VM_A_GUEST.value,
        output_id=output_id, role="host weston output (compositor pixels)",
        fmt="PNG", scale=1.0))
    bundle.add_oracle(OracleRecord(
        capture=str(shot.relative_to(bundle.root)), ok=res.ok,
        output_id=res.payload.output_id if res.payload else None,
        generation=res.payload.generation if res.payload else None,
        frame=res.payload.frame if res.payload else None,
        measured_scale=res.measured_scale, hidden_scaling=res.hidden_scaling,
        stale_generation=res.stale_generation,
        bad_bands=[b.name for b in res.bands if not b.ok], notes=res.notes))
    bundle.manifest.passed = res.ok
    bundle.write()
    return res, bundle


if __name__ == "__main__":  # pragma: no cover - manual host runner
    import json
    import sys

    result, bundle = run_render_golden()
    print(f"oracle: {result.summary()}")
    print(f"bundle: {bundle.root}")
    print(json.dumps([b.__dict__ for b in result.bands], indent=2))
    sys.exit(0 if result.ok else 1)
