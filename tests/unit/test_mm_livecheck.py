"""Live render/golden integration test (marker -> weston -> capture -> oracle).

Marked ``slow``: spawns stock headless weston and the C marker client, captures
with weston-screenshooter, and runs the deterministic oracle on the decoded PNG.
Deselect with ``-m 'not slow'`` for the fast host subset. Skips (not fails) when
weston / weston-screenshooter / the marker binary are unavailable, so CI without
a compositor stays green.

A pass means the marker renders + captures + decodes correctly through a real
compositor on this host — NOT remote-output or A5 proof, and stock weston is not
qdwin (the A1 placement probe is separate).
"""
from __future__ import annotations

import shutil

import pytest

from multimachine.harness import livecheck as L

pytestmark = pytest.mark.slow


def _have_env() -> bool:
    return bool(shutil.which("weston") and shutil.which("weston-screenshooter")
                and L.find_marker_binary())


@pytest.mark.skipif(not _have_env(),
                    reason="weston/weston-screenshooter/marker client unavailable")
class TestLiveRenderGolden:
    def test_static_marker_decodes_through_real_compositor(self, tmp_path):
        res, bundle = L.run_render_golden(
            width=1280, height=480, output_id=1, generation=5, frame=42,
            bundle_dir=tmp_path / "rg")
        assert res.ok, res.summary()
        assert res.payload.output_id == 1
        assert res.payload.generation == 5
        assert res.payload.frame == 42
        assert all(b.ok for b in res.bands)
        # bundle is written + complete
        assert (bundle.root / "manifest.json").exists()
        assert bundle.manifest.passed is True
        assert len(bundle.manifest.captures) == 1

    def test_second_geometry_and_output_id_live(self, tmp_path):
        # a different size + output id proves nothing is hardcoded.
        res, bundle = L.run_render_golden(
            width=800, height=400, seam_x=400, output_id=2, generation=7,
            frame=3, bundle_dir=tmp_path / "rg2")
        assert res.ok, res.summary()
        assert res.payload.output_id == 2
        assert res.payload.frame == 3

    def test_oracle_catches_wrong_active_generation_on_live_capture(self, tmp_path):
        # render at generation 5, then re-run the oracle on the captured image
        # against active generation 9 -> stale rejection on real pixels.
        import multimachine.harness.marker as M
        import multimachine.harness.oracle as O
        from multimachine.harness.capture import load_image

        res, bundle = L.run_render_golden(
            width=800, height=400, output_id=1, generation=5, frame=1,
            bundle_dir=tmp_path / "rg3")
        assert res.ok
        shot = bundle.root / bundle.manifest.captures[0].path
        img = load_image(shot)
        layout = M.compute_layout(800, 400)
        stale = O.evaluate(img, layout, 1.0, active_generation=9)
        assert stale.stale_generation and not stale.ok
