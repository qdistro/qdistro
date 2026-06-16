"""Two-VM scenario orchestrator (the qci-engine side of the harness).

Composes everything else — :mod:`topology`, :mod:`netem`, :mod:`bridge`,
:mod:`capture`, :mod:`oracle`, :mod:`evidence` — into the executable form of the
MM-01 remote-whole-window-viewer slice (codex impl-2). The VM-touching steps go
through a small :class:`VMBackend` interface, so:

- a :class:`MockBackend` exercises the *orchestration logic* end-to-end with
  synthetic captures (no libvirt, no FreeRDP) — the dry-run validation codex
  blessed while the VM is contended;
- a real qci backend (a thin adapter over ``vm-exec`` / ``virsh screenshot`` /
  ``tc``, authored when the VM frees) runs the same logic for real.

The orchestrator never weakens the gate: the deterministic oracle decides
pass/fail and the evidence bundle's honesty rule (decoded-remote capture must
carry a passing oracle record) is enforced.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from . import oracle as O
from . import marker as M
from .capture import load_image
from .evidence import (
    Capture, CaptureClass, EvidenceBundle, OracleRecord, Topology as EvTopology,
)
from .netem import profile
from .topology import Topology
from ..bridge import (
    SourceWindowInfo, ViewStreamApproved, bridge_approved, bridge_torn_down,
)
from ..sidechannel import RemoteViewerState


class VMBackend(Protocol):
    """The minimal VM operations the scenario needs. A real impl wraps qci."""

    def spin(self, name: str) -> str: ...
    def exec(self, vm: str, argv: list[str]) -> str: ...
    def screenshot(self, vm: str, screen: int, dest: Path) -> Path: ...
    def subscribe_view_stream(self, vm: str, handle: int) -> ViewStreamApproved: ...
    def apply_netem(self, vm: str, dev: str, profile_name: str) -> None: ...
    def clear_netem(self, vm: str, dev: str) -> None: ...
    def destroy(self, vm: str) -> None: ...


@dataclass
class ViewerSliceResult:
    bundle: EvidenceBundle
    oracle: O.OracleResult
    viewer: RemoteViewerState
    source_alive: bool
    passed: bool


def run_viewer_slice(
    backend: VMBackend, topology: Topology, *, generation: int,
    bundle_dir: Path | str, netem_profile: str = "lan-clean",
    width: int = 800, height: int = 600, marker_output_id: int = 1,
    source_handle: int = 1, tear_down: bool = True,
) -> ViewerSliceResult:
    """Drive MM-01: marker on VM-A → subscribe → bridge → decode on VM-B →
    oracle → (optional) teardown. Returns a populated evidence bundle + verdict.

    The flow is identical for the mock and real backends; only the backend's
    side effects differ. Steps mirror the MM-01 scenario doc.
    """
    prof = profile(netem_profile)  # validates the name early
    bundle = EvidenceBundle.create(
        bundle_dir, scenario="mm-01-viewer-slice", step="static",
        generation=generation,
        topology=EvTopology(vms=[topology.vm_a, topology.vm_b],
                            netem_profile=netem_profile,
                            description="MM-01 remote whole-window viewer"))

    a = backend.spin(topology.vm_a)
    b = backend.spin(topology.vm_b)
    try:
        backend.apply_netem(a, topology.link_dev, netem_profile)

        # VM-A: launch the marker as the source toplevel.
        backend.exec(a, [
            "qdwin-marker-client", "--width", str(width), "--height", str(height),
            "--output-id", str(marker_output_id), "--generation", str(generation),
            "--frame", "0", "--fullscreen"])

        # VM-A: subscribe the source toplevel; bridge the approved endpoint.
        approved = backend.subscribe_view_stream(a, source_handle)
        src = SourceWindowInfo(window_id=source_handle, source_machine=topology.vm_a,
                               title="marker", app_id="qdwin-marker-client",
                               req_w=width, req_h=height)
        announce = bridge_approved(approved, src, generation)
        viewer = RemoteViewerState(generation=generation)
        if not viewer.apply(announce):
            raise RuntimeError("viewer rejected the Announce")

        # VM-B: decode the RDP stream (sdl-freerdp, no scaling) and capture the
        # DECODED-REMOTE framebuffer (what the peer monitor shows).
        vm_b_screen = next(s.screen_index for s in topology.screens.screens
                           if s.vm == topology.vm_b)
        decoded = bundle.root / "captures" / "vm-b-decoded.ppm"
        decoded.parent.mkdir(parents=True, exist_ok=True)
        backend.screenshot(b, vm_b_screen, decoded)

        img = load_image(decoded)
        layout = M.compute_layout(width, height)
        res = O.evaluate(img, layout, 1.0, tol=O.TOL_RDP,
                         active_generation=generation,
                         expect_output_id=marker_output_id)

        cap = Capture(path=str(decoded.relative_to(bundle.root)),
                      capture_class=CaptureClass.VM_B_HOST.value,
                      output_id=marker_output_id, role="VM-B monitor (decoded RDP)",
                      fmt="PPM", scale=1.0)
        bundle.manifest.captures.append(cap)
        bundle.add_oracle(OracleRecord(
            capture=cap.path, ok=res.ok,
            output_id=res.payload.output_id if res.payload else None,
            generation=res.payload.generation if res.payload else None,
            frame=res.payload.frame if res.payload else None,
            measured_scale=res.measured_scale, hidden_scaling=res.hidden_scaling,
            stale_generation=res.stale_generation,
            bad_bands=[x.name for x in res.bands if not x.ok], notes=res.notes))

        source_alive = True
        if tear_down:
            # map the shipped torn_down to the side-channel + assert detach.
            msg = bridge_torn_down("subscriber disconnected", generation,
                                   source_handle, announce.meta.stream_id)
            viewer.apply(msg)
            # the source app must survive a viewer/link teardown (detach).
            source_alive = bool(backend.exec(a, ["pgrep", "-f",
                                                 "qdwin-marker-client"]))

        # verdict; the honesty rule is a *pass* gate — only a bundle that
        # CLAIMS a remote proof must carry a passing decoded-remote oracle
        # record. A failing scenario is a valid failure record (decoded capture
        # present, failing oracle), not an exception.
        passed = res.ok and source_alive and (not tear_down or not viewer.windows)
        if passed:
            bundle.assert_remote_proof()  # refuses to mark a pass without it
        bundle.manifest.passed = passed
        bundle.write()
        return ViewerSliceResult(bundle, res, viewer, source_alive, passed)
    finally:
        backend.clear_netem(a, topology.link_dev)
        backend.destroy(a)
        backend.destroy(b)
