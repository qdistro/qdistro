#!/bin/bash
# Phase 1.7: labwc side-by-side parity harness for qdwin smoke output.
#
# Goal: render the same layer-shell client stack (waybar + noctalia)
# under labwc instead of qdwin, capture a screenshot, and write it
# alongside the qdwin reference for manual visual comparison. NOT
# load-bearing — qdwin is the only supported compositor — but a
# useful regression diff when libweston-desktop or noctalia bumps
# expose differences in popup positioning / fractional scale / cursor
# handling.
#
# Run inside a graphical-capable VM (clone of weston-desktop-* or
# layershell-*). Requires: labwc, waybar, noctalia-shell, grim or
# weston-screenshooter. Auto-installs labwc if missing.
#
# Output:
#   /tmp/labwc-noct.log
#   /tmp/labwc-parity-<timestamp>.png
#
# Compare against the saved qdwin reference at
#   tests/integration/permissions-gui/screenshots/qdshell-first-light-260503.png
# The diff is informational only.
set -u
set +e

DURATION="${DURATION:-15}"
OUT_DIR="${OUT_DIR:-/tmp}"
TS="$(date +%Y%m%d-%H%M%S)"
PNG="$OUT_DIR/labwc-parity-$TS.png"

if ! command -v labwc >/dev/null 2>&1; then
    echo "[parity] labwc not installed — installing..."
    if ! zypper -n install labwc grim wlr-randr 2>&1 | tail -3; then
        echo "[parity] WARN: labwc install failed; aborting"
        exit 2
    fi
fi
if ! command -v grim >/dev/null 2>&1; then
    echo "[parity] grim still missing after install; aborting"
    exit 2
fi

export XDG_RUNTIME_DIR=/run/user/1000
mkdir -p "$XDG_RUNTIME_DIR"
chown admin:users "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"

rm -f /tmp/labwc-noct.log /tmp/labwc-qdshell.log /tmp/labwc-grim.log

# labwc reads ~/.config/labwc/{rc.xml,environment}; defaults are fine
# for a smoke. Use the headless backend since the test VM has no real
# DRM device. labwc auto-picks the next free wayland-N socket name;
# we read the actual chosen name from its debug log rather than
# trying to force one (no labwc CLI flag for this).
runuser -u admin -- bash -c '
    export XDG_RUNTIME_DIR=/run/user/1000
    export WLR_BACKENDS=headless
    # WLR_RENDERER=pixman forces CPU rendering. Without it, the
    # headless backend still tries GBM/DRM allocations and falls
    # over with DRM_IOCTL_MODE_CREATE_DUMB Permission-denied in
    # any VM where admin lacks render+video group membership.
    export WLR_RENDERER=pixman
    export WLR_HEADLESS_OUTPUTS=1
    export WLR_LIBINPUT_NO_DEVICES=1
    # -d enables the debug logging that surfaces "WAYLAND_DISPLAY=..."
    exec labwc -d -s "sleep '"$DURATION"'" \
        > /tmp/labwc-noct.log 2>&1
' &
LBP=$!
echo "labwc pid=$LBP"

WAYLAND_SOCK=""
for i in 1 2 3 4 5 6 7 8 9 10; do
    sleep 1
    cand=$(grep -oE 'WAYLAND_DISPLAY=wayland-[0-9]+' /tmp/labwc-noct.log 2>/dev/null \
            | tail -1 | cut -d= -f2)
    if [ -n "$cand" ] && [ -S "$XDG_RUNTIME_DIR/$cand" ]; then
        WAYLAND_SOCK=$cand
        echo "labwc socket ready after ${i}s on $WAYLAND_SOCK"
        break
    fi
done
if [ -z "$WAYLAND_SOCK" ]; then
    echo "ERROR: labwc socket not appearing"
    cat /tmp/labwc-noct.log
    kill $LBP 2>/dev/null
    exit 1
fi

# Launch noctalia/qdshell against labwc.
runuser -u admin -- bash -c "
    export XDG_RUNTIME_DIR=/run/user/1000
    export WAYLAND_DISPLAY=$WAYLAND_SOCK
    export QML_DISABLE_DISK_CACHE=1
    timeout $((DURATION - 2)) dbus-run-session -- \
        qs -p /usr/share/quickshell/qdshell \
        > /tmp/labwc-qdshell.log 2>&1
" &
QPID=$!

# Give the shell time to render.
sleep $((DURATION - 4))

# Capture via grim. labwc supports wlr-screencopy out of the box.
runuser -u admin -- bash -c "
    export XDG_RUNTIME_DIR=/run/user/1000
    export WAYLAND_DISPLAY=$WAYLAND_SOCK
    grim '$PNG' 2>/tmp/labwc-grim.log
"
GRC=$?

wait $QPID 2>/dev/null
kill $LBP 2>/dev/null
wait $LBP 2>/dev/null

echo ""
echo "=== labwc-noct.log: errors / criticals ==="
grep -nE 'error|critical|fatal|FAIL|invalid' /tmp/labwc-noct.log | head -40 || echo "(none)"
echo ""
echo "=== qdshell on labwc: first 20 log lines ==="
head -20 /tmp/labwc-qdshell.log

if [ "$GRC" = "0" ] && [ -s "$PNG" ]; then
    echo ""
    echo "PASS: parity capture saved → $PNG"
    echo "Compare against qdwin reference at"
    echo "  tests/integration/permissions-gui/screenshots/qdshell-first-light-260503.png"
    exit 0
fi

echo "FAIL: grim capture missing or empty"
exit 1
