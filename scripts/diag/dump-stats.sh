#!/bin/bash
DUMP=/tmp/qfwd-dump.ppm
python3 - <<'EOF'
import struct
data = open('/tmp/qfwd-dump.ppm','rb').read()
idx = 0
for _ in range(3):
    idx = data.find(b'\n', idx) + 1
pixels = data[idx:]
W, H = 640, 480
print(f"pixels={len(pixels)} expected={W*H*3}")
# Sample 5 rows: top, q1, mid, q3, bottom.
for label, y in [('top0', 0), ('q1', 120), ('mid', 240), ('q3', 360), ('bot', 479)]:
    row = pixels[y*W*3:(y+1)*W*3]
    nz = sum(1 for b in row if b != 0)
    uniq = len(set(row))
    sample = row[100*3:103*3].hex()
    print(f"row {label} y={y}: nz={nz}/{len(row)} uniq_bytes={uniq} sample@100={sample}")
EOF
