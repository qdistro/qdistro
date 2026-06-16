"""Tests for the capture/image-loading helpers (PPM parser, normalization)."""
from __future__ import annotations

import numpy as np
import pytest

from multimachine.harness import capture as C


def _write_ppm(path, arr):
    h, w, _ = arr.shape
    with open(path, "wb") as f:
        f.write(f"P6\n{w} {h}\n255\n".encode())
        f.write(arr.astype(np.uint8).tobytes())


class TestLoadImage:
    def test_ppm_roundtrip(self, tmp_path):
        arr = np.random.default_rng(1).integers(0, 256, (17, 23, 3), dtype=np.uint8)
        p = tmp_path / "x.ppm"
        _write_ppm(p, arr)
        assert np.array_equal(C.load_image(p), arr)

    def test_ppm_with_comment_header(self, tmp_path):
        arr = np.zeros((4, 5, 3), dtype=np.uint8)
        p = tmp_path / "c.ppm"
        with open(p, "wb") as f:
            f.write(b"P6\n# a comment\n5 4\n255\n")
            f.write(arr.tobytes())
        assert C.load_image(p).shape == (4, 5, 3)

    def test_non_p6_rejected(self, tmp_path):
        p = tmp_path / "bad.ppm"
        p.write_bytes(b"P3\n1 1\n255\n0 0 0\n")
        with pytest.raises(ValueError, match="P6"):
            C.load_image(p)

    def test_png_via_pil(self, tmp_path):
        from PIL import Image
        arr = np.random.default_rng(2).integers(0, 256, (8, 9, 3), dtype=np.uint8)
        p = tmp_path / "y.png"
        Image.fromarray(arr).save(p)
        assert np.array_equal(C.load_image(p), arr)
