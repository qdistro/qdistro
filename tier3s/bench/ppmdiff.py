#!/usr/bin/env python3
# ppmdiff.py a.img b.img -> "same" | "diff x0 y0 x1 y1 count"
# Any image format PIL decodes (virsh screenshot emits PNG on this
# libvirt). Identical geometry required.
import sys

import numpy as np
from PIL import Image


def load(p):
    with Image.open(p) as im:
        return np.asarray(im.convert("RGB"))


try:
    a = load(sys.argv[1])
    b = load(sys.argv[2])
except Exception as e:  # noqa: BLE001 — report decode failures as data
    print("error", e)
    sys.exit(2)

if a.shape != b.shape:
    print("error geometry")
    sys.exit(2)

mask = np.any(a != b, axis=2)
if not mask.any():
    print("same")
    sys.exit(0)

mode = sys.argv[3] if len(sys.argv) > 3 else "diff"
if mode == "rect":
    # bbox of the largest dense block: rows/cols carrying >10% of the
    # diff pixels, so a tiny panel-clock blip can't merge into a window
    # rect
    rows = np.nonzero(mask.sum(axis=1) > max(8, mask.shape[1] // 20))[0]
    cols = np.nonzero(mask.sum(axis=0) > max(8, mask.shape[0] // 20))[0]
    if len(rows) == 0 or len(cols) == 0:
        print("error no-dense-region")
        sys.exit(2)
    print("diff", int(cols[0]), int(rows[0]), int(cols[-1]), int(rows[-1]),
          int(mask[rows[0]:rows[-1] + 1, cols[0]:cols[-1] + 1].sum()))
else:
    ys, xs = np.nonzero(mask)
    print("diff", xs.min(), ys.min(), xs.max(), ys.max(), int(mask.sum()))
