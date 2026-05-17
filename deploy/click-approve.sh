#!/bin/bash
# Run as admin with DISPLAY=:0. Find admin approvals window, focus it,
# click Approve via window-relative coordinates (avoids rootless
# Xwayland's lack of global mouse cursor manipulation).
set -u
export DISPLAY=:0
WID=$(xdotool search --name 'admin approvals' | head -1)
if [ -z "$WID" ]; then
    echo "[click-approve] no window found"
    exit 1
fi
echo "[click-approve] WID=$WID"
xdotool getwindowgeometry "$WID"
xdotool windowactivate --sync "$WID"
sleep 0.3
# Window-relative click on the Approve button. Layout: queue list ~290px wide,
# then detail pane with Scope row and buttons row. Approve button is in the
# detail pane around x=50 (relative to detail), y=174 from top of window.
# Approve abs coords from screenshot: (~528, ~274). Window pos via geometry.
xdotool windowactivate --sync "$WID"
sleep 0.2
# Try keyboard shortcut first (Ctrl+Y is wired in admin app), then fall
# back to coord-based click. Window-relative coords: Approve button is
# at ~(338, 133) from window origin per screenshot inspection.
xdotool key --window "$WID" --clearmodifiers ctrl+y
sleep 0.3
xdotool mousemove --window "$WID" --sync 338 133 click 1
sleep 0.3
echo "[click-approve] done"
