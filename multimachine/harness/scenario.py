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
        exported_label: str, sentinel_label: str) -> ViewStreamApproved: ...

    def launch_sentinel(self, vm: str, *, generation: int,
                        sentinel_telemetry: str, sentinel_label: str) -> None: ...

    def read_telemetry(self, vm: str, path: str) -> dict: ...

    def inject_input(self, vm: str) -> None: ...


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
