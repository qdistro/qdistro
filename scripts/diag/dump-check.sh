#!/bin/bash
DUMP=/tmp/qfwd-dump.ppm
if [ ! -s "$DUMP" ]; then
    echo "FAIL: no dump"
    exit 1
fi
ls -la "$DUMP"
echo ---
head -c 40 "$DUMP"
echo
echo ---
python3 - <<EOF
data = open('$DUMP','rb').read()
idx = 0
for _ in range(3):
    nl = data.find(b'\n', idx)
    if nl < 0:
        raise SystemExit('bad ppm')
    idx = nl + 1
pixels = data[idx:]
nz = sum(1 for b in pixels if b != 0)
total = len(pixels)
ratio = nz / max(total, 1)
print(f'nz={nz}/{total} ratio={ratio:.4f}')
# Sample: are pixels uniform or varied?
unique_bytes = len(set(pixels[:1000]))
print(f'first1000_unique_bytes={unique_bytes}')
EOF
