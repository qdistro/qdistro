#!/bin/bash
# Run this as admin inside the labwc Wayland session to focus the admin
# approvals window and send Ctrl+Y to approve the currently-selected
# pending request.
set -u
export WAYLAND_DISPLAY=wayland-0
export XDG_RUNTIME_DIR=/run/user/1000

echo "[approve] toplevels:"
wlrctl toplevel list || true

echo "[approve] focusing admin approvals"
wlrctl toplevel focus title:'admin approvals' || true
sleep 0.4

echo "[approve] sending Ctrl+Y"
wtype -M ctrl -k y -m ctrl
sleep 0.4

echo "[approve] done"
