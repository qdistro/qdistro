#!/bin/bash
# Desktop launcher for the installed approval UI. The GUI test VM uses the
# separate start-admin-app.sh launcher because its XWayland display is driven
# by xdotool; the installed qdwin session runs native Wayland.
set -euo pipefail

if [ "$(id -u)" != 1000 ]; then
    echo "qdistro admin approvals: launch from the admin (uid 1000) session" >&2
    exit 1
fi

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/1000}"
export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-1}"
case "$WAYLAND_DISPLAY" in
    /*) DISPLAY_SOCKET=$WAYLAND_DISPLAY ;;
    *) DISPLAY_SOCKET=$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY ;;
esac
if [ ! -S "$DISPLAY_SOCKET" ]; then
    echo "qdistro admin approvals: no Wayland display at $DISPLAY_SOCKET" >&2
    exit 1
fi
export QT_QPA_PLATFORM=wayland
exec /usr/bin/python3 /usr/libexec/qdistro/qdistro_admin_app.py "$@"
