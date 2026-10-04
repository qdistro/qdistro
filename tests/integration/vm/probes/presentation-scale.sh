#!/bin/bash
# In-VM driver for the presentation-live.bats scale test.
#
# Sets the outer qdwin output to scale 2 through wlr-output-management
# (wlr-randr), runs presentation-scale.py expecting devicePixelRatio 2,
# then restores scale 1 and runs it again. The UI font must stay in
# points at both scales. The original scale is restored on any exit.

set -u

PROBE=${1:-/tmp/presentation-scale.py}
RUNTIME_DIR=/run/user/1000

as_admin() {
    runuser -u admin -- env XDG_RUNTIME_DIR="$RUNTIME_DIR" WAYLAND_DISPLAY=wayland-1 "$@"
}

command -v wlr-randr >/dev/null 2>&1 || { echo "FAIL: wlr-randr not installed"; exit 1; }
OUTPUT=$(as_admin wlr-randr 2>/dev/null | awk 'NR==1 {print $1}')
[ -n "$OUTPUT" ] || { echo "FAIL: wlr-randr lists no output"; exit 1; }
ORIG_SCALE=$(as_admin wlr-randr 2>/dev/null | awk '/^[^ ]/ {n++} n==1 && $1=="Scale:" {print $2; exit}')
ORIG_SCALE=${ORIG_SCALE:-1}
echo "[presentation-scale] output=$OUTPUT original scale=$ORIG_SCALE"
trap 'as_admin wlr-randr --output "$OUTPUT" --scale "$ORIG_SCALE" >/dev/null 2>&1 || true' EXIT

rc=0
for scale in 2 1; do
    if ! as_admin wlr-randr --output "$OUTPUT" --scale "$scale"; then
        echo "FAIL: wlr-randr could not set $OUTPUT scale $scale"
        rc=1
        continue
    fi
    sleep 1
    echo "PASS: compositor output $OUTPUT scale set to $scale"
    as_admin env QT_QPA_PLATFORM=wayland PYTHONSAFEPATH=1 \
        python3 "$PROBE" --expect-dpr "$scale" | sed "s/^\(PASS\|FAIL\): /\1: [scale $scale] /"
    [ "${PIPESTATUS[0]}" -eq 0 ] || rc=1
done
exit "$rc"
