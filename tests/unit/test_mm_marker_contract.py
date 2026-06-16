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
