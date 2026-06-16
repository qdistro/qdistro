"""Deterministic pixel oracle for the two-VM display harness.

The *strong* pass/fail signal (the vision agent in ``agent.py`` is only a
secondary reviewer). Operates on a captured RGB image plus the known marker
``MarkerLayout`` and the physical ``scale`` at which it was rendered, and:

- decodes the corner barcode back to a :class:`~.marker.MarkerPayload`
  (output/generation/frame/geometry/scale) — robust to RDP colour jitter via
  luminance thresholding;
- classifies each colour band by region majority within a per-channel colour
  distance tolerance (8-16 for lossless/pixman, 24-40 for RDP-decode paths —
  codex r6), ignoring a guard band around band edges;
- measures scale independently from the corner timing pattern and flags
  *hidden scaling* when measured scale disagrees with the stamped
  ``scale_x100`` (a monitor-extension invalidator per 09);
- checks stamped generation against the active dock generation (stale-frame
  rejection, D3).

All inputs are numpy uint8 (H, W, 3). The oracle never declares "feels native";
it declares geometry/identity/colour correctness only (09 guardrails).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import marker as M

# Default colour-distance tolerances (per-channel max abs difference).
TOL_LOSSLESS = 12
TOL_RDP = 32
# Region-majority fraction required for a band to classify correctly.
MAJORITY = 0.92
# Logical px guard band ignored around each band edge.
GUARD_LOGICAL = 4


@dataclass
class BandResult:
    name: str
    expected: str
    classified: str
    majority: float
    ok: bool


@dataclass
class OracleResult:
    ok: bool
    payload: M.MarkerPayload | None
    payload_error: str | None
    bands: list[BandResult] = field(default_factory=list)
    measured_scale: float | None = None
    hidden_scaling: bool = False
    stale_generation: bool = False
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        bad = [b.name for b in self.bands if not b.ok]
        parts = [f"ok={self.ok}"]
        if self.payload is not None:
            parts.append(
                f"out={self.payload.output_id} gen={self.payload.generation} "
                f"frame={self.payload.frame}"
            )
        if self.payload_error:
            parts.append(f"payload_err={self.payload_error}")
        if bad:
            parts.append(f"bad_bands={bad}")
        if self.hidden_scaling:
            parts.append(f"hidden_scaling measured={self.measured_scale}")
        if self.stale_generation:
            parts.append("STALE_GENERATION")
        return " ".join(parts)


def _color_dist(patch: np.ndarray, color: tuple[int, int, int]) -> np.ndarray:
    """Per-pixel max per-channel abs difference to ``color``."""
    ref = np.array(color, dtype=np.int16)
    diff = np.abs(patch.astype(np.int16) - ref)
    return diff.max(axis=-1)


def classify_color(patch: np.ndarray, tol: int) -> tuple[str, float]:
    """Return (nearest palette colour name, fraction of pixels within tol).

    A pixel is attributed to the *single* palette colour it is within ``tol``
    of (max per-channel). If it is within tol of several, the nearest wins.
    """
    px = patch.reshape(-1, 3).astype(np.int16)
    best_name = "unknown"
    best_frac = 0.0
    # nearest-colour assignment per pixel
    names = list(M.PALETTE)
    dists = np.stack([_color_dist(px, M.PALETTE[n]) for n in names], axis=0)
    nearest = dists.argmin(axis=0)
    nearest_dist = dists.min(axis=0)
    within = nearest_dist <= tol
    for i, n in enumerate(names):
        frac = float(np.count_nonzero(within & (nearest == i))) / max(1, px.shape[0])
        if frac > best_frac:
            best_frac, best_name = frac, n
    return best_name, best_frac


def _sample_cell(img: np.ndarray, cx: float, cy: float, cell_px: float) -> int:
    """Sample one barcode cell centre; return 1 (white) or 0 (black)."""
    half = max(1, int(cell_px * 0.3))
    y, x = int(round(cy)), int(round(cx))
    y0, y1 = max(0, y - half), min(img.shape[0], y + half + 1)
    x0, x1 = max(0, x - half), min(img.shape[1], x + half + 1)
    patch = img[y0:y1, x0:x1]
    lum = patch.astype(np.float32).mean()
    return 1 if lum >= 128 else 0


def decode_corner(
    img: np.ndarray, layout: M.MarkerLayout, scale: float,
    origin: tuple[int, int] = (0, 0),
) -> M.MarkerPayload:
    """Decode the corner barcode using known geometry.

    ``origin`` is the physical-pixel (x, y) of the marker surface's top-left
    within ``img`` (0,0 when the capture is exactly the surface).
    """
    cpx = M.CELL_PX * scale
    qz = M.QUIET_CELLS * cpx
    ox, oy = origin
    base_x = ox + qz
    base_y = oy + qz
    grid = [[0] * M.GRID_COLS for _ in range(M.GRID_ROWS)]
    for r in range(M.GRID_ROWS):
        for c in range(M.GRID_COLS):
            cx = base_x + (c + 0.5) * cpx
            cy = base_y + (r + 0.5) * cpx
            grid[r][c] = _sample_cell(img, cx, cy, cpx)
    return M.decode_cell_matrix(grid)


def measure_scale_from_corner(
    img: np.ndarray, layout: M.MarkerLayout, expected_scale: float,
    origin: tuple[int, int] = (0, 0),
) -> float:
    """Measure the physical cell pitch from the corner timing row in IMAGE space.

    ``expected_scale`` is used ONLY to pick a y inside the timing row; the pitch
    itself is derived from the alternating black/white run widths in the captured
    pixels (codex impl-3 finding 3 — not from the stamped/caller scale). The
    timing row is the origin anchor (black) then alternating white/black cells,
    so consecutive same-luminance runs are one cell wide each. Averaging run
    widths over many cells is robust to RDP jitter and detects hidden client
    scaling (a uniformly resampled capture changes the run widths).

    Returns the measured scale (pitch / CELL_PX), or ``expected_scale`` if the
    pattern cannot be read.
    """
    cpx_exp = M.CELL_PX * expected_scale
    qz = M.QUIET_CELLS * cpx_exp
    ox, oy = origin
    y = int(round(oy + qz + 0.5 * cpx_exp))
    y = min(max(0, y), img.shape[0] - 1)
    row = img[y].astype(np.float32).mean(axis=-1)
    x = int(max(0, ox))
    # advance through the white quiet zone to the first black run (the anchor).
    while x < row.shape[0] and row[x] >= 128:
        x += 1
    # collect consecutive same-luminance run widths across the timing row, until
    # a run is implausibly wide (the grid merged into the right quiet zone / band
    # content) or we have measured the whole grid.
    runs: list[int] = []
    lo, hi = 0.3 * cpx_exp, 3.0 * cpx_exp
    while x < row.shape[0] and len(runs) < M.GRID_COLS:
        black = row[x] < 128
        run_start = x
        while x < row.shape[0] and (row[x] < 128) == black:
            x += 1
        w = x - run_start
        if w > hi:          # merged into quiet zone / band content — stop.
            break
        if w >= lo:         # cell-sized run.
            runs.append(w)
        else:               # sub-cell noise run; keep scanning.
            continue
    if len(runs) < 3:
        return expected_scale
    pitch = float(np.median(runs))
    return pitch / M.CELL_PX


def classify_bands(
    img: np.ndarray, layout: M.MarkerLayout, scale: float,
    tol: int = TOL_LOSSLESS, origin: tuple[int, int] = (0, 0),
    skip_corner_rows: bool = True,
) -> list[BandResult]:
    """Classify each band's colour by region majority, ignoring guard bands."""
    ox, oy = origin
    guard = int(round(GUARD_LOGICAL * scale))
    # sample a horizontal strip below the corner block (avoids barcode/fiducial).
    y_top = oy + int(round((layout.corner_px[3] + M.FIDUCIAL_CELLS * M.FIDUCIAL_CELL_PX + 8) * scale))
    y_bot = oy + int(round((layout.height - GUARD_LOGICAL) * scale))
    y_top = min(max(0, y_top), img.shape[0] - 2)
    y_bot = min(max(y_top + 1, y_bot), img.shape[0])
    results: list[BandResult] = []
    for b in layout.bands:
        # a band with no positive logical width is not sampleable: sampling its
        # "centre" would read an adjacent band and falsely fail it. Mark it
        # not-applicable (ok=True, classified="n/a") rather than misclassify
        # (codex impl-3 finding 9). compute_layout can produce zero-width outer
        # bands near a seam/edge.
        if b.x1 - b.x0 <= 0:
            results.append(BandResult(b.name, b.color, "n/a", 0.0, True))
            continue
        x0 = ox + int(round(b.x0 * scale)) + guard
        x1 = ox + int(round(b.x1 * scale)) - guard
        if x1 - x0 < 2:  # band narrow after guard; sample within its own extent
            bx0 = ox + int(round(b.x0 * scale))
            bx1 = ox + int(round(b.x1 * scale))
            if bx1 - bx0 < 1:   # collapses to <1px after scaling — not sampleable
                results.append(BandResult(b.name, b.color, "n/a", 0.0, True))
                continue
            xc = (bx0 + bx1) // 2
            x0, x1 = max(bx0, xc - 1), min(bx1, xc + 1)
        x0, x1 = max(0, x0), min(img.shape[1], x1)
        patch = img[y_top:y_bot, x0:x1]
        name, frac = classify_color(patch, tol) if patch.size else ("unknown", 0.0)
        results.append(BandResult(b.name, b.color, name, frac,
                                  name == b.color and frac >= MAJORITY))
    return results


def evaluate(
    img: np.ndarray, layout: M.MarkerLayout, scale: float,
    *, tol: int = TOL_LOSSLESS, origin: tuple[int, int] = (0, 0),
    active_generation: int | None = None,
    expect_output_id: int | None = None,
) -> OracleResult:
    """Full oracle pass over one capture against the expected marker contract."""
    res = OracleResult(ok=False, payload=None, payload_error=None)

    try:
        res.payload = decode_corner(img, layout, scale, origin=origin)
    except Exception as e:  # noqa: BLE001
        res.payload_error = str(e)

    res.bands = classify_bands(img, layout, scale, tol=tol, origin=origin)

    try:
        res.measured_scale = measure_scale_from_corner(
            img, layout, scale, origin=origin)
    except Exception as e:  # noqa: BLE001
        res.notes.append(f"scale-measure failed: {e}")

    # hidden-scaling: the scale measured from the captured pixels must agree
    # with BOTH (a) the scale the harness expected (the ``scale`` arg — what the
    # output mode / no-client-scaling contract says it should be) and (b) the
    # stamped ``scale_x100`` when the barcode decoded. Comparing against the
    # expected scale catches hidden client scaling even when the mis-scaled
    # capture makes the barcode itself undecodable (codex impl-3 finding 3).
    if res.measured_scale is not None:
        if abs(res.measured_scale - scale) > 0.15 * max(scale, 0.1):
            res.hidden_scaling = True
            res.notes.append(
                f"hidden scaling: expected={scale:.2f} "
                f"measured={res.measured_scale:.2f}")
        if res.payload is not None:
            stamped = res.payload.scale_x100 / 100.0
            if abs(res.measured_scale - stamped) > 0.15 * max(stamped, 0.1):
                res.hidden_scaling = True
                res.notes.append(
                    f"hidden scaling: stamped={stamped:.2f} "
                    f"measured={res.measured_scale:.2f}")

    # stale-generation (D3): a frame stamped with a non-active generation.
    if active_generation is not None and res.payload is not None:
        if res.payload.generation != active_generation:
            res.stale_generation = True
            res.notes.append(
                f"stale generation {res.payload.generation} != active {active_generation}")

    if expect_output_id is not None and res.payload is not None:
        if res.payload.output_id != expect_output_id:
            res.notes.append(
                f"output_id {res.payload.output_id} != expected {expect_output_id}")

    bands_ok = all(b.ok for b in res.bands)
    res.ok = (
        res.payload is not None
        and bands_ok
        and not res.hidden_scaling
        and not res.stale_generation
        and (expect_output_id is None or res.payload.output_id == expect_output_id)
    )
    return res
