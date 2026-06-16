"""Capture adapters + image loading for the two-VM display harness.

Two concerns:

1. **Image loading / normalization** (``load_image``): read a capture into an
   (H, W, 3) uint8 RGB numpy array regardless of source format. ``virsh
   screenshot`` emits PPM (P6); ``weston-screenshooter`` emits PNG. PPM is
   parsed natively (no PIL dependency on that path); everything else goes
   through PIL. The oracle works in a known RGB space (09: "normalize captures
   into a known RGB colour space before matching").

2. **Capture adapters** (``CaptureAdapter`` subclasses): thin wrappers that
   shell out to the right tool for each capture *class* (09 "name every
   framebuffer by what it proves"). They run a subprocess only when invoked, so
   importing this module never needs a VM. Each adapter records its
   :class:`~.evidence.CaptureClass` so a bundle is honestly labelled.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .evidence import CaptureClass


# --------------------------------------------------------------------------
# Image loading
# --------------------------------------------------------------------------
def _read_ppm(path: Path) -> np.ndarray:
    """Parse a binary PPM (P6). Header tokens are whitespace-separated and may
    be interleaved with comments (# ...)."""
    data = path.read_bytes()
    if not data.startswith(b"P6"):
        raise ValueError(f"{path}: not a P6 PPM")
    # tokenize header: magic, width, height, maxval — skipping comments.
    pos = 2
    tokens: list[bytes] = []
    while len(tokens) < 3:
        while pos < len(data) and data[pos] in b" \t\r\n":
            pos += 1
        if pos < len(data) and data[pos:pos + 1] == b"#":
            while pos < len(data) and data[pos] not in b"\r\n":
                pos += 1
            continue
        start = pos
        while pos < len(data) and data[pos] not in b" \t\r\n":
            pos += 1
        tokens.append(data[start:pos])
    width, height, maxval = (int(t) for t in tokens)
    if maxval != 255:
        raise ValueError(f"{path}: unsupported maxval {maxval}")
    pos += 1  # single whitespace after maxval
    expected = width * height * 3
    pixels = np.frombuffer(data[pos:pos + expected], dtype=np.uint8)
    if pixels.size != expected:
        raise ValueError(f"{path}: short pixel data {pixels.size} != {expected}")
    return pixels.reshape(height, width, 3).copy()


def load_image(path: Path | str) -> np.ndarray:
    """Load any capture as an (H, W, 3) uint8 RGB array.

    Dispatch is by CONTENT (magic bytes), not extension: ``virsh screenshot``
    emits PNG even when the caller named the file ``.ppm`` (host-dependent), so
    trusting the suffix would mis-route. P6 PPM is parsed natively; anything else
    goes through PIL."""
    path = Path(path)
    with open(path, "rb") as f:
        magic = f.read(2)
    if magic == b"P6":
        return _read_ppm(path)
    from PIL import Image  # lazy: only the non-PPM path needs PIL

    img = Image.open(path).convert("RGB")
    return np.asarray(img, dtype=np.uint8)


# --------------------------------------------------------------------------
# Capture adapters
# --------------------------------------------------------------------------
@dataclass
class CaptureAdapter:
    """Base: a named source that writes a capture file on ``capture()``."""

    capture_class: CaptureClass
    role: str = ""

    def capture(self, dest: Path | str) -> Path:  # pragma: no cover - abstract
        raise NotImplementedError


@dataclass
class VirshScreenshot(CaptureAdapter):
    """Host-side ``virsh screenshot <dom> --screen N`` (QEMU virtual head).

    For VM-A this is the local display head (``VM_A_HOST``); for VM-B it is the
    best "what VM-B's monitor shows" after FreeRDP decode (``VM_B_HOST``).
    """

    domain: str = ""
    screen: int = 0
    connect: str = "qemu:///session"

    def capture(self, dest: Path | str) -> Path:
        dest = Path(dest)
        subprocess.run(
            ["virsh", "-c", self.connect, "screenshot", self.domain,
             str(dest), "--screen", str(self.screen)],
            check=True, capture_output=True)
        return dest


@dataclass
class WestonScreenshooter(CaptureAdapter):
    """Guest-side ``weston-screenshooter`` (compositor logical pixels, pre-encode).

    Runs inside the guest via ``vm_exec`` then copies the guest file out to
    ``dest`` via ``vm_copy_out`` and verifies it exists, so ``capture(dest)``
    only returns a path it actually produced (codex impl-3 finding 12 — never
    report success without the file).
    """

    vm_exec: object = None       # Callable[[list[str]], subprocess.CompletedProcess]
    vm_copy_out: object = None   # Callable[[str guest_path, Path dest], None]
    guest_wayland: str = "wayland-1"

    def capture(self, dest: Path | str) -> Path:
        if self.vm_exec is None or self.vm_copy_out is None:
            raise RuntimeError(
                "WestonScreenshooter needs vm_exec AND vm_copy_out callables")
        dest = Path(dest)
        guest_path = f"/tmp/{dest.name}"
        self.vm_exec([  # type: ignore[operator]
            "env", f"WAYLAND_DISPLAY={self.guest_wayland}",
            "weston-screenshooter", guest_path])
        self.vm_copy_out(guest_path, dest)  # type: ignore[operator]
        if not dest.exists():
            raise RuntimeError(
                f"weston-screenshooter capture not copied out to {dest}")
        return dest
