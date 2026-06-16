"""Self-tests for the multi-machine marker contract + pixel oracle.

These pin the harness contracts (``09-test-strategy.md`` "build now"): the
barcode codec round-trips, the reference renderer + oracle agree, colour-band
classification survives RDP-style jitter, hidden scaling is caught, and
stale-generation frames are rejected. Pure host tests (numpy/PIL present);
no VM, no compositor.
"""
from __future__ import annotations

import numpy as np
import pytest

from multimachine.harness import marker as M
from multimachine.harness import oracle as O


# ---- codec (pure-Python, no imaging) -------------------------------------
class TestCodec:
    def test_payload_roundtrip(self):
        p = M.MarkerPayload(output_id=2, generation=7, frame=123456,
                            x=1024, y=0, w=800, h=600, scale_x100=150)
        assert M.MarkerPayload.unpack(p.pack()) == p

    def test_crc_rejects_corruption(self):
        raw = bytearray(M.MarkerPayload(1, 1, 1, 0, 0, 10, 10).pack())
        raw[5] ^= 0xFF
        with pytest.raises(ValueError, match="CRC"):
            M.MarkerPayload.unpack(bytes(raw))

    def test_bad_magic_rejected(self):
        raw = bytearray(M.MarkerPayload(1, 1, 1, 0, 0, 10, 10).pack())
        raw[0] ^= 0xFF
        # corrupting magic also breaks CRC; either error is acceptable.
        with pytest.raises(ValueError):
            M.MarkerPayload.unpack(bytes(raw))

    def test_cell_matrix_roundtrip(self):
        p = M.MarkerPayload(3, 42, 999, -5, 12, 1280, 720, 200)
        assert M.decode_cell_matrix(M.cell_matrix(p)) == p

    def test_crc8_known_vector(self):
        # CRC-8/SMBUS (poly 0x07, init 0x00) of "123456789" is 0xF4.
        assert M.crc8(b"123456789") == 0xF4

    def test_capacity_fits(self):
        assert M.PAYLOAD_LEN * 8 <= M.DATA_CAPACITY_BITS


# ---- layout --------------------------------------------------------------
class TestLayout:
    def test_bands_cover_width_without_gaps(self):
        lay = M.compute_layout(1600, 900, seam_x=800)
        assert lay.bands[0].x0 == 0
        assert lay.bands[-1].x1 == 1600
        for a, b in zip(lay.bands, lay.bands[1:]):
            assert a.x1 == b.x0, "bands must be contiguous"

    def test_seam_bands_straddle_seam(self):
        lay = M.compute_layout(1600, 900, seam_x=800)
        sl = next(b for b in lay.bands if b.name == "seam-left")
        sr = next(b for b in lay.bands if b.name == "seam-right")
        assert sl.x1 == 800 and sr.x0 == 800


# ---- renderer + oracle agree ---------------------------------------------
def _render(width=1280, height=480, seam_x=None, scale=1.0, gen=5, out=1, frame=10):
    lay = M.compute_layout(width, height, seam_x=seam_x)
    pay = M.MarkerPayload(output_id=out, generation=gen, frame=frame,
                          x=0, y=0, w=width, h=height,
                          scale_x100=int(round(scale * 100)))
    img = M.render_rgb(lay, pay, scale=scale)
    return lay, pay, img


class TestOracle:
    def test_clean_render_passes(self):
        lay, pay, img = _render()
        res = O.evaluate(img, lay, 1.0, active_generation=5, expect_output_id=1)
        assert res.ok, res.summary()
        assert res.payload == pay
        assert all(b.ok for b in res.bands)

    def test_decode_corner_exact(self):
        lay, pay, img = _render(gen=11, out=3, frame=77)
        got = O.decode_corner(img, lay, 1.0)
        assert got.generation == 11 and got.output_id == 3 and got.frame == 77

    def test_survives_rdp_jitter(self):
        lay, pay, img = _render()
        rng = np.random.default_rng(0)
        noise = rng.integers(-20, 21, img.shape, dtype=np.int16)
        noisy = np.clip(img.astype(np.int16) + noise, 0, 255).astype(np.uint8)
        res = O.evaluate(noisy, lay, 1.0, tol=O.TOL_RDP, active_generation=5)
        assert res.ok, res.summary()

    def test_band_corruption_fails(self):
        lay, pay, img = _render()
        # paint the left-anchor band the wrong colour.
        b = lay.bands[0]
        img[200:, b.x0 + 4 : b.x1 - 4] = M.PALETTE["blue"]
        res = O.evaluate(img, lay, 1.0, active_generation=5)
        assert not res.ok
        assert any(not r.ok and r.name == "left-anchor" for r in res.bands)

    def test_scale_2x_decodes(self):
        lay, pay, img = _render(scale=2.0)
        res = O.evaluate(img, lay, 2.0, active_generation=5)
        assert res.ok, res.summary()
        assert res.payload.scale_x100 == 200

    def test_hidden_scaling_detected(self):
        # render at 2x but stamp scale_x100=100 -> measured disagrees with stamped.
        lay = M.compute_layout(1280, 480)
        pay = M.MarkerPayload(1, 5, 10, 0, 0, 1280, 480, scale_x100=100)
        img = M.render_rgb(lay, pay, scale=2.0)
        res = O.evaluate(img, lay, 2.0, active_generation=5)
        assert res.hidden_scaling, res.summary()
        assert not res.ok

    def test_hidden_scaling_detected_when_caller_expects_1x(self):
        # codex impl-3 finding 3: the image is actually scaled 1.5x but the
        # caller passes expected scale 1.0 (it believes no client scaling) and
        # the stamp says 1.0. The oracle must MEASURE the real pitch from pixels
        # and flag the mismatch — not tautologically recover the caller's scale.
        lay = M.compute_layout(800, 400)
        pay = M.MarkerPayload(1, 5, 10, 0, 0, 800, 400, scale_x100=100)
        img = M.render_rgb(lay, pay, scale=1.5)     # captured pixels are 1.5x
        res = O.evaluate(img, lay, 1.0, active_generation=5)  # caller expects 1x
        assert res.measured_scale is not None
        assert res.measured_scale > 1.3, res.summary()  # measured ~1.5 from pixels
        assert res.hidden_scaling and not res.ok

    def test_measured_scale_matches_real_scale_independent_of_caller(self):
        lay = M.compute_layout(800, 400)
        pay = M.MarkerPayload(1, 5, 10, 0, 0, 800, 400, scale_x100=200)
        img = M.render_rgb(lay, pay, scale=2.0)
        # even if the caller under-states expected scale, the pitch is measured
        # from pixels and should land near the true 2.0.
        m = O.measure_scale_from_corner(img, lay, expected_scale=2.0)
        assert 1.7 < m < 2.3

    def test_stale_generation_rejected(self):
        lay, pay, img = _render(gen=4)
        res = O.evaluate(img, lay, 1.0, active_generation=5)
        assert res.stale_generation
        assert not res.ok

    def test_wrong_output_id_flagged(self):
        lay, pay, img = _render(out=2)
        res = O.evaluate(img, lay, 1.0, active_generation=5, expect_output_id=1)
        assert not res.ok
        assert any("output_id" in n for n in res.notes)

    def test_auto_origin_decodes_offset_capture(self):
        # a compositor places the marker view at an offset on a black output
        # (qdwin insets a toplevel); auto_origin must find it and decode.
        lay, pay, marker = _render(width=608, height=448, gen=7, out=1, frame=11)
        canvas = np.zeros((480, 640, 3), dtype=np.uint8)  # black output
        canvas[32:32 + 448, 32:32 + 608] = marker          # placed at (32,32)
        res = O.evaluate(canvas, lay, 1.0, auto_origin=True,
                         active_generation=7, expect_output_id=1)
        assert res.ok, res.summary()
        assert res.payload.frame == 11
        assert any("auto-origin=(32, 32)" in n for n in res.notes)

    def test_detect_origin_all_black_returns_zero(self):
        assert O.detect_origin(np.zeros((10, 10, 3), dtype=np.uint8)) == (0, 0)

    def test_zero_width_band_marked_not_applicable(self):
        # codex impl-3 finding 9: a seam_x that collapses the outer bands to zero
        # width must mark them n/a (ok), not sample an adjacent band and fail.
        lay = M.compute_layout(800, 400, seam_x=50)
        zero = [b for b in lay.bands if b.x1 - b.x0 <= 0]
        assert zero, "expected at least one zero-width band for this layout"
        pay = M.MarkerPayload(1, 5, 10, 0, 0, 800, 400, scale_x100=100)
        img = M.render_rgb(lay, pay, scale=1.0)
        res = O.evaluate(img, lay, 1.0, active_generation=5)
        for b in res.bands:
            if (b.name in {z.name for z in zero}):
                assert b.classified == "n/a" and b.ok
        assert res.ok, res.summary()
