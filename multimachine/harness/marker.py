"""Deterministic marker contract for the two-VM display harness.

This module is the *single source of truth* for what the marker client paints.
Three consumers key off it:

1. the C marker client ``qdwin/test-client/qdwin-marker-client.c`` (renders it
   live inside a qdwin session);
2. the pixel oracle (``oracle.py``) — decodes the corner barcode and classifies
   the colour bands;
3. the render/golden tests — compare a headless render against this reference.

Design follows codex round 6 (``09-test-strategy.md``): large solid high-contrast
regions (not single-pixel lines), a machine-readable corner block carrying
output/generation/frame/geometry/scale, vertical bands at known compositor
x-ranges, and 8x8 fiducial checkers for hidden-scale detection.

The **codec** (pack/unpack/CRC/bit-layout) is intentionally pure-Python with no
numpy dependency so it (a) unit-tests with no imaging libs and (b) ports
mechanically to C. Only the reference *renderer* needs numpy.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Palette — high-contrast, non-near-threshold colours that survive RDP encode /
# colour conversion (codex r6). Order is part of the contract.
# --------------------------------------------------------------------------
PALETTE: dict[str, tuple[int, int, int]] = {
    "red": (0xE0, 0x20, 0x20),
    "green": (0x20, 0xC0, 0x60),
    "blue": (0x20, 0x60, 0xE0),
    "yellow": (0xE0, 0xD0, 0x20),
    "white": (0xFF, 0xFF, 0xFF),
    "black": (0x00, 0x00, 0x00),
}

# Vertical bands at known x-ranges (codex r6). The seam sits between
# ``seam-left`` and ``seam-right``; ``*-anchor`` bands are always present even
# when there is no seam (whole-window viewer tests) so the oracle always has
# fixed reference regions. Each band gets a distinct palette colour.
BANDS: list[tuple[str, str]] = [
    ("left-anchor", "red"),
    ("pre-seam", "green"),
    ("seam-left", "blue"),
    ("seam-right", "yellow"),
    ("post-seam", "green"),
    ("right-anchor", "red"),
]

# Corner-block (barcode) geometry. A GRID_ROWS x GRID_COLS grid of cells, each
# CELL_PX logical px square, drawn in the top-left of the marker surface.
#   - cell (0,0) is the black origin/finder anchor;
#   - row 0 / col 0 (from index 1) are alternating timing patterns;
#   - interior cells (row>=1, col>=1) carry data bits, row-major, white=1.
# A 1-cell white quiet zone surrounds the grid.
GRID_ROWS = 14
GRID_COLS = 14
CELL_PX = 12  # logical px per cell at scale 1.0
QUIET_CELLS = 1
DATA_CAPACITY_BITS = (GRID_ROWS - 1) * (GRID_COLS - 1)  # 169

# Fiducial checker inside each band (scale detection).
FIDUCIAL_CELLS = 8
FIDUCIAL_CELL_PX = 4  # logical px per checker cell at scale 1.0

MAGIC = b"MM"
VERSION = 1

# Payload wire format (little-endian), see MarkerPayload.pack():
#   magic[2] version[1] output_id[1] generation[2] frame[4]
#   x[2] y[2] w[2] h[2] scale_x100[1]  -> 19 bytes, + CRC8 -> 20 bytes.
_PAYLOAD_STRUCT = struct.Struct("<2sBBHIhhhhB")
PAYLOAD_LEN = _PAYLOAD_STRUCT.size + 1  # + CRC8
assert PAYLOAD_LEN * 8 <= DATA_CAPACITY_BITS, "barcode too small for payload"


def crc8(data: bytes) -> int:
    """CRC-8 (poly 0x07, init 0x00, no reflection). Trivial to port to C."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if (crc & 0x80) else (crc << 1) & 0xFF
    return crc


@dataclass(frozen=True)
class MarkerPayload:
    """The machine-readable state stamped into the corner barcode + text."""

    output_id: int
    generation: int
    frame: int
    x: int
    y: int
    w: int
    h: int
    scale_x100: int = 100

    def pack(self) -> bytes:
        body = _PAYLOAD_STRUCT.pack(
            MAGIC, VERSION, self.output_id & 0xFF, self.generation & 0xFFFF,
            self.frame & 0xFFFFFFFF, self.x, self.y, self.w, self.h,
            self.scale_x100 & 0xFF,
        )
        return body + bytes([crc8(body)])

    @classmethod
    def unpack(cls, raw: bytes) -> "MarkerPayload":
        if len(raw) < PAYLOAD_LEN:
            raise ValueError(f"payload too short: {len(raw)} < {PAYLOAD_LEN}")
        raw = raw[:PAYLOAD_LEN]
        body, crc = raw[:-1], raw[-1]
        if crc8(body) != crc:
            raise ValueError("payload CRC mismatch")
        (magic, version, output_id, generation, frame, x, y, w, h,
         scale_x100) = _PAYLOAD_STRUCT.unpack(body)
        if magic != MAGIC:
            raise ValueError(f"bad magic {magic!r}")
        if version != VERSION:
            raise ValueError(f"unsupported marker version {version}")
        return cls(output_id, generation, frame, x, y, w, h, scale_x100)


# --------------------------------------------------------------------------
# Bit/cell layout helpers (pure-Python; mirrored by the C renderer/decoder).
# --------------------------------------------------------------------------
def _bytes_to_bits(data: bytes) -> list[int]:
    """MSB-first bit list."""
    bits: list[int] = []
    for byte in data:
        for i in range(7, -1, -1):
            bits.append((byte >> i) & 1)
    return bits


def _bits_to_bytes(bits: list[int]) -> bytes:
    out = bytearray()
    for i in range(0, len(bits) - len(bits) % 8, 8):
        byte = 0
        for b in bits[i : i + 8]:
            byte = (byte << 1) | (b & 1)
        out.append(byte)
    return bytes(out)


def interior_cells() -> list[tuple[int, int]]:
    """(row, col) of data cells, row-major. len == DATA_CAPACITY_BITS."""
    return [(r, c) for r in range(1, GRID_ROWS) for c in range(1, GRID_COLS)]


def cell_matrix(payload: MarkerPayload) -> list[list[int]]:
    """Render the corner block as a GRID_ROWS x GRID_COLS matrix of 0/1
    (0 = black, 1 = white). The contract the C renderer reproduces exactly."""
    grid = [[0] * GRID_COLS for _ in range(GRID_ROWS)]
    grid[0][0] = 0  # origin anchor (black)
    # timing patterns: alternate starting white at index 1.
    for c in range(1, GRID_COLS):
        grid[0][c] = 1 if (c % 2 == 1) else 0
    for r in range(1, GRID_ROWS):
        grid[r][0] = 1 if (r % 2 == 1) else 0
    # data: pad payload bits up to capacity with a fixed 0xA5 pattern so unused
    # cells are deterministic (helps the decoder sanity-check pitch).
    bits = _bytes_to_bits(payload.pack())
    pad = _bytes_to_bits(b"\xa5" * ((DATA_CAPACITY_BITS // 8) + 1))
    bits = (bits + pad)[:DATA_CAPACITY_BITS]
    for (r, c), bit in zip(interior_cells(), bits):
        grid[r][c] = bit
    return grid


def decode_cell_matrix(grid: list[list[int]]) -> MarkerPayload:
    """Inverse of cell_matrix(): read the interior data cells back to a payload."""
    if len(grid) != GRID_ROWS or any(len(row) != GRID_COLS for row in grid):
        raise ValueError("grid is not GRID_ROWS x GRID_COLS")
    bits = [grid[r][c] & 1 for (r, c) in interior_cells()]
    return MarkerPayload.unpack(_bits_to_bytes(bits))


# --------------------------------------------------------------------------
# Geometry of the painted surface (logical px). Bands fill the full surface;
# the corner block + fiducials are drawn on top in fixed sub-rects.
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class BandRect:
    name: str
    color: str
    x0: int
    x1: int  # exclusive


@dataclass(frozen=True)
class MarkerLayout:
    """Logical-coordinate layout of a marker surface of size (width, height).

    ``seam_x`` is the logical x of the output boundary (for spanning tests);
    bands are distributed so that ``seam-left``/``seam-right`` straddle it.
    For whole-window tests pass ``seam_x=width//2`` (bands are still anchors).
    """

    width: int
    height: int
    seam_x: int
    bands: list[BandRect] = field(default_factory=list)

    @property
    def corner_px(self) -> tuple[int, int, int, int]:
        """(x0, y0, x1, y1) logical rect of the corner barcode incl. quiet zone."""
        side_cells = max(GRID_ROWS, GRID_COLS) + 2 * QUIET_CELLS
        side = side_cells * CELL_PX
        return (0, 0, GRID_COLS * CELL_PX + 2 * QUIET_CELLS * CELL_PX,
                GRID_ROWS * CELL_PX + 2 * QUIET_CELLS * CELL_PX)


def compute_layout(width: int, height: int, seam_x: int | None = None) -> MarkerLayout:
    """Distribute the 6 named bands across ``width``.

    The four non-seam bands take equal outer shares; ``seam-left``/``seam-right``
    are two equal bands centred on ``seam_x`` (each ~1/8 width). This keeps the
    seam bands narrow and adjacent so a spanning test can read one on each side
    of the boundary.
    """
    if seam_x is None:
        seam_x = width // 2
    seam_half = max(CELL_PX, width // 8)
    sl_x0 = max(0, seam_x - seam_half)
    sr_x1 = min(width, seam_x + seam_half)
    # left region [0, sl_x0) split into left-anchor + pre-seam
    left_split = sl_x0 // 2
    # right region [sr_x1, width) split into post-seam + right-anchor
    right_split = sr_x1 + (width - sr_x1) // 2
    rects = [
        BandRect("left-anchor", "red", 0, left_split),
        BandRect("pre-seam", "green", left_split, sl_x0),
        BandRect("seam-left", "blue", sl_x0, seam_x),
        BandRect("seam-right", "yellow", seam_x, sr_x1),
        BandRect("post-seam", "green", sr_x1, right_split),
        BandRect("right-anchor", "red", right_split, width),
    ]
    return MarkerLayout(width=width, height=height, seam_x=seam_x, bands=rects)


def band_at(layout: MarkerLayout, x: int) -> BandRect | None:
    for b in layout.bands:
        if b.x0 <= x < b.x1:
            return b
    return None


# --------------------------------------------------------------------------
# Reference renderer (numpy). The C marker client reproduces this pixel-for-
# pixel at scale 1.0; golden/oracle tests render here. numpy is imported lazily
# so the codec above stays usable with no imaging libs installed.
# --------------------------------------------------------------------------
def fiducial_origin(band: BandRect, corner_y1: int) -> tuple[int, int]:
    """Logical (x, y) top-left of a band's 8x8 checker fiducial."""
    fx = band.x0 + 2
    fy = corner_y1 + 4  # just below the corner block band-row
    return fx, fy


def render_rgb(layout: MarkerLayout, payload: MarkerPayload, scale: float = 1.0):
    """Render the marker surface to an (H, W, 3) uint8 numpy array.

    Physical size = logical size * scale. ``payload.scale_x100`` should equal
    ``round(scale*100)`` so the oracle can cross-check stamped vs measured scale
    (hidden-scaling detection).
    """
    import numpy as np

    def sx(v: float) -> int:
        return int(round(v * scale))

    W, H = sx(layout.width), sx(layout.height)
    img = np.zeros((H, W, 3), dtype=np.uint8)

    # 1) bands fill the full surface.
    for b in layout.bands:
        x0, x1 = sx(b.x0), sx(b.x1)
        img[:, x0:x1] = PALETTE[b.color]

    # 2) fiducial 8x8 checkers inside each band (scale detection).
    fcell = max(1, sx(FIDUCIAL_CELL_PX))
    for b in layout.bands:
        fx, fy = fiducial_origin(b, layout.corner_px[3])
        ox, oy = sx(fx), sx(fy)
        for r in range(FIDUCIAL_CELLS):
            for c in range(FIDUCIAL_CELLS):
                val = (255, 255, 255) if (r + c) % 2 == 0 else (0, 0, 0)
                y0p, x0p = oy + r * fcell, ox + c * fcell
                if y0p + fcell <= H and x0p + fcell <= W:
                    img[y0p : y0p + fcell, x0p : x0p + fcell] = val

    # 3) corner barcode (quiet zone white, then cells), top-left.
    grid = cell_matrix(payload)
    cpx = max(1, sx(CELL_PX))
    qz = QUIET_CELLS * cpx
    cx0, cy0, cx1, cy1 = (sx(v) for v in layout.corner_px)
    img[cy0:cy1, cx0:cx1] = (255, 255, 255)  # quiet zone
    for r in range(GRID_ROWS):
        for c in range(GRID_COLS):
            val = 255 if grid[r][c] else 0
            y0p, x0p = cy0 + qz + r * cpx, cx0 + qz + c * cpx
            img[y0p : y0p + cpx, x0p : x0p + cpx] = (val, val, val)

    return img

