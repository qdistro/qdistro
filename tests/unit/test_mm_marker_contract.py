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
