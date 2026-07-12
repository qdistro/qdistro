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

    def test_ppm_crlf_header_no_byte_shift(self, tmp_path):
        # a "\r\n" after maxval must be consumed as ONE separator, not leak a
        # byte into the pixels (codex impl-6 Low). First pixel is 0x20 to also
        # confirm a whitespace-valued pixel byte is NOT eaten.
        w, h = 3, 2
        px = bytes([0x20, 0x00, 0xff] * (w * h))
        p = tmp_path / "crlf.ppm"
        with open(p, "wb") as f:
            f.write(f"P6\r\n{w} {h}\r\n255\r\n".encode() + px)
        img = C.load_image(p)
        assert img.shape == (h, w, 3)
        assert img[0, 0, 0] == 0x20 and img[0, 0, 2] == 0xff

    def test_ppm_with_comment_header(self, tmp_path):
        arr = np.zeros((4, 5, 3), dtype=np.uint8)
        p = tmp_path / "c.ppm"
        with open(p, "wb") as f:
            f.write(b"P6\n# a comment\n5 4\n255\n")
            f.write(arr.tobytes())
        assert C.load_image(p).shape == (4, 5, 3)

    def test_png_named_ppm_loads_by_content(self, tmp_path):
        # virsh screenshot emits PNG even for a .ppm destination; load_image
        # dispatches by magic bytes, not suffix, so this must load (not be fed to
        # the P6 parser). Regression for the live QciVMBackend capture path.
        from PIL import Image
        arr = np.random.default_rng(3).integers(0, 256, (6, 7, 3), dtype=np.uint8)
        p = tmp_path / "vm-b-decoded.ppm"   # PNG content, .ppm name
        Image.fromarray(arr).save(p, format="PNG")
        assert np.array_equal(C.load_image(p), arr)

    def test_garbage_rejected(self, tmp_path):
        p = tmp_path / "bad.ppm"
        p.write_bytes(b"not an image at all")
        with pytest.raises(Exception):
            C.load_image(p)

    def test_png_via_pil(self, tmp_path):
        from PIL import Image
        arr = np.random.default_rng(2).integers(0, 256, (8, 9, 3), dtype=np.uint8)
        p = tmp_path / "y.png"
        Image.fromarray(arr).save(p)
        assert np.array_equal(C.load_image(p), arr)


class TestWestonScreenshooterAdapter:
    def test_capture_copies_out_and_verifies(self, tmp_path):
        # codex impl-3 finding 12: capture(dest) must actually produce dest.
        calls = {}

        def fake_exec(argv):
            calls["exec"] = argv

        def fake_copy_out(guest_path, dest):
            calls["copy"] = (guest_path, dest)
            from pathlib import Path
            Path(dest).write_bytes(b"img")   # simulate fetching the guest file

        adapter = C.WestonScreenshooter(
            capture_class=C.CaptureClass.VM_A_GUEST,
            vm_exec=fake_exec, vm_copy_out=fake_copy_out)
        out = adapter.capture(tmp_path / "shot.png")
        assert out.exists()
        assert calls["copy"][0] == "/tmp/shot.png"

    def test_capture_raises_if_not_copied_out(self, tmp_path):
        adapter = C.WestonScreenshooter(
            capture_class=C.CaptureClass.VM_A_GUEST,
            vm_exec=lambda a: None, vm_copy_out=lambda g, d: None)  # no-op copy
        import pytest
        with pytest.raises(RuntimeError, match="not copied out"):
            adapter.capture(tmp_path / "shot.png")
