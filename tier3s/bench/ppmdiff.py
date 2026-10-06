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
ys, xs = np.nonzero(mask)
print("diff", xs.min(), ys.min(), xs.max(), ys.max(), int(mask.sum()))
