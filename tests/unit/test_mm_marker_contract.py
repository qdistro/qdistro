"""Cross-language contract test: the C marker client == the Python reference.

The C client (``qdwin/test-client/qdwin-marker-client.c``) and the Python
reference renderer (``marker.render_rgb``) must paint pixel-identical output,
because the pixel oracle decodes whatever the *C* client paints in a live VM
but the golden/render tests use the *Python* renderer. If they drift, a VM pass
would not imply a golden pass.

Two layers:

- ``test_golden_*`` (always runs): a committed PNG **produced by the C client**
  is loaded, the oracle decodes the expected payload, and the Python reference
  is asserted pixel-identical to it. Pins the contract in CI without a build.
- ``test_live_c_client_*`` (runs when the built binary is found): renders fresh
  from C offscreen (``--dump-ppm``) and re-checks the agreement, catching drift
  the frozen golden cannot.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from multimachine.harness import capture as C
from multimachine.harness import marker as M
from multimachine.harness import oracle as O

_DATA = Path(__file__).parent / "data" / "mm"
_GOLDEN = _DATA / "marker-golden-384x256-o2-g11-f7.png"
# Golden parameters (encoded in the filename).
_G = dict(width=384, height=256, output_id=2, generation=11, frame=7)


def _python_reference():
    lay = M.compute_layout(_G["width"], _G["height"])
    pay = M.MarkerPayload(output_id=_G["output_id"], generation=_G["generation"],
                          frame=_G["frame"], x=0, y=0,
                          w=_G["width"], h=_G["height"], scale_x100=100)
    return lay, pay, M.render_rgb(lay, pay, scale=1.0)


class TestGolden:
    def test_golden_decodes_to_expected_payload(self):
        img = C.load_image(_GOLDEN)
        lay, _, _ = _python_reference()
        res = O.evaluate(img, lay, 1.0, active_generation=_G["generation"],
                         expect_output_id=_G["output_id"])
        assert res.ok, res.summary()
        assert res.payload.frame == _G["frame"]

    def test_python_reference_matches_golden_pixel_for_pixel(self):
        golden = C.load_image(_GOLDEN)
        _, _, py = _python_reference()
        assert golden.shape == py.shape
        assert np.array_equal(golden, py), (
            "Python reference renderer drifted from the committed C-rendered "
            "golden — marker.py and qdwin-marker-client.c are out of sync.")


_LIVE_PERVIEW = _DATA / "live-perview-qfwd-source-640x480-o1-g7-f11.png"


class TestLivePerViewCapture:
    """Regression on a REAL VM capture: the qfwd PipeWire source frame of the
    marker, captured through qdwin composition + the shipped per-view path
    (2026-06-16 live run). Decodes only with auto_origin (qdwin inset the
    toplevel by 32px). This is VM_A_RDP_SOURCE (source-side) evidence — proves
    the marker survives qdwin composition + per-view capture, not decoded-remote.
    """

    def test_live_qfwd_source_decodes_with_auto_origin(self):
        img = C.load_image(_LIVE_PERVIEW)
        assert img.shape == (480, 640, 3)
        lay = M.compute_layout(640, 480)
        res = O.evaluate(img, lay, 1.0, tol=O.TOL_RDP, auto_origin=True,
                         active_generation=7, expect_output_id=1)
        assert res.ok, res.summary()
        assert res.payload.frame == 11 and res.payload.output_id == 1
        assert all(b.ok for b in res.bands)

    def test_live_capture_needs_origin_correction(self):
        # documents the compositor placement: at origin (0,0) it does NOT decode.
        img = C.load_image(_LIVE_PERVIEW)
        assert O.detect_origin(img) == (32, 32)


_LIVE_DECODED = _DATA / "live-decoded-remote-vmb-1280x800-o1-g20.png"


class TestLiveDecodedRemoteCapture:
    """Regression on the DECODED-REMOTE capture (the honesty-rule capture):
    a REAL two-VM live run (2026-06-16 session 2). VM-A runs a dedicated headless
    qdwin (libweston + qdwin-shell.so, [pipewire] num-outputs raised) with the
    fullscreen marker as the source toplevel; the shipped per-view path spawns
    qdistro-forward (RDP server); VM-B runs ``sdl-freerdp`` as a fullscreen
    Wayland client under a kiosk-shell weston on its own DRM head; the decoded
    output is captured host-side via ``virsh screenshot`` (QMP). RDP bytes are
    chained VM-B -> host loopback -> VM-A over two SLIRP NATs (PLAN A, codex
    impl-4). This is VM_B_HOST (decoded-remote) — it proves what the peer monitor
    actually shows, within RDP tolerance, at 1:1 with NO hidden scaling.
    """

    def test_decoded_remote_decodes_clean_no_hidden_scaling(self):
        img = C.load_image(_LIVE_DECODED)
        assert img.shape == (800, 1280, 3)
        lay = M.compute_layout(1280, 800)
        res = O.evaluate(img, lay, 1.0, tol=O.TOL_RDP, auto_origin=True,
                         active_generation=20, expect_output_id=1)
        assert res.ok, res.summary()
        assert res.payload.output_id == 1 and res.payload.generation == 20
        assert res.payload.w == 1280 and res.payload.h == 800
        assert res.measured_scale == 1.0 and not res.hidden_scaling
        assert not res.stale_generation
        assert all(b.ok for b in res.bands)

    def test_decoded_remote_kiosk_capture_is_flush(self):
        # kiosk-shell places the fullscreen surface at the output origin: the
        # decoded marker fills the head 1:1 (auto_origin (0,0)), unlike a
        # desktop-shell capture that centred + clipped the quiet zone.
        img = C.load_image(_LIVE_DECODED)
        assert O.detect_origin(img) == (0, 0)

    def test_decoded_remote_satisfies_honesty_rule(self, tmp_path):
        from multimachine.harness.evidence import (
            CaptureClass, EvidenceBundle, OracleRecord, Topology as EvTopology)
        img = C.load_image(_LIVE_DECODED)
        lay = M.compute_layout(1280, 800)
        res = O.evaluate(img, lay, 1.0, tol=O.TOL_RDP, auto_origin=True,
                         active_generation=20, expect_output_id=1)
        b = EvidenceBundle.create(
            tmp_path / "b", scenario="mm-01-decoded-remote", step="live-decoded",
            generation=20,
            topology=EvTopology(vms=["vm-a", "vm-b"], netem_profile="lan-clean",
                                description="decoded-remote live"))
        cap = b.add_capture(_LIVE_DECODED, CaptureClass.VM_B_HOST, output_id=1,
                            role="VM-B monitor (decoded RDP)", fmt="PNG", scale=1.0)
        b.add_oracle(OracleRecord(
            capture=cap.path, ok=res.ok, output_id=res.payload.output_id,
            generation=res.payload.generation, frame=res.payload.frame,
            measured_scale=res.measured_scale, hidden_scaling=res.hidden_scaling))
        b.manifest.passed = res.ok
        b.assert_remote_proof()  # must not raise: passing oracle on a VM_B capture


# ---- Phase-1: LIVE managed-toplevel viewer gate (scenario-2) -------------
_LIVE_MANAGED = _DATA / "live-managed-toplevel-vmb-1280x800-o1-g22.png"


class TestLiveManagedToplevelCapture:
    """Regression on the Phase-1 MANAGED-TOPLEVEL capture (scenario-2, codex
    impl-8/impl-9): a REAL two-VM live run (2026-06-16 session 4). VM-A runs the
    dedicated headless qdwin + fullscreen marker + the shipped per-view RDP path;
    VM-B runs the REAL ``mm-viewer-launch`` (``python3 -m multimachine.viewer``)
    INSIDE a kiosk-shell weston — it consumes a host-served JSON-lines control
    side-channel (a real forwarded TCP byte stream over VM-B's SLIRP NAT) and, on
    the ``Announce``, launches ``sdl-freerdp`` fullscreen ITSELF. So the captured
    surface IS the viewer-managed toplevel, not a bare decoder. Captured host-side
    via ``virsh screenshot`` (QMP). VM_B_HOST (decoded-remote) at 1:1, NO hidden
    scaling.

    Honesty (impl-8): geometry/protocol/process/lifecycle only — NOT A5. The full
    live gate also proved (not re-checkable from this still): step-9 viewer-close
    releases the stream slot while the source survives, and step-10 host-injected
    Disconnect blanks the viewer + a stale-generation control msg is rejected live
    + the source survives. Input confinement (step 8) is DEFERRED — the marker has
    no input hook (impl-9 Q4). The decoded gfx channel is forced to RemoteFX
    progressive (full-range RGB); the FreeRDP build's H.264/AVC path decodes
    limited-range (dim), which would crush the barcode — a color artifact unrelated
    to the geometry claim the fence makes.
    """

    def test_managed_toplevel_decodes_clean_no_hidden_scaling(self):
        img = C.load_image(_LIVE_MANAGED)
        assert img.shape == (800, 1280, 3)
        lay = M.compute_layout(1280, 800)
        res = O.evaluate(img, lay, 1.0, tol=O.TOL_RDP, auto_origin=True,
                         active_generation=22, expect_output_id=1)
        assert res.ok, res.summary()
        assert res.payload.output_id == 1 and res.payload.generation == 22
        assert res.payload.w == 1280 and res.payload.h == 800
        assert res.measured_scale == 1.0 and not res.hidden_scaling
        assert not res.stale_generation
        assert all(b.ok for b in res.bands)

    def test_managed_toplevel_kiosk_capture_is_flush(self):
        # the viewer-managed sdl-freerdp toplevel lands fullscreen at the kiosk
        # output origin: the decoded marker fills the head 1:1 (auto_origin (0,0)).
        assert O.detect_origin(C.load_image(_LIVE_MANAGED)) == (0, 0)

    def test_managed_toplevel_rejects_stale_generation(self):
        # the fence: the SAME capture must FAIL the oracle under a different active
        # generation (proves the generation stamp is read, not assumed).
        img = C.load_image(_LIVE_MANAGED)
        lay = M.compute_layout(1280, 800)
        res = O.evaluate(img, lay, 1.0, tol=O.TOL_RDP, auto_origin=True,
                         active_generation=23, expect_output_id=1)
        assert not res.ok and res.stale_generation

    def test_managed_toplevel_satisfies_honesty_rule(self, tmp_path):
        from multimachine.harness.evidence import (
            CaptureClass, EvidenceBundle, OracleRecord, Topology as EvTopology)
        img = C.load_image(_LIVE_MANAGED)
        lay = M.compute_layout(1280, 800)
        res = O.evaluate(img, lay, 1.0, tol=O.TOL_RDP, auto_origin=True,
                         active_generation=22, expect_output_id=1)
        b = EvidenceBundle.create(
            tmp_path / "b", scenario="09-mm-viewer-managed-toplevel",
            step="live-managed", generation=22,
            topology=EvTopology(vms=["vm-a", "vm-b"], netem_profile="lan-clean",
                                description="managed-toplevel live"))
        cap = b.add_capture(_LIVE_MANAGED, CaptureClass.VM_B_HOST, output_id=1,
                            role="VM-B monitor (viewer-managed decoded toplevel)",
                            fmt="PNG", scale=1.0)
        b.add_oracle(OracleRecord(
            capture=cap.path, ok=res.ok, output_id=res.payload.output_id,
            generation=res.payload.generation, frame=res.payload.frame,
            measured_scale=res.measured_scale, hidden_scaling=res.hidden_scaling))
        b.manifest.passed = res.ok
        b.assert_remote_proof()  # passing oracle tied to a VM_B decoded capture


# ---- A1-min: LIVE two-output straddle (render gate) ----------------------
_A1_OUT0 = _DATA / "live-straddle-a1-out0-800x600-o1-g20.png"
_A1_OUT1 = _DATA / "live-straddle-a1-out1-800x600-o1-g20.png"
_A1_CALIB = _DATA / "live-straddle-a1-calib-out0-800x600-o1-g20.png"

# Geometry of the real run (codex impl-7): two adjacent 800x600 qdwin outputs
# (Virtual-1 [0,0,800,600] = out0/head0, Virtual-2 [800,0,800,600] = out1/head1),
# a 512x400 marker (seam_x=256) placed by the QDWIN_TEST_PLACE_* hook at global
# (544,100) so its internal seam lands exactly on the output boundary x=800.
_A1_MW, _A1_MH, _A1_SEAM = 512, 400, 256
_A1_MARKER_X_IN_OUT0, _A1_OY, _A1_GEN = 544, 100, 20


class TestLiveStraddleA1:
    """Regression on the A1-min LIVE two-output straddle render gate (the
    render-gate proof, 2026-06-16 session 3). A SINGLE dedicated qdwin
    (libweston + qdwin-shell.so) drives TWO adjacent DRM outputs on one
    virtio-gpu (max_outputs=2, the 2nd connector force-enabled with the kernel
    ``video=Virtual-2:800x600e``; both QEMU scanouts registered as consoles via
    ``-display egl-headless``). One normal marker toplevel, placed across the
    seam by the test-only ``QDWIN_TEST_PLACE_*`` hook, is composited by libweston
    onto BOTH outputs (``output_mask``); each output's QEMU virtual head is
    captured host-side via QMP ``screendump device=gpu0 head=N``. This is
    VM_A_HOST (local two-output straddle) — it proves the libweston render +
    per-output capture + oracle truth, NOT WM policy / runtime output lifecycle /
    RDP-as-monitor / input (codex impl-5 scope).
    """

    def _layout(self):
        return M.compute_layout(_A1_MW, _A1_MH, seam_x=_A1_SEAM)

    def test_straddle_passes_clean_no_hidden_scaling(self):
        out0 = C.load_image(_A1_OUT0)
        out1 = C.load_image(_A1_OUT1)
        assert out0.shape == (600, 800, 3) and out1.shape == (600, 800, 3)
        res = O.evaluate_straddle(
            out0, out1, self._layout(), marker_x_in_out0=_A1_MARKER_X_IN_OUT0,
            scale=1.0, tol=O.TOL_RDP, oy=_A1_OY, active_generation=_A1_GEN,
            expect_output_id=1)
        assert res.ok, res.summary()
        assert res.seam_continuous and not res.hidden_scaling
        assert res.measured_scale == 1.0 and not res.stale_generation
        assert res.payload.output_id == 1 and res.payload.generation == _A1_GEN
        assert all(b.ok for b in res.out0_bands) and res.out0_bands
        assert all(b.ok for b in res.out1_bands) and res.out1_bands

    def test_straddle_head_mapping_is_non_ambiguous(self):
        # Swapping the per-output captures (head1->out0, head0->out1) MUST fail:
        # the oracle's source-rect-aware halves only pass under the correct
        # head->output assignment, so the screen-index mapping is proven, not
        # assumed (codex impl-7 mapping requirement).
        out0 = C.load_image(_A1_OUT0)
        out1 = C.load_image(_A1_OUT1)
        swapped = O.evaluate_straddle(
            out1, out0, self._layout(), marker_x_in_out0=_A1_MARKER_X_IN_OUT0,
            scale=1.0, tol=O.TOL_RDP, oy=_A1_OY, active_generation=_A1_GEN)
        assert not swapped.ok

    def test_straddle_rejects_stale_generation(self):
        out0 = C.load_image(_A1_OUT0)
        out1 = C.load_image(_A1_OUT1)
        res = O.evaluate_straddle(
            out0, out1, self._layout(), marker_x_in_out0=_A1_MARKER_X_IN_OUT0,
            scale=1.0, tol=O.TOL_RDP, oy=_A1_OY, active_generation=_A1_GEN + 5)
        assert not res.ok and res.stale_generation

    def test_calibration_zero_decoration_offset_and_mapping(self):
        # The calibration pass placed the marker WHOLLY inside out0 at global
        # (100,100). Its content top-left must land EXACTLY at (100,100) (no
        # server-side decoration offset) and NOTHING must appear on out1 — which
        # also proves head0 is the output holding global x in [0,800) = out0.
        cal0 = C.load_image(_A1_CALIB)
        mask = cal0.sum(2) > 40
        ys, xs = np.where(mask)
        assert (xs.min(), ys.min()) == (100, 100)          # zero offset
        assert (xs.max(), ys.max()) == (100 + _A1_MW - 1, 100 + _A1_MH - 1)

    def test_straddle_satisfies_render_gate_evidence(self, tmp_path):
        from multimachine.harness.evidence import (
            CaptureClass, EvidenceBundle, OracleRecord, Topology as EvTopology)
        out0 = C.load_image(_A1_OUT0)
        out1 = C.load_image(_A1_OUT1)
        res = O.evaluate_straddle(
            out0, out1, self._layout(), marker_x_in_out0=_A1_MARKER_X_IN_OUT0,
            scale=1.0, tol=O.TOL_RDP, oy=_A1_OY, active_generation=_A1_GEN,
            expect_output_id=1)
        b = EvidenceBundle.create(
            tmp_path / "b", scenario="a1-min-straddle", step="live-straddle",
            generation=_A1_GEN,
            topology=EvTopology(vms=["vm-a"], netem_profile="lan-clean",
                                description="A1-min two-output straddle (local)"))
        # Two VM_A_HOST captures = the two QEMU virtual heads (the two qdwin
        # outputs) the marker straddles.
        b.add_capture(_A1_OUT0, CaptureClass.VM_A_HOST, output_id=1,
                      role="qdwin Virtual-1 (out0, left of seam)", fmt="PNG",
                      scale=1.0)
        b.add_capture(_A1_OUT1, CaptureClass.VM_A_HOST, output_id=1,
                      role="qdwin Virtual-2 (out1, right of seam)", fmt="PNG",
                      scale=1.0)
        b.add_oracle(OracleRecord(
            capture=str(_A1_OUT0), ok=res.ok, output_id=res.payload.output_id,
            generation=res.payload.generation, frame=res.payload.frame,
            measured_scale=res.measured_scale,
            hidden_scaling=res.hidden_scaling))
        b.manifest.passed = res.ok
        assert b.manifest.passed       # render gate: a passing straddle oracle
        assert res.ok and not res.hidden_scaling


def _find_marker_binary() -> str | None:
    env = os.environ.get("QDWIN_MARKER_CLIENT")
    if env and Path(env).exists():
        return env
    for cand in (
        "/tmp/mm-build/qdwin-marker-client",
        # common qdwin build dirs (sibling repo)
        *(str(p) for p in Path("/home/play2/qdistro/qdwin").glob(
            "build*/qdwin-marker-client")),
    ):
        if Path(cand).exists():
            return cand
    return shutil.which("qdwin-marker-client")


class TestLiveCClient:
    def test_live_c_client_matches_python(self, tmp_path):
        binary = _find_marker_binary()
        if not binary:
            pytest.skip("qdwin-marker-client not built (set QDWIN_MARKER_CLIENT)")
        ppm = tmp_path / "live.ppm"
        subprocess.run(
            [binary, "--dump-ppm", str(ppm), "--width", str(_G["width"]),
             "--height", str(_G["height"]), "--output-id", str(_G["output_id"]),
             "--generation", str(_G["generation"]), "--frame", str(_G["frame"])],
            check=True, capture_output=True)
        live = C.load_image(ppm)
        _, _, py = _python_reference()
        assert np.array_equal(live, py), (
            "live C render != Python reference (contract drift)")

    def test_live_c_client_decodes_various_params(self, tmp_path):
        binary = _find_marker_binary()
        if not binary:
            pytest.skip("qdwin-marker-client not built")
        for out, gen, frame, w, h in [(1, 5, 42, 1280, 480),
                                       (3, 99, 1000, 800, 600)]:
            ppm = tmp_path / f"c-{out}-{gen}.ppm"
            subprocess.run(
                [binary, "--dump-ppm", str(ppm), "--width", str(w),
                 "--height", str(h), "--output-id", str(out),
                 "--generation", str(gen), "--frame", str(frame)],
                check=True, capture_output=True)
            img = C.load_image(ppm)
            lay = M.compute_layout(w, h)
            res = O.evaluate(img, lay, 1.0, active_generation=gen,
                             expect_output_id=out)
            assert res.ok, f"({out},{gen}): {res.summary()}"
            assert res.payload.frame == frame

    def test_live_c_client_non_centered_seam_matches_python(self, tmp_path):
        # codex impl-3 finding 8: cover the --seam-x layout path (the C band
        # distribution must match Python's compute_layout for a non-centered seam).
        binary = _find_marker_binary()
        if not binary:
            pytest.skip("qdwin-marker-client not built")
        w, h, seam = 1280, 480, 400
        ppm = tmp_path / "seam.ppm"
        subprocess.run(
            [binary, "--dump-ppm", str(ppm), "--width", str(w), "--height",
             str(h), "--seam-x", str(seam), "--output-id", "1",
             "--generation", "5", "--frame", "1"],
            check=True, capture_output=True)
        live = C.load_image(ppm)
        lay = M.compute_layout(w, h, seam_x=seam)
        pay = M.MarkerPayload(1, 5, 1, 0, 0, w, h, 100)
        py = M.render_rgb(lay, pay, scale=1.0)
        assert np.array_equal(live, py), "C/Python seam layout drift"
        res = O.evaluate(live, lay, 1.0, active_generation=5, expect_output_id=1)
        assert res.ok, res.summary()
