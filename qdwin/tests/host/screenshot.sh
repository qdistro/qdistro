#!/bin/bash
# Capture qdwin's output to a known PNG path.
#
# Usage: screenshot.sh <test-id> <name>
#   -> writes $TEST_ROOT/<id>/shots/<name>.png
#   -> echoes the path
#
# Wraps weston-screenshooter, which writes to CWD with a fixed
# wayland-screenshot-YYYY-MM-DD_HH-MM-SS.png pattern (no -o flag
# in the Tumbleweed build). We cd into the shots dir, run it,
# rename the newest match.

set -eo pipefail
. "$(dirname "$0")/lib.sh"

TEST_ID=${1:?usage: screenshot.sh <test-id> <name>}
NAME=${2:?usage: screenshot.sh <test-id> <name>}

RUNTIME=$(ht_runtime "$TEST_ID")
SOCK=$(ht_socket "$TEST_ID")
SHOTS=$(ht_shots "$TEST_ID")
mkdir -p "$SHOTS"

OUT="$SHOTS/$NAME.png"
(
    cd "$SHOTS"
    XDG_RUNTIME_DIR="$RUNTIME" WAYLAND_DISPLAY="$SOCK" \
        weston-screenshooter >/dev/null 2>&1
    # Newest wayland-screenshot-*.png becomes our named file.
    fresh=$(ls -1t wayland-screenshot-*.png 2>/dev/null | head -1)
    [ -n "$fresh" ] || { echo "[screenshot.sh] weston-screenshooter produced no file" >&2; exit 9; }
    mv "$fresh" "$NAME.png"
)
echo "$OUT"
