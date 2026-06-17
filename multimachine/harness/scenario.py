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
from .control_server import ControlServer
from .netem import profile
from .topology import Topology
from ..bridge import (
    SourceWindowInfo, ViewStreamApproved, bridge_approved, bridge_torn_down,
)
from ..sidechannel import Disconnect, Focus, RemoteViewerState


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


# ==========================================================================
# Scenario-2: live managed-toplevel gate (codex impl-8 §4 / impl-9)
# ==========================================================================
class ManagedVMBackend(VMBackend, Protocol):
    """:class:`VMBackend` plus the managed-viewer operations the scenario-2 gate
    needs (codex impl-9 Q5). A real impl launches ``mm-viewer-launch`` inside the
    proven kiosk-shell weston on VM-B; the mock runs the real :mod:`..viewer`
    state machine against the real :class:`ControlServer` over loopback."""

    def launch_viewer(self, vm: str, *, control_host: str, control_port: int,
                      rdp_host: str, rdp_port: int, generation: int, otp: str,
                      size: str, status_file: str) -> None: ...

    def viewer_status(self, vm: str) -> dict: ...

    def await_decode(self, vm: str, timeout: int = 25) -> bool: ...

    def capture(self, vm: str, screen: int, dest: Path) -> Path: ...

    def stop_viewer(self, vm: str) -> None: ...

    def resubscribe(self, vm: str) -> ViewStreamApproved | None: ...

    def source_alive(self, vm: str) -> bool: ...


@dataclass
class ManagedSliceResult:
    bundle: EvidenceBundle
    oracle: O.OracleResult
    managed_status: dict           # viewer status when the managed toplevel was up
    closed_status: dict            # viewer status after the viewer-side close
    disconnect_status: dict        # viewer status after the link-drop Disconnect
    fresh_approval: bool           # a second subscribe got a fresh stream slot
    source_alive_after_close: bool
    source_alive_after_disconnect: bool
    stale_rejected_live: bool      # a stale-generation control msg was rejected live
    passed: bool


def _poll(fn, predicate, *, tries: int = 50, delay: float = 0.2):
    """Poll ``fn()`` until ``predicate(result)`` or attempts exhausted; returns the
    last result. ``time.sleep`` is the only side effect (kept here so the scenario
    body reads as a flat sequence)."""
    import time
    last = fn()
    for _ in range(tries):
        if predicate(last):
            return last
        time.sleep(delay)
        last = fn()
    return last


def run_managed_toplevel_slice(
    backend: ManagedVMBackend, topology: Topology, *, generation: int,
    bundle_dir: Path | str, netem_profile: str = "lan-clean",
    width: int = 1280, height: int = 800, marker_output_id: int = 1,
    source_handle: int = 1, control_port: int = 5556,
    viewer_control_host: str = "10.0.2.2", viewer_rdp_host: str = "10.0.2.2",
    rdp_user: str = "mm",
) -> ManagedSliceResult:
    """Drive the Phase-1 **managed-toplevel** gate (codex impl-8 §4 / impl-9):
    VM-A marker → subscribe → host-served control side-channel → VM-B
    ``mm-viewer-launch`` (real viewer, fullscreen kiosk decode) → decoded oracle,
    then the lifecycle checks: viewer-side close releases the stream slot while the
    source survives (step 9), and a host-injected ``Disconnect`` blanks the viewer
    + a stale-generation message is rejected + the source survives (step 10).

    Honesty (impl-8): the pass is geometry/protocol/process/lifecycle only — NOT
    "feels native"/A5. The control channel is a **real forwarded TCP byte stream**
    (host-served, viewer reaches it over its SLIRP NAT); the decoded oracle on a
    ``VM_B_HOST`` capture is the fence. Input confinement (step 8) is DEFERRED:
    the marker has no input hook, so a frame-delta input claim would be dishonest.
    """
    profile(netem_profile)  # validates the name early
    bundle = EvidenceBundle.create(
        bundle_dir, scenario="09-mm-viewer-managed-toplevel", step="static",
        generation=generation,
        topology=EvTopology(vms=[topology.vm_a, topology.vm_b],
                            netem_profile=netem_profile,
                            description="Phase-1 managed-toplevel viewer gate"))

    a = backend.spin(topology.vm_a)
    b = backend.spin(topology.vm_b)
    control: ControlServer | None = None
    try:
        backend.apply_netem(a, topology.link_dev, netem_profile)

        # VM-A: marker source toplevel + subscribe + bridge the approval.
        backend.exec(a, [
            "qdwin-marker-client", "--width", str(width), "--height", str(height),
            "--output-id", str(marker_output_id), "--generation", str(generation),
            "--frame", "0", "--fullscreen"])
        approved = backend.subscribe_view_stream(a, source_handle)
        src = SourceWindowInfo(window_id=source_handle, source_machine=topology.vm_a,
                               title="marker", app_id="qdwin-marker-client",
                               req_w=width, req_h=height)
        announce = bridge_approved(approved, src, generation)
        stream_id = announce.meta.stream_id

        # host-served control sidecar (impl-9 Q1): bind, tell VM-B to connect.
        control = ControlServer(port=control_port)
        backend.launch_viewer(
            b, control_host=viewer_control_host, control_port=control.port,
            rdp_host=viewer_rdp_host, rdp_port=approved.rdp_port,
            generation=generation, otp=approved.rdp_password,
            size=f"{width}x{height}", status_file="/run/mm-b/viewer-status.json")
        control.accept()                       # block for the VM-B viewer client
        control.send(announce)                 # the source-derived Announce

        # wait until the viewer reports the managed toplevel, then capture it.
        managed_status = _poll(
            lambda: backend.viewer_status(b),
            lambda s: s.get("status") == "connected" and s.get("windows"))

        vm_b_screen = next(s.screen_index for s in topology.screens.screens
                           if s.vm == topology.vm_b)
        decoded = bundle.root / "captures" / "vm-b-managed.ppm"
        decoded.parent.mkdir(parents=True, exist_ok=True)
        # the viewer reports "connected" on Announce, BEFORE sdl-freerdp renders;
        # wait for the decoded frame so the capture is not a blank pre-frame head.
        backend.await_decode(b)
        layout = M.compute_layout(width, height)
        # capture-retry: the marker animates (so the output stays bright), so a
        # single `virsh screenshot` may catch a torn RDP frame mid-repaint
        # (barcode CRC mismatch). Re-capture until the oracle decodes a clean frame
        # (or attempts exhausted — then the last result is the honest failure).
        res = O.OracleResult(ok=False, payload=None, payload_error="no capture")
        import time as _t
        for _attempt in range(8):
            backend.capture(b, vm_b_screen, decoded)   # capture-only (viewer is live)
            img = load_image(decoded)
            res = O.evaluate(img, layout, 1.0, tol=O.TOL_RDP, auto_origin=True,
                             active_generation=generation,
                             expect_output_id=marker_output_id)
            if res.ok:
                break
            _t.sleep(0.4)
        cap = Capture(path=str(decoded.relative_to(bundle.root)),
                      capture_class=CaptureClass.VM_B_HOST.value,
                      output_id=marker_output_id,
                      role="VM-B monitor (viewer-managed decoded toplevel)",
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

        # --- step 9: viewer-side close releases the slot; source survives -----
        backend.stop_viewer(b)
        closed_status = backend.viewer_status(b)
        source_alive_after_close = backend.source_alive(a)
        fresh = backend.resubscribe(a)
        fresh_approval = bool(fresh and fresh.rdp_password
                              and fresh.rdp_password != approved.rdp_password)

        # --- step 10: link-drop Disconnect blanks the viewer + stale reject ---
        # relaunch a managed viewer for the disconnect subcase (the step-9 one is
        # gone). Reuse the fresh approval's slot.
        disconnect_status: dict = {}
        stale_rejected_live = False
        source_alive_after_disconnect = source_alive_after_close
        if fresh_approval and control is not None:
            ann2 = bridge_approved(fresh, src, generation)
            control.close()
            control = ControlServer(port=control_port)
            backend.launch_viewer(
                b, control_host=viewer_control_host, control_port=control.port,
                rdp_host=viewer_rdp_host, rdp_port=fresh.rdp_port,
                generation=generation, otp=fresh.rdp_password,
                size=f"{width}x{height}", status_file="/run/mm-b/viewer-status.json")
            control.accept()
            control.send(ann2)
            _poll(lambda: backend.viewer_status(b),
                  lambda s: s.get("status") == "connected" and s.get("windows"))
            # a stale-generation Focus must be rejected live (recorded in status).
            control.send(Focus("focus", generation - 1, source_handle, True,
                               ann2.meta.stream_id))
            rejected_status = _poll(
                lambda: backend.viewer_status(b),
                lambda s: any(r.get("reason") == "stale-generation"
                              for r in s.get("rejected", [])))
            stale_rejected_live = any(
                r.get("reason") == "stale-generation"
                for r in rejected_status.get("rejected", []))
            # now drop the link: host injects Disconnect.
            control.send(Disconnect("disconnect", generation, "link-drop"))
            disconnect_status = _poll(
                lambda: backend.viewer_status(b),
                lambda s: s.get("status") == "disconnected")
            source_alive_after_disconnect = backend.source_alive(a)

        # --- verdict ----------------------------------------------------------
        # NB the close is a viewer-process kill, so ``closed_status`` is a
        # best-effort last-written snapshot (a hard-killed viewer does not write a
        # clean "closed" status); the HONEST close proof is the source surviving +
        # the stream slot freeing (a fresh subscribe succeeds), not the stale file.
        managed_ok = bool(res.ok and managed_status.get("windows"))
        close_ok = source_alive_after_close and fresh_approval
        disconnect_ok = (disconnect_status.get("status") == "disconnected"
                         and stale_rejected_live
                         and source_alive_after_disconnect)
        passed = managed_ok and close_ok and disconnect_ok
        if passed:
            bundle.assert_remote_proof()       # honesty gate
        bundle.manifest.passed = passed
        bundle.manifest.notes.append(
            "input confinement (step 8) DEFERRED: the marker has no input hook; "
            "a frame-delta input claim would be dishonest (impl-9 Q4). Follow-up: "
            "per-marker input telemetry + sentinel toplevel.")
        bundle.write()
        return ManagedSliceResult(
            bundle, res, managed_status, closed_status, disconnect_status,
            fresh_approval, source_alive_after_close,
            source_alive_after_disconnect, stale_rejected_live, passed)
    finally:
        if control is not None:
            control.close()
        backend.stop_viewer(b)
        backend.clear_netem(a, topology.link_dev)
        backend.destroy(a)
        backend.destroy(b)


# ==========================================================================
# Scenario-2b: VM-A-SERVED managed-toplevel gate (product shape, codex impl-12)
# ==========================================================================
class VmaControlBackend(ManagedVMBackend, Protocol):
    """:class:`ManagedVMBackend` plus the VM-A-served control ops (codex impl-12):
    the JSON-lines control side-channel originates in VM-A (an ``mm-control``
    ``systemd --user`` unit), not on the host. ``control_port`` is the port
    mm-control binds in VM-A (and the VM-A QEMU hostfwd exposes on host loopback)
    — the viewer reaches it at ``10.0.2.2:control_port`` over its own NAT."""

    control_port: int

    def launch_control(self, vm: str, *, generation: int, window_id: int,
                       source_machine: str, title: str, app_id: str,
                       req_w: int, req_h: int,
                       marker_unit: str = "mm-marker") -> str: ...

    def control_log(self, vm: str) -> dict: ...

    def stop_control(self, vm: str) -> None: ...

    def kill_marker(self, vm: str) -> None: ...


@dataclass
class VmaManagedSliceResult:
    bundle: EvidenceBundle
    oracle: O.OracleResult
    managed_status: dict
    announced_stream_id: str       # the stream_id VM-A minted + announced in-guest
    viewer_stream_id: str          # the stream_id the VM-B viewer actually received
    host_served_control: bool      # MUST be False (no host ControlServer; impl-12)
    detach_reason: str             # mm-control watcher reason after the viewer kill
    detach_emitted_closed: bool    # mm-control wrongly emitted Closed on detach? (False)
    source_alive_after_detach: bool
    fresh_approval: bool
    source_closed_emitted: bool    # mm-control emitted source-driven Closed (True)
    viewer_proxy_removed: bool     # the viewer dropped the proxy after that Closed
    source_dead_after_close: bool  # the marker is now gone
    passed: bool


def _control_metadata(topology: Topology, source_handle: int) -> dict:
    return dict(window_id=source_handle, source_machine=topology.vm_a,
                title="marker", app_id="qdwin-marker-client")


def run_managed_toplevel_vma_slice(
    backend: VmaControlBackend, topology: Topology, *, generation: int,
    bundle_dir: Path | str, netem_profile: str = "lan-clean",
    width: int = 1280, height: int = 800, marker_output_id: int = 1,
    source_handle: int = 1, viewer_control_host: str = "10.0.2.2",
    viewer_rdp_host: str = "10.0.2.2", rdp_user: str = "mm",
) -> VmaManagedSliceResult:
    """Drive the **VM-A-served** managed-toplevel gate (codex impl-12) — the
    product-shaped successor to :func:`run_managed_toplevel_slice`.

    The control side-channel now ORIGINATES in VM-A: an ``mm-control``
    ``systemd --user`` unit builds the source-derived ``Announce`` IN-GUEST
    (``stream_id`` minted in-guest) and a watcher emits ``Closed`` when the SOURCE
    toplevel (the marker's own unit) dies — driven by the source, NOT by the host
    orchestrator. The host is only a NAT/loopback bridge (a VM-A hostfwd, symmetric
    with the RDP relay); there is NO host :class:`ControlServer`.

    Three honest checks:

    1. **Managed oracle** — the VM-B viewer connects to VM-A's control port, gets
       the in-guest Announce, decodes fullscreen; the decoded oracle passes on the
       viewer-managed toplevel, and the stream_id the viewer shows is the one VM-A
       minted (control bytes are source-originated).
    2. **Viewer detach (step 9)** — killing the VM-B viewer is a detach, not source
       death: mm-control emits NO ``Closed``, the source marker survives, and a
       fresh resubscribe reclaims the stream slot.
    3. **Source-driven Closed (step 10)** — killing the marker on VM-A makes
       mm-control's watcher emit ``Closed`` (source-exit), which removes the viewer
       proxy. This proves VM-A owns the source lifecycle.

    Honesty (impl-12): proves "VM-A-served control using source-derived metadata" +
    "VM-A owns the source lifecycle" — geometry/protocol/process/lifecycle only.
    Stale-generation rejection stays covered by :class:`RemoteViewerState` unit
    tests + the prior session-4 live evidence (we deliberately do NOT reintroduce a
    host-side control inject hook here — it would blur the ownership story).
    """
    profile(netem_profile)
    bundle = EvidenceBundle.create(
        bundle_dir, scenario="09-mm-viewer-managed-toplevel-vma", step="static",
        generation=generation,
        topology=EvTopology(vms=[topology.vm_a, topology.vm_b],
                            netem_profile=netem_profile,
                            description="Phase-1 VM-A-served managed-toplevel gate"))

    a = backend.spin(topology.vm_a)
    b = backend.spin(topology.vm_b)
    meta = _control_metadata(topology, source_handle)
    status_file = "/run/mm-b/viewer-status.json"
    try:
        backend.apply_netem(a, topology.link_dev, netem_profile)

        # VM-A source: marker toplevel + subscribe (the RDP creds for the viewer).
        backend.exec(a, [
            "qdwin-marker-client", "--width", str(width), "--height", str(height),
            "--output-id", str(marker_output_id), "--generation", str(generation),
            "--frame", "0", "--fullscreen"])
        approved = backend.subscribe_view_stream(a, source_handle)

        # VM-A-SERVED control: mm-control produces the Announce in-guest. No host
        # ControlServer is constructed anywhere in this slice (impl-12 Q1 caveat:
        # the host listener for the control port is the VM-A hostfwd, not Python).
        announced_stream_id = backend.launch_control(
            a, generation=generation, req_w=width, req_h=height, **meta)

        backend.launch_viewer(
            b, control_host=viewer_control_host, control_port=backend.control_port,
            rdp_host=viewer_rdp_host, rdp_port=approved.rdp_port,
            generation=generation, otp=approved.rdp_password,
            size=f"{width}x{height}", status_file=status_file)
        managed_status = _poll(
            lambda: backend.viewer_status(b),
            lambda s: s.get("status") == "connected" and s.get("windows"))
        windows = managed_status.get("windows") or [{}]
        viewer_stream_id = windows[0].get("stream_id", "")

        # decoded oracle on the viewer-managed toplevel (capture-retry for tearing).
        vm_b_screen = next(s.screen_index for s in topology.screens.screens
                           if s.vm == topology.vm_b)
        decoded = bundle.root / "captures" / "vm-b-managed-vma.ppm"
        decoded.parent.mkdir(parents=True, exist_ok=True)
        backend.await_decode(b)
        layout = M.compute_layout(width, height)
        res = O.OracleResult(ok=False, payload=None, payload_error="no capture")
        import time as _t
        for _attempt in range(8):
            backend.capture(b, vm_b_screen, decoded)
            res = O.evaluate(load_image(decoded), layout, 1.0, tol=O.TOL_RDP,
                             auto_origin=True, active_generation=generation,
                             expect_output_id=marker_output_id)
            if res.ok:
                break
            _t.sleep(0.4)
        cap = Capture(path=str(decoded.relative_to(bundle.root)),
                      capture_class=CaptureClass.VM_B_HOST.value,
                      output_id=marker_output_id,
                      role="VM-B monitor (VM-A-served managed toplevel)",
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

        # --- step 9: viewer-side close is a DETACH (no Closed); slot frees -----
        backend.stop_viewer(b)
        # let mm-control observe the EOF + run its re-check, then read what it sent.
        detach_ctl = _poll(lambda: backend.control_log(a),
                           lambda c: c.get("reason"), tries=40, delay=0.25)
        detach_reason = detach_ctl.get("reason", "")
        detach_emitted_closed = any(m.get("type") == "closed"
                                    for m in detach_ctl.get("sent", []))
        source_alive_after_detach = backend.source_alive(a)
        fresh = backend.resubscribe(a)
        fresh_approval = bool(fresh and fresh.rdp_password
                              and fresh.rdp_password != approved.rdp_password)

        # --- step 10: SOURCE death drives Closed (VM-A owns the lifecycle) -----
        source_closed_emitted = False
        viewer_proxy_removed = False
        source_dead_after_close = False
        if fresh_approval:
            backend.launch_control(a, generation=generation, req_w=width,
                                   req_h=height, **meta)
            backend.launch_viewer(
                b, control_host=viewer_control_host,
                control_port=backend.control_port, rdp_host=viewer_rdp_host,
                rdp_port=fresh.rdp_port, generation=generation,
                otp=fresh.rdp_password, size=f"{width}x{height}",
                status_file=status_file)
            _poll(lambda: backend.viewer_status(b),
                  lambda s: s.get("status") == "connected" and s.get("windows"))
            backend.kill_marker(a)                 # source toplevel dies on VM-A
            close_ctl = _poll(
                lambda: backend.control_log(a),
                lambda c: any(m.get("type") == "closed" for m in c.get("sent", [])),
                tries=40, delay=0.25)
            source_closed_emitted = any(
                m.get("type") == "closed" and m.get("reason") == "source-exit"
                for m in close_ctl.get("sent", []))
            # require a PARSED viewer status (a real status with empty windows),
            # not an absent/unparseable {} — a crashed viewer or missing status
            # file must NOT count as a clean proxy removal (codex impl-13). After a
            # source-driven Closed the viewer is "idle" (proxy gone, link up) or
            # "disconnected" (its decoder also saw the source vanish); both end with
            # windows == [].
            _terminal = ("idle", "disconnected")
            closed_status = _poll(
                lambda: backend.viewer_status(b),
                lambda s: s.get("status") in _terminal and not s.get("windows"))
            viewer_proxy_removed = (closed_status.get("status") in _terminal
                                    and not closed_status.get("windows"))
            source_dead_after_close = not backend.source_alive(a)

        # --- verdict ----------------------------------------------------------
        managed_ok = bool(res.ok and managed_status.get("windows")
                          and viewer_stream_id
                          and viewer_stream_id == announced_stream_id)
        detach_ok = (detach_reason == "viewer-eof" and not detach_emitted_closed
                     and source_alive_after_detach and fresh_approval)
        closed_ok = (source_closed_emitted and viewer_proxy_removed
                     and source_dead_after_close)
        passed = managed_ok and detach_ok and closed_ok
        if passed:
            bundle.assert_remote_proof()       # honesty gate
        bundle.manifest.passed = passed
        bundle.manifest.notes.append(
            "VM-A-SERVED control (impl-12): the Announce/Closed bytes originate in "
            f"VM-A's mm-control unit (stream_id {announced_stream_id} minted "
            "in-guest); NO host ControlServer. Source-driven Closed on marker "
            "death proves VM-A owns the lifecycle. Stale-generation rejection: "
            "covered by RemoteViewerState unit tests + prior session-4 live gate.")
        bundle.write()
        return VmaManagedSliceResult(
            bundle, res, managed_status, announced_stream_id, viewer_stream_id,
            host_served_control=False, detach_reason=detach_reason,
            detach_emitted_closed=detach_emitted_closed,
            source_alive_after_detach=source_alive_after_detach,
            fresh_approval=fresh_approval,
            source_closed_emitted=source_closed_emitted,
            viewer_proxy_removed=viewer_proxy_removed,
            source_dead_after_close=source_dead_after_close, passed=passed)
    finally:
        backend.stop_viewer(b)
        backend.stop_control(a)
        backend.clear_netem(a, topology.link_dev)
        backend.destroy(a)
        backend.destroy(b)


# ==========================================================================
# Scenario-3: step-8 input-confinement gate (codex impl-10)
# ==========================================================================
class InputConfinementBackend(ManagedVMBackend, Protocol):
    """:class:`ManagedVMBackend` plus the input-confinement operations (codex
    impl-10): bring up an EXPORTED marker (with per-seat input telemetry) + a
    LOCAL unexported SENTINEL marker and an ``--allow-input`` subscribe; inject
    input at the VM-B viewer (ydotool → sdl-freerdp → RDP → forward → per-stream
    seat); read each marker's telemetry."""

    def setup_confinement_source(
        self, vm: str, *, generation: int, width: int, height: int,
        exported_telemetry: str, sentinel_telemetry: str,
        exported_label: str, sentinel_label: str,
        allow_input: int = 1) -> ViewStreamApproved: ...

    def launch_sentinel(self, vm: str, *, generation: int,
                        sentinel_telemetry: str, sentinel_label: str) -> None: ...

    def read_telemetry(self, vm: str, path: str) -> dict: ...

    def inject_input(self, vm: str, *, x: int | None = None,
                     y: int | None = None,
                     absolute: bool = False) -> tuple[int, int]: ...


@dataclass
class InputConfinementResult:
    bundle: EvidenceBundle
    oracle: O.OracleResult
    exported_press_delta: int      # injected presses the EXPORTED marker received
    sentinel_press_delta: int      # injected presses the SENTINEL marker received (must be 0)
    exported_before: dict
    exported_after: dict
    sentinel_before: dict
    sentinel_after: dict
    passed: bool


def _press_total(telemetry: dict) -> int:
    """button_press + key_press across all seats (the confinement proof signal —
    codex impl-10: PRESS deltas, not enter)."""
    t = telemetry.get("totals", {}) if telemetry else {}
    return int(t.get("button_press", 0)) + int(t.get("key_press", 0))


def run_input_confinement_slice(
    backend: InputConfinementBackend, topology: Topology, *, generation: int,
    bundle_dir: Path | str, netem_profile: str = "lan-clean",
    width: int = 1280, height: int = 800, marker_output_id: int = 1,
    source_handle: int = 1, control_port: int = 5556,
    viewer_control_host: str = "10.0.2.2", viewer_rdp_host: str = "10.0.2.2",
    exported_telemetry: str = "/run/mm-a/exported.json",
    sentinel_telemetry: str = "/run/mm-a/sentinel.json",
) -> InputConfinementResult:
    """Drive the step-8 **input-confinement** gate (codex impl-10): prove input
    sent through the VM-B viewer reaches ONLY the exported source window, not a
    second local toplevel. The injection rides the ENTIRE shipped path — ydotool
    on VM-B → kiosk weston seat → ``sdl-freerdp`` → RDP → ``qdistro-forward`` →
    ``qdwin_stream_input_v1.inject_*`` → the source view's per-stream
    ``weston_seat`` (focus-locked to the exported marker) → the marker's
    ``wl_pointer``/``wl_keyboard``.

    Pass = the managed decoded oracle is ok AND the EXPORTED marker's press count
    increases AND the local SENTINEL marker's press count stays 0 (delta-based, so
    setup input never counts). Honesty: protocol/seat/process correctness only —
    the per-stream seat isolation is the shipped invariant under test.
    """
    profile(netem_profile)
    bundle = EvidenceBundle.create(
        bundle_dir, scenario="10-mm-input-confinement", step="static",
        generation=generation,
        topology=EvTopology(vms=[topology.vm_a, topology.vm_b],
                            netem_profile=netem_profile,
                            description="Phase-1 input-confinement gate"))

    a = backend.spin(topology.vm_a)
    b = backend.spin(topology.vm_b)
    control: ControlServer | None = None
    try:
        backend.apply_netem(a, topology.link_dev, netem_profile)

        # VM-A: exported marker (telemetry) + local sentinel (telemetry) +
        # an --allow-input subscribe so the forward gets the inject channel.
        approved = backend.setup_confinement_source(
            a, generation=generation, width=width, height=height,
            exported_telemetry=exported_telemetry,
            sentinel_telemetry=sentinel_telemetry,
            exported_label="exported", sentinel_label="sentinel")
        src = SourceWindowInfo(window_id=source_handle, source_machine=topology.vm_a,
                               title="marker", app_id="qdwin-marker-client",
                               req_w=width, req_h=height)
        announce = bridge_approved(approved, src, generation)

        control = ControlServer(port=control_port)
        backend.launch_viewer(
            b, control_host=viewer_control_host, control_port=control.port,
            rdp_host=viewer_rdp_host, rdp_port=approved.rdp_port,
            generation=generation, otp=approved.rdp_password,
            size=f"{width}x{height}", status_file="/run/mm-b/viewer-status.json")
        control.accept()
        control.send(announce)
        _poll(lambda: backend.viewer_status(b),
              lambda s: s.get("status") == "connected" and s.get("windows"))

        # the decoded oracle must pass first (the viewer really shows the marker).
        vm_b_screen = next(s.screen_index for s in topology.screens.screens
                           if s.vm == topology.vm_b)
        decoded = bundle.root / "captures" / "vm-b-confinement.ppm"
        decoded.parent.mkdir(parents=True, exist_ok=True)
        backend.await_decode(b)
        layout = M.compute_layout(width, height)
        res = O.OracleResult(ok=False, payload=None, payload_error="no capture")
        import time as _t
        for _ in range(8):
            backend.capture(b, vm_b_screen, decoded)
            res = O.evaluate(load_image(decoded), layout, 1.0, tol=O.TOL_RDP,
                             auto_origin=True, active_generation=generation,
                             expect_output_id=marker_output_id)
            if res.ok:
                break
            _t.sleep(0.4)
        cap = Capture(path=str(decoded.relative_to(bundle.root)),
                      capture_class=CaptureClass.VM_B_HOST.value,
                      output_id=marker_output_id,
                      role="VM-B monitor (input-confinement viewer)",
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

        # NOW launch the sentinel (a visible local toplevel that would overlap the
        # per-view capture — so only after the oracle has its clean exported frame).
        backend.launch_sentinel(a, generation=generation,
                                sentinel_telemetry=sentinel_telemetry,
                                sentinel_label="sentinel")

        # baseline both telemetry files, inject at VM-B, then poll for a delta.
        exported_before = backend.read_telemetry(a, exported_telemetry)
        sentinel_before = backend.read_telemetry(a, sentinel_telemetry)
        base_exp = _press_total(exported_before)
        backend.inject_input(b)
        exported_after = _poll(
            lambda: backend.read_telemetry(a, exported_telemetry),
            lambda tel: _press_total(tel) > base_exp, tries=40, delay=0.25)
        sentinel_after = backend.read_telemetry(a, sentinel_telemetry)

        exported_delta = _press_total(exported_after) - base_exp
        sentinel_delta = _press_total(sentinel_after) - _press_total(sentinel_before)

        # FAIL CLOSED on the sentinel (codex impl-11): a `sentinel_delta == 0` is
        # only a confinement proof if the sentinel was actually ALIVE, identified,
        # and binding seats — otherwise an absent/malformed/never-started sentinel
        # (read_telemetry → {} → press 0) would satisfy the negative-control half
        # for free. Require its telemetry to be well-formed, correctly labelled, and
        # to have seen at least one seat (so it COULD have received a leak).
        sentinel_valid = (
            sentinel_after.get("label") == "sentinel"
            and int(sentinel_after.get("seats_seen", 0) or 0) >= 1
            and isinstance(sentinel_after.get("totals"), dict))

        passed = bool(res.ok and exported_delta > 0 and sentinel_delta == 0
                      and sentinel_valid)
        if passed:
            bundle.assert_remote_proof()
        bundle.manifest.passed = passed
        bundle.manifest.notes.append(
            f"input confinement: exported press delta={exported_delta} (>0), "
            f"sentinel press delta={sentinel_delta} (==0), sentinel_valid="
            f"{sentinel_valid}. Injected end-to-end via ydotool→sdl-freerdp→RDP→"
            "qdistro-forward→per-stream seat.")
        bundle.write()
        return InputConfinementResult(
            bundle, res, exported_delta, sentinel_delta, exported_before,
            exported_after, sentinel_before, sentinel_after, passed)
    finally:
        if control is not None:
            control.close()
        backend.stop_viewer(b)
        backend.clear_netem(a, topology.link_dev)
        backend.destroy(a)
        backend.destroy(b)


# ==========================================================================
# Scenario-3b: read-only (allow_input=0) NEGATIVE CONTROL (codex impl-11/13)
# ==========================================================================
@dataclass
class NegativeControlResult:
    bundle: EvidenceBundle
    oracle: O.OracleResult
    exported_press_delta: int      # MUST be 0 (permission bit gated injection)
    sentinel_press_delta: int      # MUST be 0
    inject_attempted: bool         # the SAME end-to-end injection was driven
    exported_alive: bool           # the exported marker survived (0 isn't vacuous)
    exported_valid: bool           # its telemetry is well-formed (it really ran)
    exported_after: dict
    sentinel_after: dict
    passed: bool


def run_input_negative_control_slice(
    backend: InputConfinementBackend, topology: Topology, *, generation: int,
    bundle_dir: Path | str, netem_profile: str = "lan-clean",
    width: int = 1280, height: int = 800, marker_output_id: int = 1,
    source_handle: int = 1, control_port: int = 5556,
    viewer_control_host: str = "10.0.2.2", viewer_rdp_host: str = "10.0.2.2",
    exported_telemetry: str = "/run/mm-a/exported.json",
    sentinel_telemetry: str = "/run/mm-a/sentinel.json",
    settle: float = 6.0,
) -> NegativeControlResult:
    """Drive the **read-only negative control** for the input-confinement gate
    (codex impl-11/13): export the source with ``allow_input=0`` (NO
    ``--allow-input`` subscription), then attempt the IDENTICAL end-to-end
    injection (ydotool on VM-B → sdl-freerdp → RDP → ``qdistro-forward``) and assert
    that BOTH the exported marker AND the sentinel see **zero** injected presses —
    proving the source-side permission bit actually gates injection.

    Honesty: this is the negative half of the confinement claim and is interpreted
    ALONGSIDE the positive gate (same apparatus, ``allow_input=1`` → exported delta
    > 0). The fence here is fail-closed: a 0 delta is only meaningful if (a) the
    SAME injection was actually driven (``inject_attempted``), (b) the exported
    marker is ALIVE and (c) wrote well-formed telemetry — otherwise an absent/dead
    marker would satisfy "delta 0" for free. Protocol/permission correctness only.
    """
    profile(netem_profile)
    bundle = EvidenceBundle.create(
        bundle_dir, scenario="10-mm-input-negative-control", step="static",
        generation=generation,
        topology=EvTopology(vms=[topology.vm_a, topology.vm_b],
                            netem_profile=netem_profile,
                            description="Phase-1 read-only negative-control gate"))

    a = backend.spin(topology.vm_a)
    b = backend.spin(topology.vm_b)
    control: ControlServer | None = None
    try:
        backend.apply_netem(a, topology.link_dev, netem_profile)

        # read-only export: allow_input=0 → the forward gets NO inject channel.
        approved = backend.setup_confinement_source(
            a, generation=generation, width=width, height=height,
            exported_telemetry=exported_telemetry,
            sentinel_telemetry=sentinel_telemetry,
            exported_label="exported", sentinel_label="sentinel", allow_input=0)
        src = SourceWindowInfo(window_id=source_handle, source_machine=topology.vm_a,
                               title="marker", app_id="qdwin-marker-client",
                               req_w=width, req_h=height)
        announce = bridge_approved(approved, src, generation)

        control = ControlServer(port=control_port)
        backend.launch_viewer(
            b, control_host=viewer_control_host, control_port=control.port,
            rdp_host=viewer_rdp_host, rdp_port=approved.rdp_port,
            generation=generation, otp=approved.rdp_password,
            size=f"{width}x{height}", status_file="/run/mm-b/viewer-status.json")
        control.accept()
        control.send(announce)
        _poll(lambda: backend.viewer_status(b),
              lambda s: s.get("status") == "connected" and s.get("windows"))

        # the decoded oracle must pass (the viewer really shows the marker stream).
        vm_b_screen = next(s.screen_index for s in topology.screens.screens
                           if s.vm == topology.vm_b)
        decoded = bundle.root / "captures" / "vm-b-negative.ppm"
        decoded.parent.mkdir(parents=True, exist_ok=True)
        backend.await_decode(b)
        layout = M.compute_layout(width, height)
        res = O.OracleResult(ok=False, payload=None, payload_error="no capture")
        import time as _t
        for _ in range(8):
            backend.capture(b, vm_b_screen, decoded)
            res = O.evaluate(load_image(decoded), layout, 1.0, tol=O.TOL_RDP,
                             auto_origin=True, active_generation=generation,
                             expect_output_id=marker_output_id)
            if res.ok:
                break
            _t.sleep(0.4)
        cap = Capture(path=str(decoded.relative_to(bundle.root)),
                      capture_class=CaptureClass.VM_B_HOST.value,
                      output_id=marker_output_id,
                      role="VM-B monitor (read-only negative control)",
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

        # launch the sentinel (after the oracle), baseline, attempt injection.
        backend.launch_sentinel(a, generation=generation,
                                sentinel_telemetry=sentinel_telemetry,
                                sentinel_label="sentinel")
        exported_before = backend.read_telemetry(a, exported_telemetry)
        sentinel_before = backend.read_telemetry(a, sentinel_telemetry)
        base_exp = _press_total(exported_before)
        base_sen = _press_total(sentinel_before)
        inject_attempted = False
        try:
            backend.inject_input(b)            # the SAME path as the positive gate
            inject_attempted = True
        except Exception:                      # noqa: BLE001 — recorded as a failure
            inject_attempted = False
        # give any (wrongly) delivered press time to land, then read once. Unlike
        # the positive gate we cannot poll-for-a-delta (we EXPECT none), so we
        # settle a fixed window — long enough that a real leak would have landed.
        _t.sleep(settle)
        exported_after = backend.read_telemetry(a, exported_telemetry)
        sentinel_after = backend.read_telemetry(a, sentinel_telemetry)

        exported_delta = _press_total(exported_after) - base_exp
        sentinel_delta = _press_total(sentinel_after) - base_sen
        exported_alive = backend.source_alive(a)
        # the exported marker really ran (fail-closed: a dead/absent marker that
        # never wrote telemetry must NOT pass a "delta 0" for free).
        exported_valid = isinstance(exported_after.get("totals"), dict)

        passed = bool(res.ok and inject_attempted and exported_alive
                      and exported_valid and exported_delta == 0
                      and sentinel_delta == 0)
        if passed:
            bundle.assert_remote_proof()
        bundle.manifest.passed = passed
        bundle.manifest.notes.append(
            f"read-only negative control (allow_input=0): exported press delta="
            f"{exported_delta} (==0), sentinel press delta={sentinel_delta} (==0), "
            f"inject_attempted={inject_attempted}, exported_alive={exported_alive}. "
            "The SAME ydotool→sdl-freerdp→RDP→qdistro-forward injection as the "
            "positive confinement gate was driven; with no --allow-input "
            "subscription the forward gets no inject channel, so NOTHING receives "
            "the presses. Proves the source-side permission bit gates injection.")
        bundle.write()
        return NegativeControlResult(
            bundle, res, exported_delta, sentinel_delta, inject_attempted,
            exported_alive, exported_valid, exported_after, sentinel_after, passed)
    finally:
        if control is not None:
            control.close()
        backend.stop_viewer(b)
        backend.clear_netem(a, topology.link_dev)
        backend.destroy(a)
        backend.destroy(b)


# ==========================================================================
# Scenario-3d: compositor-boundary DIRECT-CLAIMANT gate (A1, session 7)
# ==========================================================================
class DirectClaimantBackend(InputConfinementBackend, Protocol):
    """:class:`InputConfinementBackend` plus the direct-claimant op (A1): bring up
    a single-VM source where qdwin spawns ``qdwin-stream-claimant`` in place of
    ``qdistro-forward`` (the trusted ``QDWIN_FORWARD_BIN`` seam), claims the
    per-stream token, and drives ``qdwin_stream_input_v1`` inject directly."""

    def setup_claimant_source(
        self, vm: str, *, generation: int, width: int, height: int,
        exported_telemetry: str, sentinel_telemetry: str,
        exported_label: str, sentinel_label: str) -> dict: ...

    def read_claimant_status(self, vm: str, path: str) -> dict: ...


@dataclass
class DirectClaimantResult:
    bundle: EvidenceBundle
    # claimant self-report (fail-closed witness that the claim path ran):
    claim_real: bool          # claim(real token) succeeded
    already_claimed: bool     # 2nd claim of the same token → already_claimed error
    invalid_token: bool       # claim(bogus token) → invalid_token error
    inject_sent: bool         # motion+button injected on the live handle
    # the real proof (marker telemetry, NOT the claimant's word):
    exported_press_delta: int # presses the EXPORTED marker received (must be > 0)
    sentinel_press_delta: int # presses the SENTINEL received (must be 0)
    pressed_seat_name: str | None   # wl_seat.name that got the press
    expected_seat_name: str         # "qdwin-stream-<rdp_port>" (the per-stream seat)
    seat_identity_ok: bool          # the press landed on the per-stream seat
    exported_alive: bool
    exported_valid: bool
    sentinel_alive: bool
    status: dict
    exported_after: dict
    sentinel_after: dict
    passed: bool


def _pressed_seat(telemetry: dict) -> dict | None:
    """The seat in a marker's telemetry that received an injected BUTTON press
    (the per-stream seat the claimant drove). Picks the seat with the most
    presses; ties broken by pointer motion (the injected motion rode the same
    seat)."""
    best = None
    for seat in (telemetry.get("seats") or []):
        if not isinstance(seat, dict):
            continue
        if int(seat.get("button_press", 0) or 0) > 0:
            if (best is None
                    or seat["button_press"] > best["button_press"]
                    or (seat["button_press"] == best["button_press"]
                        and int(seat.get("pointer_motion", 0) or 0)
                        > int(best.get("pointer_motion", 0) or 0))):
                best = seat
    return best


def run_direct_claimant_slice(
    backend: DirectClaimantBackend, topology: Topology, *, generation: int,
    bundle_dir: Path | str, width: int = 1280, height: int = 800,
    exported_telemetry: str = "/run/user/1000/exported.json",
    sentinel_telemetry: str = "/run/user/1000/sentinel.json",
) -> DirectClaimantResult:
    """Drive the **compositor-boundary direct-claimant** gate (A1, codex
    impl-17/impl-18). A SINGLE-VM gate: qdwin spawns ``qdwin-stream-claimant`` in
    place of ``qdistro-forward`` (the trusted ``QDWIN_FORWARD_BIN`` seam), so the
    real one-shot per-stream access token is claimed and ``inject_*`` is driven
    DIRECTLY against ``qdwin_stream_input_v1`` — with NO FreeRDP / RDP shadow
    server / remote viewer in the loop. A failure is therefore unambiguously
    compositor-side; this narrows the Phase-1 isolation claim to the compositor
    boundary the live two-VM gates prove end to end.

    The honesty here does NOT rest on the claimant's self-report. Because this is
    single-VM with NO other input path (no ydotool, no RDP, no production
    forward), the ONLY way the exported marker can register a BUTTON PRESS is the
    claimant's ``inject_pointer_button`` through the claimed per-stream handle —
    which qdwin routes exclusively through the source view's per-stream
    ``weston_seat``. So the gate asserts, all of:

      - the marker's per-seat PRESS delta > 0 AND it landed on the seat named
        ``qdwin-stream-<rdp_port>`` (the per-stream seat — the strongest fence
        that the event went through the stream handle, not an ambient seat);
      - the LOCAL sentinel's press delta == 0 (confinement), with the sentinel
        proven LIVE + writing well-formed telemetry (a dead/absent sentinel is a
        HARD FAIL, never a vacuous 0);
      - the negative protocol contract held: a 2nd claim of the same token →
        ``already_claimed``; a bogus token → ``invalid_token``;
      - the claimant reported a successful real claim + a sent inject.

    (The ``not_claimed`` error is unreachable by construction — an inject handle
    only exists after a successful claim, and a failed claim is a fatal protocol
    error — so it is documented, not asserted.) NOT a remote-monitor claim: this
    gate says nothing about what a peer screen shows, so ``assert_remote_proof``
    deliberately does not apply."""
    bundle = EvidenceBundle.create(
        bundle_dir, scenario="10-mm-direct-claimant", step="static",
        generation=generation,
        topology=EvTopology(vms=[topology.vm_a],
                            netem_profile="none",
                            description="Phase-1 compositor-boundary direct-claimant gate"))

    a = backend.spin(topology.vm_a)
    try:
        result = backend.setup_claimant_source(
            a, generation=generation, width=width, height=height,
            exported_telemetry=exported_telemetry,
            sentinel_telemetry=sentinel_telemetry,
            exported_label="exported", sentinel_label="sentinel")
        status = result.get("status") or {}
        rdp_port = int(result.get("rdp_port") or 0)

        exported_after = backend.read_telemetry(a, exported_telemetry)
        sentinel_after = backend.read_telemetry(a, sentinel_telemetry)

        claim_real = bool(status.get("claim_real"))
        already_claimed = bool(status.get("already_claimed"))
        invalid_token = bool(status.get("invalid_token"))
        inject_sent = bool(status.get("inject_sent"))

        # The marker is fresh and the claimant is the ONLY input source, so its
        # press total IS the injected-press delta from a 0 baseline.
        exported_delta = _press_total(exported_after)
        sentinel_delta = _press_total(sentinel_after)

        pressed = _pressed_seat(exported_after)
        pressed_seat_name = (pressed.get("seat_name") if pressed else None) or None
        expected_seat_name = f"qdwin-stream-{rdp_port}" if rdp_port else ""
        seat_identity_ok = bool(
            pressed_seat_name and expected_seat_name
            and pressed_seat_name == expected_seat_name)

        exported_alive = backend.source_alive(a)
        exported_valid = isinstance(exported_after.get("totals"), dict)
        # the sentinel must be a REAL, live, telemetry-writing client for its zero
        # to mean anything (fail-closed; codex impl-18).
        sentinel_valid = (isinstance(sentinel_after.get("totals"), dict)
                          and int(sentinel_after.get("seats_seen", 0) or 0) >= 1)
        sentinel_alive = sentinel_valid

        passed = bool(
            claim_real and already_claimed and invalid_token and inject_sent
            and exported_alive and exported_valid and exported_delta > 0
            and seat_identity_ok
            and sentinel_alive and sentinel_valid and sentinel_delta == 0)

        bundle.manifest.passed = passed
        bundle.manifest.notes.append(
            f"compositor-boundary direct-claimant gate (A1): qdwin spawned "
            f"qdwin-stream-claimant in place of qdistro-forward; it claimed the "
            f"real per-stream token and injected motion+button DIRECTLY via "
            f"qdwin_stream_input_v1 (no FreeRDP/RDP/viewer). exported press delta="
            f"{exported_delta} (>0) on seat '{pressed_seat_name}' "
            f"(expected '{expected_seat_name}', match={seat_identity_ok}); sentinel "
            f"press delta={sentinel_delta} (==0, alive={sentinel_alive}); negatives: "
            f"already_claimed={already_claimed}, invalid_token={invalid_token}; "
            f"claim_real={claim_real}, inject_sent={inject_sent}, "
            f"exported_alive={exported_alive}. Narrows the per-stream input-seat "
            "isolation claim to the compositor boundary.")
        bundle.write()
        return DirectClaimantResult(
            bundle, claim_real, already_claimed, invalid_token, inject_sent,
            exported_delta, sentinel_delta, pressed_seat_name, expected_seat_name,
            seat_identity_ok, exported_alive, exported_valid, sentinel_alive,
            status, exported_after, sentinel_after, passed)
    finally:
        backend.destroy(a)


# ==========================================================================
# Scenario-3c: coordinate-fidelity gate (codex impl-11 deferred; session 6)
# ==========================================================================
@dataclass
class CoordinateFidelityResult:
    bundle: EvidenceBundle
    oracle: O.OracleResult
    # three AXIS-ALIGNED probe pixels (P2 shares P1's y, P3 shares P1's x) and where
    # each landed in the marker's surface space — so we measure per-axis scale AND
    # cross-axis shear, not just a single diagonal (codex impl-16).
    p1: tuple[int, int]
    m1: tuple[int, int]
    p2: tuple[int, int]
    m2: tuple[int, int]
    p3: tuple[int, int]
    m3: tuple[int, int]
    x_scale: float           # m = scale·p + offset, fitted per axis
    y_scale: float
    offset_x: float
    offset_y: float
    cross_x_shear: float     # Δx from a pure-Δy move (should be ~0: no y→x leak)
    cross_y_shear: float     # Δy from a pure-Δx move (should be ~0: no x→y leak)
    offset_tol: float
    scale_tol: float
    shear_tol: float
    seats_found: bool        # all three injections produced a per-stream seat reading
    passed: bool


def _injected_seat(telemetry: dict) -> dict | None:
    """The per-stream seat in a marker's telemetry that received injected pointer
    MOTION (the forward's seat) — its ``last_x``/``last_y`` is where the injected
    pointer landed in the exported marker's surface-local space. Picks the seat with
    the most motion (the local seat, if any, never moves)."""
    best = None
    for seat in (telemetry.get("seats") or []):
        if not isinstance(seat, dict):
            continue
        if int(seat.get("pointer_motion", 0) or 0) > 0:
            if best is None or seat["pointer_motion"] > best["pointer_motion"]:
                best = seat
    return best


def run_input_coordinate_fidelity_slice(
    backend: InputConfinementBackend, topology: Topology, *, generation: int,
    bundle_dir: Path | str, netem_profile: str = "lan-clean",
    width: int = 1280, height: int = 800, marker_output_id: int = 1,
    source_handle: int = 1, control_port: int = 5556,
    viewer_control_host: str = "10.0.2.2", viewer_rdp_host: str = "10.0.2.2",
    exported_telemetry: str = "/run/mm-a/exported.json",
    sentinel_telemetry: str = "/run/mm-a/sentinel.json",
    p1: tuple[int, int] | None = None, p2: tuple[int, int] | None = None,
    p3: tuple[int, int] | None = None,
    offset_tol: float = 24.0, scale_tol: float = 0.2, shear_tol: float = 24.0,
) -> CoordinateFidelityResult:
    """Drive the **coordinate-fidelity** gate (codex impl-11 deferred): inject the
    pointer at TWO known viewer pixels and assert the source window receives them as
    a FAITHFUL LINEAR map — zero offset, isotropic scale — validating the whole
    coordinate path (ydotool → fullscreen 1:1 sdl-freerdp → RDP framebuffer →
    ``qdistro-forward`` → ``qdwin_stream_input_v1.inject_pointer_motion`` →
    ``weston_coord_surface_to_global`` → the marker's ``wl_pointer.motion``).

    THREE AXIS-ALIGNED points (not one, not a single diagonal) because the
    END-TO-END map is ``m = scale·p + offset`` and we must SEPARATE the product
    transform's fidelity from the test apparatus's fixed scale: ydotool's
    ``--absolute`` uinput injection lands at a constant multiple of the requested
    pixel (measured ~2.0× on the live rig — a uinput axis-range artifact, NOT a
    product bug). P2 shares P1's y (varies x only) and P3 shares P1's x (varies y
    only), so we measure ``x_scale``/``y_scale`` independently AND the cross-axis
    shear (a pure-x move must not change y, and vice-versa). We then assert the
    PRODUCT properties: ``offset ≈ 0`` (no translation), ``x_scale ≈ y_scale``
    (isotropic), both scales non-degenerate, and ``cross shear ≈ 0`` (no skew / axis
    swap / cross-axis leak). A diagonal-only pair could be fooled by a shear that is
    correct on that line; the axis-aligned triple cannot. The uniform apparatus
    scale is allowed (Phase-1 scope: this is faithful-linear coordinate fidelity up
    to a uniform scale, not an absolute-scale calibration).

    Honesty: protocol/transform correctness only. Fail-closed — if any injection
    yields no per-stream seat reading, FAIL; the decoded oracle is still a gate.
    """
    profile(netem_profile)
    # three axis-aligned points, all in-bounds AFTER the ~2× apparatus scale:
    # P1=(160,100), P2=(480,100) [same y, varies x], P3=(160,300) [same x, varies y].
    p1 = (width // 8, height // 8) if p1 is None else p1
    p2 = (3 * width // 8, height // 8) if p2 is None else p2
    p3 = (width // 8, 3 * height // 8) if p3 is None else p3
    bundle = EvidenceBundle.create(
        bundle_dir, scenario="10-mm-input-coordinate-fidelity", step="static",
        generation=generation,
        topology=EvTopology(vms=[topology.vm_a, topology.vm_b],
                            netem_profile=netem_profile,
                            description="Phase-1 input coordinate-fidelity gate"))

    a = backend.spin(topology.vm_a)
    b = backend.spin(topology.vm_b)
    control: ControlServer | None = None
    try:
        backend.apply_netem(a, topology.link_dev, netem_profile)

        # input-capable export (allow_input=1) — we need injection to actually reach
        # the per-stream seat so its last_x/last_y is meaningful.
        approved = backend.setup_confinement_source(
            a, generation=generation, width=width, height=height,
            exported_telemetry=exported_telemetry,
            sentinel_telemetry=sentinel_telemetry,
            exported_label="exported", sentinel_label="sentinel", allow_input=1)
        src = SourceWindowInfo(window_id=source_handle, source_machine=topology.vm_a,
                               title="marker", app_id="qdwin-marker-client",
                               req_w=width, req_h=height)
        announce = bridge_approved(approved, src, generation)

        control = ControlServer(port=control_port)
        backend.launch_viewer(
            b, control_host=viewer_control_host, control_port=control.port,
            rdp_host=viewer_rdp_host, rdp_port=approved.rdp_port,
            generation=generation, otp=approved.rdp_password,
            size=f"{width}x{height}", status_file="/run/mm-b/viewer-status.json")
        control.accept()
        control.send(announce)
        _poll(lambda: backend.viewer_status(b),
              lambda s: s.get("status") == "connected" and s.get("windows"))

        # decoded oracle (the viewer really shows the marker, geometry 1:1).
        vm_b_screen = next(s.screen_index for s in topology.screens.screens
                           if s.vm == topology.vm_b)
        decoded = bundle.root / "captures" / "vm-b-coordfidelity.ppm"
        decoded.parent.mkdir(parents=True, exist_ok=True)
        backend.await_decode(b)
        layout = M.compute_layout(width, height)
        res = O.OracleResult(ok=False, payload=None, payload_error="no capture")
        import time as _t
        for _ in range(8):
            backend.capture(b, vm_b_screen, decoded)
            res = O.evaluate(load_image(decoded), layout, 1.0, tol=O.TOL_RDP,
                             auto_origin=True, active_generation=generation,
                             expect_output_id=marker_output_id)
            if res.ok:
                break
            _t.sleep(0.4)
        cap = Capture(path=str(decoded.relative_to(bundle.root)),
                      capture_class=CaptureClass.VM_B_HOST.value,
                      output_id=marker_output_id,
                      role="VM-B monitor (coordinate-fidelity viewer)",
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

        # inject at the THREE axis-aligned pixels (ABSOLUTE — bypass libinput pointer
        # acceleration), reading the per-stream seat coordinate after each. The
        # seat's motion count strictly increases per injection; we BASELINE it before
        # EACH inject and poll for the increment before reading last_x/last_y, so a
        # stale/pre-existing motion can't be mistaken for an injection (impl-16).
        def _seat_motion(tel: dict) -> int:
            s = _injected_seat(tel)
            return int(s.get("pointer_motion", 0) or 0) if s else 0

        def _inject_read(px: int, py: int):
            base = _seat_motion(backend.read_telemetry(a, exported_telemetry))
            backend.inject_input(b, x=px, y=py, absolute=True)
            tel = _poll(lambda: backend.read_telemetry(a, exported_telemetry),
                        lambda t: _seat_motion(t) > base, tries=40, delay=0.25)
            seat = _injected_seat(tel)
            if seat is None:
                return None
            return (int(seat.get("last_x", 0)), int(seat.get("last_y", 0)))

        m1 = _inject_read(p1[0], p1[1])
        m2 = _inject_read(p2[0], p2[1])
        m3 = _inject_read(p3[0], p3[1])
        seats_found = None not in (m1, m2, m3)
        mm1, mm2, mm3 = (m1 or (0, 0)), (m2 or (0, 0)), (m3 or (0, 0))
        # P2 varies x only (same y as P1): fit x_scale + the x→y cross shear.
        # P3 varies y only (same x as P1): fit y_scale + the y→x cross shear.
        fittable = seats_found and p2[0] != p1[0] and p3[1] != p1[1]
        if fittable:
            x_scale = (mm2[0] - mm1[0]) / (p2[0] - p1[0])
            y_scale = (mm3[1] - mm1[1]) / (p3[1] - p1[1])
            offset_x = mm1[0] - x_scale * p1[0]
            offset_y = mm1[1] - y_scale * p1[1]
            cross_y_shear = mm2[1] - mm1[1]    # pure-x move changed y? (should be 0)
            cross_x_shear = mm3[0] - mm1[0]    # pure-y move changed x? (should be 0)
        else:
            x_scale = y_scale = offset_x = offset_y = 0.0
            cross_x_shear = cross_y_shear = 10 ** 9

        passed = bool(
            res.ok and fittable
            and abs(offset_x) <= offset_tol and abs(offset_y) <= offset_tol
            and abs(x_scale - y_scale) <= scale_tol
            and x_scale > 0.1 and y_scale > 0.1          # both axes non-degenerate
            and abs(cross_x_shear) <= shear_tol
            and abs(cross_y_shear) <= shear_tol)
        if passed:
            bundle.assert_remote_proof()
        bundle.manifest.passed = passed
        bundle.manifest.notes.append(
            f"coordinate fidelity: p1={p1}→{mm1}, p2={p2}→{mm2}, p3={p3}→{mm3}; "
            f"x_scale={x_scale:.3f} y_scale={y_scale:.3f} "
            f"offset=({offset_x:.1f},{offset_y:.1f}) "
            f"cross_shear=(x:{cross_x_shear:.1f},y:{cross_y_shear:.1f}); "
            f"tol offset={offset_tol} scale={scale_tol} shear={shear_tol}. Proves "
            "the product coordinate transform is a faithful linear map (zero offset, "
            "isotropic, no cross-axis shear) up to the apparatus's uinput scale.")
        bundle.write()
        return CoordinateFidelityResult(
            bundle, res, tuple(p1), mm1, tuple(p2), mm2, tuple(p3), mm3,
            x_scale, y_scale, offset_x, offset_y, cross_x_shear, cross_y_shear,
            offset_tol, scale_tol, shear_tol, seats_found, passed)
    finally:
        if control is not None:
            control.close()
        backend.stop_viewer(b)
        backend.clear_netem(a, topology.link_dev)
        backend.destroy(a)
        backend.destroy(b)


# ==========================================================================
# Scenario-3e: ABSOLUTE-pixel coordinate calibration gate (A2, codex impl-21)
# ==========================================================================
class AbsoluteCoordinateBackend(InputConfinementBackend, Protocol):
    """:class:`InputConfinementBackend` plus the VM-B calibration probe (A2): a
    fullscreen kiosk-side marker that measures the ydotool→kiosk-pointer apparatus
    map INDEPENDENTLY of the RDP/source path."""

    def setup_calibration_probe(self, vm: str, *, generation: int,
                                telemetry: str = "/run/mm-b/calib-probe.json",
                                label: str = "calib") -> str: ...

    def stop_calibration_probe(self, vm: str) -> None: ...


@dataclass
class AbsoluteCoordinateResult:
    bundle: EvidenceBundle
    oracle: O.OracleResult
    points: list                  # injected viewer pixels p_i
    k: list                       # measured apparatus coords T_apparatus(p_i) (calib)
    m: list                       # source-received coords (product phase)
    calib_scale_x: float          # fitted apparatus affine (SANITY check only)
    calib_scale_y: float
    calib_offset_x: float
    calib_offset_y: float
    calib_cross_xy: float         # |b|/|a| (px-y leaking into kx) — should be ~0
    calib_cross_yx: float         # |d|/|e| (px-x leaking into ky) — should be ~0
    calib_residual_max: float
    calib_repeat_dev: float       # repeated-center reproduced k_center within?
    calib_ok: bool
    product_max_err: float        # max per-point |m_i - k_i| (the absolute assertion)
    product_rms_err: float
    product_ok: bool
    passed: bool


def run_absolute_coordinate_slice(
    backend: AbsoluteCoordinateBackend, topology: Topology, *, generation: int,
    bundle_dir: Path | str, netem_profile: str = "lan-clean",
    width: int = 1280, height: int = 800, marker_output_id: int = 1,
    source_handle: int = 1, control_port: int = 5556,
    viewer_control_host: str = "10.0.2.2", viewer_rdp_host: str = "10.0.2.2",
    exported_telemetry: str = "/run/mm-a/exported.json",
    sentinel_telemetry: str = "/run/mm-a/sentinel.json",
    calib_telemetry: str = "/run/mm-b/calib-probe.json",
    scale_lo: float = 1.5, scale_hi: float = 2.5, calib_resid_tol: float = 3.0,
    calib_cross_tol: float = 0.02, calib_offset_tol: float = 8.0,
    calib_repeat_tol: float = 3.0, product_tol: float = 4.0,
    product_rms_tol: float = 2.5,
) -> AbsoluteCoordinateResult:
    """Drive the **absolute-pixel coordinate calibration** gate (A2, codex impl-21).
    Turns the coordinate proof from "faithful-linear UP TO a uniform ~2× apparatus
    scale" (which could LAUNDER a product-side uniform-scale bug as apparatus scale)
    into an ABSOLUTE-pixel assertion, by measuring the apparatus map INDEPENDENTLY.

    Two phases on the SAME kiosk-weston geometry:

    - **Calibration:** a fullscreen kiosk-side marker probe on VM-B (NO sdl-freerdp /
      RDP / source in the path). Inject ``ydotool --absolute`` at N in-bounds points
      and read the probe's received coords ``k_i = T_apparatus(p_i)`` — the
      ydotool→uinput→kiosk-pointer apparatus, measured directly. A full affine is
      fitted as a SANITY check (near-zero cross terms, scale in range, ~0 offset,
      small residual); a repeated centre re-reproduces ``k_centre`` (drift/stale
      control). The probe is torn down before the product phase (phase isolation).
    - **Product:** the real viewer (sdl-freerdp) + exported source marker. Inject the
      SAME points and read the source's per-stream-seat coords ``m_i``.

    **Assertion:** ``max_i |m_i - k_i| <= product_tol`` (compared against the
    MEASURED ``k_i`` per point, NOT a refit affine — a refit would re-introduce the
    laundering). Because the decoded oracle proves RDP renders 1:1 (kiosk px == viewer
    px == source-view px), ``m_i == k_i`` is the absolute-identity target; a
    product-side scale/offset/shear deviates from the independently-measured ``k_i``
    and FAILS.

    Contract scope (codex impl-21): the calibration removes ONLY the ydotool/kiosk
    apparatus. The product phase's ``m_i`` rides ``sdl-freerdp``'s RDP-input mapping
    too, so this gate validates the deployed **RDP-client→source input path as a
    whole** (the blame domain includes sdl-freerdp's input coordinate handling, not
    just qdistro-forward). The decoded oracle proves RENDER 1:1, not input 1:1."""
    import numpy as _np
    profile(netem_profile)
    W, H = width, height
    # Points in INJECTABLE p-space: k≈2p must stay in-bounds, so p in ~[W/16,7W/16]
    # (k spans ~[W/8,7W/8] — most of the surface, with margin). center + 4 near-corners
    # + 4 edge-mids (codex impl-21/22), spread wide so a LOCALIZED product bug outside
    # a narrow patch can't slip through.
    pts = [
        (W // 4, H // 4),                                   # centre (k≈W/2,H/2)
        (W // 16, H // 16), (7 * W // 16, H // 16),         # top corners (k≈W/8..7W/8)
        (W // 16, 7 * H // 16), (7 * W // 16, 7 * H // 16), # bottom corners
        (W // 4, H // 16), (W // 4, 7 * H // 16),           # top/bottom edge-mid
        (W // 16, H // 4), (7 * W // 16, H // 4),           # left/right edge-mid
    ]
    bundle = EvidenceBundle.create(
        bundle_dir, scenario="10-mm-absolute-coordinate", step="static",
        generation=generation,
        topology=EvTopology(vms=[topology.vm_a, topology.vm_b],
                            netem_profile=netem_profile,
                            description="Phase-1 absolute-pixel coordinate calibration"))

    def _inject_read(vm: str, telemetry: str, px: int, py: int):
        """Inject ABSOLUTE at (px,py) on VM-B and read the injected seat's landing
        coord from `telemetry`, with motion-freshness so a stale read can't pass."""
        def _motion(tel):
            s = _injected_seat(tel)
            return int(s.get("pointer_motion", 0) or 0) if s else 0
        base = _motion(backend.read_telemetry(vm, telemetry))
        backend.inject_input(topology.vm_b, x=px, y=py, absolute=True)
        tel = _poll(lambda: backend.read_telemetry(vm, telemetry),
                    lambda t: _motion(t) > base, tries=40, delay=0.25)
        seat = _injected_seat(tel)
        if seat is None:
            return None
        return (int(seat.get("last_x", 0)), int(seat.get("last_y", 0)))

    a = backend.spin(topology.vm_a)
    b = backend.spin(topology.vm_b)
    control: ControlServer | None = None
    calib_up = False
    try:
        backend.apply_netem(a, topology.link_dev, netem_profile)

        # ---- Phase 1: CALIBRATION (kiosk probe, no RDP/source) ----
        backend.setup_calibration_probe(b, generation=generation,
                                        telemetry=calib_telemetry, label="calib")
        calib_up = True
        k: list = []
        for (px, py) in pts:                  # bail on the first dead seat (fail-closed)
            ki = _inject_read(b, calib_telemetry, px, py)
            k.append(ki)
            if ki is None:
                break
        all_calib = len(k) == len(pts) and None not in k
        k_repeat = (_inject_read(b, calib_telemetry, pts[0][0], pts[0][1])
                    if all_calib else None)            # centre again (drift control)
        backend.stop_calibration_probe(b)                # MUST precede the product
        calib_up = False                                 # phase (phase isolation)

        # geometry/bounds fence (codex impl-22): every measured apparatus coord must
        # land inside the kiosk output — a probe that mapped at the wrong size/output
        # would push k_i out of [0,W]x[0,H] and must not be trusted.
        in_bounds = all_calib and all(
            0 <= ki[0] <= W and 0 <= ki[1] <= H for ki in k)
        calib_seats_ok = all_calib and k_repeat is not None and in_bounds
        if calib_seats_ok:
            P = _np.array([[px, py, 1.0] for (px, py) in pts])
            Kx = _np.array([ki[0] for ki in k], dtype=float)
            Ky = _np.array([ki[1] for ki in k], dtype=float)
            cx, *_ = _np.linalg.lstsq(P, Kx, rcond=None)   # [a, b, c]
            cy, *_ = _np.linalg.lstsq(P, Ky, rcond=None)   # [d, e, f]
            a_, b_, c_ = cx
            d_, e_, f_ = cy
            resid = max(float(_np.max(_np.abs(P @ cx - Kx))),
                        float(_np.max(_np.abs(P @ cy - Ky))))
            cross_xy = abs(b_) / abs(a_) if a_ else 1e9     # py leaking into kx
            cross_yx = abs(d_) / abs(e_) if e_ else 1e9     # px leaking into ky
            repeat_dev = max(abs(k_repeat[0] - k[0][0]), abs(k_repeat[1] - k[0][1]))
        else:
            a_ = e_ = c_ = f_ = 0.0
            b_ = d_ = 0.0
            resid = 1e9
            cross_xy = cross_yx = 1e9
            repeat_dev = 1e9

        calib_ok = bool(
            calib_seats_ok
            and scale_lo <= a_ <= scale_hi and scale_lo <= e_ <= scale_hi
            and cross_xy <= calib_cross_tol and cross_yx <= calib_cross_tol
            and abs(c_) <= calib_offset_tol and abs(f_) <= calib_offset_tol
            and resid <= calib_resid_tol and repeat_dev <= calib_repeat_tol)

        # ---- Phase 2: PRODUCT (real viewer + source) ----
        approved = backend.setup_confinement_source(
            a, generation=generation, width=width, height=height,
            exported_telemetry=exported_telemetry,
            sentinel_telemetry=sentinel_telemetry,
            exported_label="exported", sentinel_label="sentinel", allow_input=1)
        src = SourceWindowInfo(window_id=source_handle, source_machine=topology.vm_a,
                               title="marker", app_id="qdwin-marker-client",
                               req_w=width, req_h=height)
        announce = bridge_approved(approved, src, generation)
        control = ControlServer(port=control_port)
        backend.launch_viewer(
            b, control_host=viewer_control_host, control_port=control.port,
            rdp_host=viewer_rdp_host, rdp_port=approved.rdp_port,
            generation=generation, otp=approved.rdp_password,
            size=f"{width}x{height}", status_file="/run/mm-b/viewer-status.json")
        control.accept()
        control.send(announce)
        _poll(lambda: backend.viewer_status(b),
              lambda s: s.get("status") == "connected" and s.get("windows"))

        vm_b_screen = next(s.screen_index for s in topology.screens.screens
                           if s.vm == topology.vm_b)
        decoded = bundle.root / "captures" / "vm-b-abscoord.ppm"
        decoded.parent.mkdir(parents=True, exist_ok=True)
        backend.await_decode(b)
        layout = M.compute_layout(width, height)
        res = O.OracleResult(ok=False, payload=None, payload_error="no capture")
        import time as _t
        for _ in range(8):
            backend.capture(b, vm_b_screen, decoded)
            res = O.evaluate(load_image(decoded), layout, 1.0, tol=O.TOL_RDP,
                             auto_origin=True, active_generation=generation,
                             expect_output_id=marker_output_id)
            if res.ok:
                break
            _t.sleep(0.4)
        cap = Capture(path=str(decoded.relative_to(bundle.root)),
                      capture_class=CaptureClass.VM_B_HOST.value,
                      output_id=marker_output_id,
                      role="VM-B monitor (absolute-coordinate product phase)",
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

        m: list = []
        for (px, py) in pts:                  # bail on the first dead seat (fail-closed)
            mi = _inject_read(a, exported_telemetry, px, py)
            m.append(mi)
            if mi is None:
                break
        product_seats_ok = len(m) == len(pts) and None not in m

        # ---- the ABSOLUTE assertion: m_i ≈ measured k_i (per-point) ----
        if product_seats_ok and calib_seats_ok:
            errs = [max(abs(mi[0] - ki[0]), abs(mi[1] - ki[1]))
                    for mi, ki in zip(m, k)]
            product_max_err = max(errs)
            product_rms_err = float(_np.sqrt(_np.mean(
                [((mi[0] - ki[0]) ** 2 + (mi[1] - ki[1]) ** 2) / 2.0
                 for mi, ki in zip(m, k)])))
        else:
            product_max_err = product_rms_err = 1e9
        product_in_bounds = product_seats_ok and all(
            0 <= mi[0] <= W and 0 <= mi[1] <= H for mi in m)
        product_ok = bool(product_seats_ok and product_in_bounds
                          and product_max_err <= product_tol
                          and product_rms_err <= product_rms_tol)

        passed = bool(res.ok and calib_ok and product_ok)
        if passed:
            bundle.assert_remote_proof()
        bundle.manifest.passed = passed
        bundle.manifest.notes.append(
            f"absolute coordinate calibration (A2): apparatus scale=("
            f"{a_:.3f},{e_:.3f}) offset=({c_:.1f},{f_:.1f}) cross=("
            f"{cross_xy:.4f},{cross_yx:.4f}) resid_max={resid:.2f} "
            f"repeat_dev={repeat_dev:.1f} calib_ok={calib_ok}; PRODUCT vs MEASURED "
            f"apparatus: max_err={product_max_err:.2f}px rms={product_rms_err:.2f}px "
            f"(tol max={product_tol} rms={product_rms_tol}) product_ok={product_ok}. "
            "Proves the deployed RDP-client→source input path lands at the ABSOLUTE "
            "intended pixel after removing the independently-measured apparatus scale "
            "— a product-side uniform-scale bug can no longer be laundered as "
            "apparatus scale.")
        bundle.write()
        return AbsoluteCoordinateResult(
            bundle, res, [tuple(p) for p in pts], k, m,
            float(a_), float(e_), float(c_), float(f_),
            float(cross_xy), float(cross_yx), float(resid), float(repeat_dev),
            calib_ok, float(product_max_err), float(product_rms_err), product_ok,
            passed)
    finally:
        if calib_up:                          # guarantee phase-1 teardown on any
            backend.stop_calibration_probe(b)  # exceptional exit (codex impl-22)
        if control is not None:
            control.close()
        backend.stop_viewer(b)
        backend.clear_netem(a, topology.link_dev)
        backend.destroy(a)
        backend.destroy(b)


# ==========================================================================
# Scenario-3d: 2nd-EXPORTED-view (A→B) input isolation gate (codex impl-15)
# ==========================================================================
class SecondViewBackend(InputConfinementBackend, Protocol):
    """:class:`InputConfinementBackend` plus a SECOND concurrent input-capable
    export on the same qdwin (codex impl-15): ``setup_second_export`` brings up
    marker-B on a distinct output with its own bystander/forward/relay."""

    def setup_second_export(self, vm: str, *, generation: int, width: int,
                            height: int, output_id: int, telemetry: str,
                            label: str, relay_port: int,
                            allow_input: int = 1) -> ViewStreamApproved: ...

    def marker2_alive(self, vm: str) -> bool: ...


class ForwardDeathBackend(SecondViewBackend, Protocol):
    """:class:`SecondViewBackend` plus the forward-death-watch probes (item 5,
    codex impl-26): enumerate/kill ``qdistro-forward`` children, prove a killed
    one leaves no zombie, and read the subscriber log + qdwin journal + per-marker
    unit liveness."""

    def forward_pids(self, vm: str) -> dict[int, int]: ...
    def kill_forward(self, vm: str, pid: int) -> None: ...
    def pid_reaped(self, vm: str, pid: int) -> bool: ...
    def bystander_log(self, vm: str) -> str: ...
    def qdwin_journal(self, vm: str, tail: int = 80) -> str: ...
    def marker_unit_alive(self, vm: str, unit: str = "mm-marker") -> bool: ...


@dataclass
class ForwardDeathResult:
    bundle: EvidenceBundle
    oracle: O.OracleResult            # stream-A decoded oracle (a real LIVE stream pre-kill)
    handle_a: int                     # subscriber handle for stream A
    handle_b: int                     # subscriber handle for stream B
    torn_down_a: bool                 # subscriber got torn_down(handle A, "forward exited")
    torn_down_b_absent: bool          # NO torn_down for handle B (only A's stream died)
    forward_a_reaped: bool            # killed forward gone + NO zombie (weston reaped it)
    forward_b_alive: bool             # the OTHER forward untouched
    marker_a_alive: bool              # source app A survived transport death (process truth)
    marker_b_alive: bool              # source app B untouched
    qdwin_detected: bool              # mm-qdwin journal shows the compositor-side teardown
    slot_freed: bool                  # qdwin can mint a NEW stream after the failure
    passed: bool


@dataclass
class SecondViewIsolationResult:
    bundle: EvidenceBundle
    oracle: O.OracleResult            # stream-A decoded oracle (a real stream)
    marker_a_delta: int               # >0: viewer-A injection reached marker-A
    marker_b_positive_delta: int      # >0: marker-B's seat CAN deliver (phase B1)
    marker_b_isolation_delta: int     # ==0: viewer-A injection did NOT leak to B
    marker_b_reproof_delta: int       # >0: B's seat STILL delivers after isolation (B2)
    marker_b_alive: bool              # mm-marker2/mm-relay2 live through phase A
    marker_b_valid: bool              # B telemetry well-formed (label/output_id/seats)
    distinct_views: bool              # marker-A output_id != marker-B output_id
    passed: bool


def run_second_view_isolation_slice(
    backend: SecondViewBackend, topology: Topology, *, generation: int,
    bundle_dir: Path | str, netem_profile: str = "lan-clean",
    width: int = 1280, height: int = 800, marker_output_id: int = 1,
    second_output_id: int = 2, source_handle: int = 1, second_handle: int = 2,
    control_port: int = 5556, relay_b_port: int = 5560,
    viewer_control_host: str = "10.0.2.2", viewer_rdp_host: str = "10.0.2.2",
    exported_a_telemetry: str = "/run/mm-a/exported-a.json",
    exported_b_telemetry: str = "/run/mm-a/exported-b.json",
) -> SecondViewIsolationResult:
    """Drive the **2nd-exported-view (stream-A → stream-B) input isolation** gate
    (codex impl-15) — the deepest Phase-1 isolation claim: input injected at the
    viewer of one exported window must reach ONLY that window's per-stream seat,
    never a SECOND, concurrently-exported, input-capable window's seat.

    Two simultaneous input-capable exports from ONE qdwin (marker-A on output 1,
    marker-B on output 2, both ``allow_input=1``; each forward claims its inject
    channel on spawn, so BOTH per-stream seats are live throughout). One viewer at
    a time on VM-B (so ydotool always targets the intended stream — no two-fullscreen
    focus ambiguity); marker-B's seat persists across viewer-B teardown because
    forward-B keeps its claim:

    - **Phase B (positive control):** viewer-B decodes stream-B; inject → assert
      marker-B press delta > 0. This PROVES marker-B's per-stream seat can actually
      deliver presses (so a later 0 is meaningful, not vacuous). Tear viewer-B down.
    - **Phase A (isolation):** viewer-A decodes stream-A; the decoded oracle passes
      (a real stream); inject at viewer-A → assert marker-A press delta > 0 AND
      marker-B press delta == 0 (NO new presses since phase B). marker-B's seat is
      still live, so a cross-stream leak COULD have landed — it didn't.
    - **Phase B2 (re-proof):** bring viewer-B back and inject → assert marker-B
      delta > 0 AGAIN. This proves marker-B's per-stream seat SURVIVED the whole
      isolation phase, so the phase-A zero was true confinement and not a dead seat
      / stale telemetry (codex impl-16 HIGH fail-closed).

    Pass = oracle ok AND marker_a_delta>0 AND marker_b_positive_delta>0 AND
    marker_b_isolation_delta==0 AND marker_b_reproof_delta>0 AND marker_b_alive
    (mm-marker2/mm-relay2 live) AND marker_b_valid (well-formed B telemetry) AND
    distinct_views. Honesty: per-stream-seat isolation across two LIVE input-capable
    exports — protocol/seat correctness, PRESS deltas only.
    """
    profile(netem_profile)
    bundle = EvidenceBundle.create(
        bundle_dir, scenario="10-mm-second-view-isolation", step="static",
        generation=generation,
        topology=EvTopology(vms=[topology.vm_a, topology.vm_b],
                            netem_profile=netem_profile,
                            description="Phase-1 2nd-exported-view input isolation"))

    a = backend.spin(topology.vm_a)
    b = backend.spin(topology.vm_b)
    control: ControlServer | None = None
    status_file = "/run/mm-b/viewer-status.json"
    try:
        backend.apply_netem(a, topology.link_dev, netem_profile)

        # two concurrent input-capable exports from ONE qdwin.
        approved_a = backend.setup_confinement_source(
            a, generation=generation, width=width, height=height,
            exported_telemetry=exported_a_telemetry,
            sentinel_telemetry="/run/mm-a/unused-sentinel.json",
            exported_label="exported-a", sentinel_label="sentinel", allow_input=1)
        approved_b = backend.setup_second_export(
            a, generation=generation, width=width, height=height,
            output_id=second_output_id, telemetry=exported_b_telemetry,
            label="exported-b", relay_port=relay_b_port, allow_input=1)
        src_a = SourceWindowInfo(window_id=source_handle, source_machine=topology.vm_a,
                                 title="marker-a", app_id="qdwin-marker-client",
                                 req_w=width, req_h=height)
        src_b = SourceWindowInfo(window_id=second_handle, source_machine=topology.vm_a,
                                 title="marker-b", app_id="qdwin-marker-client",
                                 req_w=width, req_h=height)
        announce_a = bridge_approved(approved_a, src_a, generation)
        announce_b = bridge_approved(approved_b, src_b, generation)

        # ---- Phase B: positive control — marker-B's seat CAN deliver presses ---
        control = ControlServer(port=control_port)
        backend.launch_viewer(
            b, control_host=viewer_control_host, control_port=control.port,
            rdp_host=viewer_rdp_host, rdp_port=approved_b.rdp_port,
            generation=generation, otp=approved_b.rdp_password,
            size=f"{width}x{height}", status_file=status_file)
        control.accept()
        control.send(announce_b)
        _poll(lambda: backend.viewer_status(b),
              lambda s: s.get("status") == "connected" and s.get("windows"))
        backend.await_decode(b)
        b_before_pos = backend.read_telemetry(a, exported_b_telemetry)
        base_b_pos = _press_total(b_before_pos)
        backend.inject_input(b)
        b_after_pos = _poll(
            lambda: backend.read_telemetry(a, exported_b_telemetry),
            lambda t: _press_total(t) > base_b_pos, tries=40, delay=0.25)
        marker_b_positive_delta = _press_total(b_after_pos) - base_b_pos
        backend.stop_viewer(b)          # forward-B + marker-B's seat persist

        # ---- Phase A: isolation — inject viewer-A, marker-B must NOT move -------
        control.close()
        control = ControlServer(port=control_port)
        backend.launch_viewer(
            b, control_host=viewer_control_host, control_port=control.port,
            rdp_host=viewer_rdp_host, rdp_port=approved_a.rdp_port,
            generation=generation, otp=approved_a.rdp_password,
            size=f"{width}x{height}", status_file=status_file)
        control.accept()
        control.send(announce_a)
        _poll(lambda: backend.viewer_status(b),
              lambda s: s.get("status") == "connected" and s.get("windows"))

        vm_b_screen = next(s.screen_index for s in topology.screens.screens
                           if s.vm == topology.vm_b)
        decoded = bundle.root / "captures" / "vm-b-2ndview.ppm"
        decoded.parent.mkdir(parents=True, exist_ok=True)
        backend.await_decode(b)
        layout = M.compute_layout(width, height)
        res = O.OracleResult(ok=False, payload=None, payload_error="no capture")
        import time as _t
        for _ in range(8):
            backend.capture(b, vm_b_screen, decoded)
            res = O.evaluate(load_image(decoded), layout, 1.0, tol=O.TOL_RDP,
                             auto_origin=True, active_generation=generation,
                             expect_output_id=marker_output_id)
            if res.ok:
                break
            _t.sleep(0.4)
        cap = Capture(path=str(decoded.relative_to(bundle.root)),
                      capture_class=CaptureClass.VM_B_HOST.value,
                      output_id=marker_output_id,
                      role="VM-B monitor (2nd-exported-view isolation, stream-A)",
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

        a_before = backend.read_telemetry(a, exported_a_telemetry)
        b_before_iso = backend.read_telemetry(a, exported_b_telemetry)
        base_a = _press_total(a_before)
        base_b_iso = _press_total(b_before_iso)
        backend.inject_input(b)
        a_after = _poll(lambda: backend.read_telemetry(a, exported_a_telemetry),
                        lambda t: _press_total(t) > base_a, tries=40, delay=0.25)
        _t.sleep(4)                     # give any (wrong) leak to marker-B time to land
        b_after_iso = backend.read_telemetry(a, exported_b_telemetry)
        backend.stop_viewer(b)

        marker_a_delta = _press_total(a_after) - base_a
        marker_b_isolation_delta = _press_total(b_after_iso) - base_b_iso
        # marker-B liveness through phase A (codex impl-16 HIGH): a 0 isolation delta
        # is only meaningful if marker-B + its seat were LIVE the whole time — else
        # b_after_iso is just a stale file. Unit liveness + well-formed B telemetry.
        marker_b_alive = backend.marker2_alive(a)
        marker_b_valid = (b_after_iso.get("label") == "exported-b"
                          and int(b_after_iso.get("output_id", -2)) == second_output_id
                          and int(b_after_iso.get("seats_seen", 0) or 0) >= 1
                          and isinstance(b_after_iso.get("totals"), dict))

        # ---- Phase B2: RE-PROVE marker-B's seat still delivers AFTER isolation ---
        # the decisive liveness proof — bring viewer-B back and inject: if marker-B's
        # per-stream seat survived the whole isolation phase it presses again, so the
        # phase-A zero was true confinement, not a dead seat (codex impl-16 HIGH).
        control.close()
        control = ControlServer(port=control_port)
        backend.launch_viewer(
            b, control_host=viewer_control_host, control_port=control.port,
            rdp_host=viewer_rdp_host, rdp_port=approved_b.rdp_port,
            generation=generation, otp=approved_b.rdp_password,
            size=f"{width}x{height}", status_file=status_file)
        control.accept()
        control.send(announce_b)
        _poll(lambda: backend.viewer_status(b),
              lambda s: s.get("status") == "connected" and s.get("windows"))
        backend.await_decode(b)
        base_b_re = _press_total(backend.read_telemetry(a, exported_b_telemetry))
        backend.inject_input(b)
        b_after_re = _poll(
            lambda: backend.read_telemetry(a, exported_b_telemetry),
            lambda t: _press_total(t) > base_b_re, tries=40, delay=0.25)
        marker_b_reproof_delta = _press_total(b_after_re) - base_b_re

        distinct_views = (
            int(a_after.get("output_id", -1)) == marker_output_id
            and int(b_after_iso.get("output_id", -2)) == second_output_id
            and a_after.get("output_id") != b_after_iso.get("output_id"))

        passed = bool(res.ok and marker_a_delta > 0 and marker_b_positive_delta > 0
                      and marker_b_isolation_delta == 0 and marker_b_reproof_delta > 0
                      and marker_b_alive and marker_b_valid and distinct_views)
        if passed:
            bundle.assert_remote_proof()
        bundle.manifest.passed = passed
        bundle.manifest.notes.append(
            f"2nd-exported-view isolation: marker-A delta={marker_a_delta} (>0); "
            f"marker-B positive-control delta={marker_b_positive_delta} (>0, seat "
            f"deliverable); marker-B isolation delta={marker_b_isolation_delta} "
            f"(==0, viewer-A injection did NOT leak); marker-B re-proof delta="
            f"{marker_b_reproof_delta} (>0, its seat SURVIVED isolation — the 0 was "
            f"true confinement, not a dead seat); marker_b_alive={marker_b_alive}; "
            f"distinct_views={distinct_views}. Per-stream seat isolation proven "
            "across two concurrent live input-capable exports.")
        bundle.write()
        return SecondViewIsolationResult(
            bundle, res, marker_a_delta, marker_b_positive_delta,
            marker_b_isolation_delta, marker_b_reproof_delta, marker_b_alive,
            marker_b_valid, distinct_views, passed)
    finally:
        if control is not None:
            control.close()
        backend.stop_viewer(b)
        backend.clear_netem(a, topology.link_dev)
        backend.destroy(a)
        backend.destroy(b)


def _bystander_blocks(log: str) -> list[tuple[int, int]]:
    """Ordered ``(handle, dynamic_rdp_port)`` per approval from a qdwin-bystander
    log. Each approval prints a block ``HANDLE=<h> ... RDP_PORT=<p> ...`` where
    RDP_PORT is the **dynamic qdwin-allocated** port (= the forward's ``--rdp-port``
    and the port qdwin logs in its teardown line) — NOT the fixed relay port in the
    viewer's ``ViewStreamApproved``. Order is deterministic: the first export
    (stream A) precedes the second (stream B), so we attribute streams by position
    rather than by the relay port the harness happens to know."""
    blocks: list[tuple[int, int]] = []
    cur_handle: int | None = None
    for line in log.splitlines():
        line = line.strip()
        if line.startswith("HANDLE="):
            v = line[len("HANDLE="):].strip()
            cur_handle = int(v) if v.isdigit() else cur_handle
        elif line.startswith("RDP_PORT=") and cur_handle is not None:
            v = line[len("RDP_PORT="):].strip()
            if v.isdigit():
                blocks.append((cur_handle, int(v)))
                cur_handle = None
    return blocks


def _torn_down_handles(log: str, reason: str = "forward exited") -> set[int]:
    """Handles for which the subscriber logged ``torn_down ... reason="<reason>"``.
    Bystander format: ``view_stream torn_down handle=<h> reason="<r>"``."""
    out: set[int] = set()
    for line in log.splitlines():
        if "torn_down" not in line or f'reason="{reason}"' not in line:
            continue
        for tok in line.split():
            if tok.startswith("handle="):
                v = tok[len("handle="):]
                if v.isdigit():
                    out.add(int(v))
    return out


def run_forward_death_slice(
    backend: ForwardDeathBackend, topology: Topology, *, generation: int,
    bundle_dir: Path | str, netem_profile: str = "lan-clean",
    width: int = 1280, height: int = 800, marker_output_id: int = 1,
    second_output_id: int = 2, source_handle: int = 1, second_handle: int = 2,
    control_port: int = 5556, relay_b_port: int = 5560,
    viewer_control_host: str = "10.0.2.2", viewer_rdp_host: str = "10.0.2.2",
    exported_a_telemetry: str = "/run/mm-a/exported-a.json",
    exported_b_telemetry: str = "/run/mm-a/exported-b.json",
) -> ForwardDeathResult:
    """Drive the **forward-death watch** gate (item 5, codex impl-26) — prove that
    when a ``qdistro-forward`` transport child dies ON ITS OWN, qdwin's pidfd
    death-watch notices, tears down ONLY that view_stream, and emits a
    subscriber-visible ``torn_down("forward exited")`` — while the SOURCE app and
    every OTHER stream are untouched.

    Two concurrent input-capable exports from ONE qdwin (marker-A on output 1,
    marker-B on output 2; each forward live). A real VM-B viewer decodes stream-A
    (anti-fake: the decoded oracle proves stream-A is a genuinely LIVE remote
    stream BEFORE the kill). Then ``SIGKILL`` forward-A only and assert:

    - **torn_down_a** — the subscriber (mm-bystander) receives exactly one
      ``torn_down`` for stream-A's handle with reason ``"forward exited"``.
    - **torn_down_b_absent** — stream-B's handle gets NO torn_down (per-stream).
    - **forward_a_reaped** — the killed forward is gone with NO zombie (weston's
      signalfd ``waitpid(-1)`` reaped it; qdwin's pidfd fired without qdwin
      waiting). **forward_b_alive** — the other forward is untouched.
    - **marker_a_alive / marker_b_alive** — both source toplevels' units stay
      active: transport death is NOT app death (process truth).
    - **qdwin_detected** — mm-qdwin's journal shows the compositor-side teardown.
    - **slot_freed** — qdwin can mint a NEW stream after the failure (the torn-down
      stream's slot/port freed; the list/resource teardown didn't poison qdwin).

    Honesty: geometry/protocol/seat/process/lifecycle only — the proof signals are
    a real protocol event (torn_down) + OS process truth (pid gone, no zombie, app
    units alive), never "looks closed". Captured BEFORE the resubscribe (which
    restarts the singleton bystander and would wipe the evidence).
    """
    profile(netem_profile)
    bundle = EvidenceBundle.create(
        bundle_dir, scenario="11-mm-forward-death-watch", step="static",
        generation=generation,
        topology=EvTopology(vms=[topology.vm_a, topology.vm_b],
                            netem_profile=netem_profile,
                            description="Phase-1 forward-death watch (item 5)"))

    a = backend.spin(topology.vm_a)
    b = backend.spin(topology.vm_b)
    control: ControlServer | None = None
    status_file = "/run/mm-b/viewer-status.json"
    try:
        backend.apply_netem(a, topology.link_dev, netem_profile)

        # two concurrent input-capable exports from ONE qdwin.
        approved_a = backend.setup_confinement_source(
            a, generation=generation, width=width, height=height,
            exported_telemetry=exported_a_telemetry,
            sentinel_telemetry="/run/mm-a/unused-sentinel.json",
            exported_label="exported-a", sentinel_label="sentinel", allow_input=1)
        approved_b = backend.setup_second_export(
            a, generation=generation, width=width, height=height,
            output_id=second_output_id, telemetry=exported_b_telemetry,
            label="exported-b", relay_port=relay_b_port, allow_input=1)
        src_a = SourceWindowInfo(window_id=source_handle, source_machine=topology.vm_a,
                                 title="marker-a", app_id="qdwin-marker-client",
                                 req_w=width, req_h=height)
        announce_a = bridge_approved(approved_a, src_a, generation)

        # ---- bring up the VM-B viewer on stream-A + decoded oracle (LIVE proof) --
        control = ControlServer(port=control_port)
        backend.launch_viewer(
            b, control_host=viewer_control_host, control_port=control.port,
            rdp_host=viewer_rdp_host, rdp_port=approved_a.rdp_port,
            generation=generation, otp=approved_a.rdp_password,
            size=f"{width}x{height}", status_file=status_file)
        control.accept()
        control.send(announce_a)
        _poll(lambda: backend.viewer_status(b),
              lambda s: s.get("status") == "connected" and s.get("windows"))

        vm_b_screen = next(s.screen_index for s in topology.screens.screens
                           if s.vm == topology.vm_b)
        decoded = bundle.root / "captures" / "vm-b-forward-death.ppm"
        decoded.parent.mkdir(parents=True, exist_ok=True)
        backend.await_decode(b)
        layout = M.compute_layout(width, height)
        res = O.OracleResult(ok=False, payload=None, payload_error="no capture")
        import time as _t
        for _ in range(8):
            backend.capture(b, vm_b_screen, decoded)
            res = O.evaluate(load_image(decoded), layout, 1.0, tol=O.TOL_RDP,
                             auto_origin=True, active_generation=generation,
                             expect_output_id=marker_output_id)
            if res.ok:
                break
            _t.sleep(0.4)
        cap = Capture(path=str(decoded.relative_to(bundle.root)),
                      capture_class=CaptureClass.VM_B_HOST.value,
                      output_id=marker_output_id,
                      role="VM-B monitor (forward-death watch, stream-A live pre-kill)",
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

        # viewer's job (proving the stream was live) is done; stop it so its own
        # freerdp teardown doesn't race the forward-death observation.
        backend.stop_viewer(b)

        # ---- map streams->handles/dynamic-ports->pids; record pre-kill state ----
        # The viewer's approved.rdp_port is the FIXED relay port; the subscriber log
        # + the forward's --rdp-port use the DYNAMIC qdwin port. Attribute streams by
        # approval ORDER (A first, B second) and key pids by the dynamic port.
        pre_log = backend.bystander_log(a)
        blocks = _bystander_blocks(pre_log)
        pids = backend.forward_pids(a)
        handle_a = handle_b = -1
        dynport_a = dynport_b = 0
        pid_a = pid_b = 0
        if len(blocks) >= 2:
            (handle_a, dynport_a), (handle_b, dynport_b) = blocks[0], blocks[1]
            pid_a = pids.get(dynport_a, 0)
            pid_b = pids.get(dynport_b, 0)
        pre_torn = _torn_down_handles(pre_log)
        # sanity: both forwards live, distinct handles, nothing torn down yet.
        pre_ok = (pid_a > 0 and pid_b > 0 and handle_a >= 0 and handle_b >= 0
                  and handle_a != handle_b and not pre_torn)

        # ---- KILL forward-A only -----------------------------------------------
        torn_down_a = forward_a_reaped = False
        torn_down_b_absent = forward_b_alive = marker_a_alive = marker_b_alive = False
        qdwin_detected = slot_freed = False
        if pre_ok:
            backend.kill_forward(a, pid_a)
            # poll the subscriber log for stream-A's torn_down("forward exited").
            post_log = _poll(
                lambda: backend.bystander_log(a),
                lambda lg: handle_a in _torn_down_handles(lg),
                tries=40, delay=0.25)
            torn = _torn_down_handles(post_log)
            torn_down_a = handle_a in torn
            torn_down_b_absent = handle_b not in torn
            # the killed forward must be GONE with no zombie; the other untouched.
            post_pids = backend.forward_pids(a)
            forward_a_reaped = (dynport_a not in post_pids
                                and backend.pid_reaped(a, pid_a))
            forward_b_alive = (dynport_b in post_pids
                               and post_pids.get(dynport_b) == pid_b)
            # process truth: both source apps survived the transport death.
            marker_a_alive = backend.marker_unit_alive(a, "mm-marker")
            marker_b_alive = backend.marker_unit_alive(a, "mm-marker2")
            # compositor-side corroboration in the qdwin journal (it logs the
            # DYNAMIC rdp_port in its 'tearing down view_stream rdp_port=N' line).
            jr = backend.qdwin_journal(a)
            qdwin_detected = ("forward exited" in jr and str(dynport_a) in jr)
            # FINALLY (evidence already captured): prove qdwin still mints streams.
            # NB resubscribe restarts the singleton bystander → wipes bystander.out,
            # so this MUST come after every bystander-log assertion above.
            fresh = backend.resubscribe(a)
            slot_freed = bool(fresh and fresh.rdp_port)

        passed = bool(res.ok and pre_ok and torn_down_a and torn_down_b_absent
                      and forward_a_reaped and forward_b_alive and marker_a_alive
                      and marker_b_alive and qdwin_detected and slot_freed)
        if passed:
            bundle.assert_remote_proof()
        bundle.manifest.passed = passed
        bundle.manifest.notes.append(
            f"forward-death watch (item 5): killed forward-A pid={pid_a} "
            f"(rdp_port={dynport_a}, handle={handle_a}); subscriber got "
            f'torn_down("forward exited")={torn_down_a}; stream-B (handle={handle_b}) '
            f"torn_down_absent={torn_down_b_absent}; forward-A reaped (no zombie)="
            f"{forward_a_reaped}; forward-B alive={forward_b_alive}; marker-A alive="
            f"{marker_a_alive}; marker-B alive={marker_b_alive} (transport death is "
            f"NOT app death); qdwin journal detected teardown={qdwin_detected}; "
            f"qdwin minted a fresh stream after the failure={slot_freed}. pidfd "
            "death-watch proven: weston owns SIGCHLD/waitpid(-1), qdwin learns of "
            "the death via pidfd readiness and tears down exactly one stream.")
        bundle.write()
        return ForwardDeathResult(
            bundle, res, handle_a, handle_b, torn_down_a, torn_down_b_absent,
            forward_a_reaped, forward_b_alive, marker_a_alive, marker_b_alive,
            qdwin_detected, slot_freed, passed)
    finally:
        if control is not None:
            control.close()
        backend.stop_viewer(b)
        backend.clear_netem(a, topology.link_dev)
        backend.destroy(a)
        backend.destroy(b)
