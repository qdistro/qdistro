#!/usr/bin/env python3
# ppmdiff.py a.ppm b.ppm -> "same" | "diff x0 y0 x1 y1 count"
# Binary P6 (as produced by `virsh screenshot`) only. Identical
# geometry required. Row/column byte-slice compares keep this at C
# speed (no per-pixel python loop).
import sys


def load(p):
    with open(p, "rb") as f:
        data = f.read()
    parts, i = [], 0
    while len(parts) < 4:
        while data[i:i+1].isspace() or data[i:i+1] == b"#":
            if data[i:i+1] == b"#":
                while data[i:i+1] != b"\n":
                    i += 1
            i += 1
        j = i
        while not data[j:j+1].isspace():
            j += 1
        parts.append(data[i:j]); i = j
    i += 1  # single whitespace before raster
    magic, w, h, mv = parts[0], int(parts[1]), int(parts[2]), int(parts[3])
    if magic != b"P6" or mv != 255:
        raise SystemExit(f"{p}: not a P6/255 ppm")
    return w, h, data[i:i + w * h * 3]


try:
    wa, ha, ra = load(sys.argv[1])
    wb, hb, rb = load(sys.argv[2])
except (OSError, ValueError, IndexError) as e:
    print("error", e)
    sys.exit(2)

if (wa, ha) != (wb, hb):
    print("error geometry")
    sys.exit(2)

if ra == rb:
    print("same")
    sys.exit(0)

stride = wa * 3
rows = [y for y in range(ha)
        if ra[y*stride:(y+1)*stride] != rb[y*stride:(y+1)*stride]]
y0, y1 = rows[0], rows[-1]
cols = [x for x in range(wa)
        if ra[x*3::stride] != rb[x*3::stride]]
x0, x1 = cols[0], cols[-1]
n = 0
for y in rows:
    ro = y * stride
    for x in range(x0, x1 + 1):
        if ra[ro + x*3:ro + x*3 + 3] != rb[ro + x*3:ro + x*3 + 3]:
            n += 1
print("diff", x0, y0, x1, y1, n)
