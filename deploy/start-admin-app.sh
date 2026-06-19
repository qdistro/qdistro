#!/bin/bash
# Start the admin approval app inside the uid-1000 admin's running
# Wayland session. Self-elevating: drops from root to the uid-1000
# user when invoked via vm-exec so the app connects to the broker
# with the admin uid the broker enforces. Works regardless of whether
# the OS account is named `admin` or `jan`.
set -u
if [ "$(id -u)" = "0" ]; then
    ADMIN_USER=$(getent passwd 1000 | cut -d: -f1)
    if [ -z "$ADMIN_USER" ]; then
        echo "start-admin-app: no uid-1000 user on this VM" >&2
        exit 2
    fi
    exec runuser -u "$ADMIN_USER" -- "$0" "$@"
fi
export XDG_RUNTIME_DIR=/run/user/1000
if [ -z "${WAYLAND_DISPLAY:-}" ]; then
    if [ -S "$XDG_RUNTIME_DIR/wayland-0" ]; then
        export WAYLAND_DISPLAY=wayland-0
    elif [ -S "$XDG_RUNTIME_DIR/wayland-1" ]; then
        export WAYLAND_DISPLAY=wayland-1
    fi
fi
export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
export QT_QPA_PLATFORM=xcb
export DISPLAY=${DISPLAY:-:0}
ADMIN_HOME=$(getent passwd "$(id -u)" | cut -d: -f6)
APP_PY="$ADMIN_HOME/qdistro/admin_app/qdistro_admin_app.py"
[ -f "$APP_PY" ] || APP_PY=/home/admin/qdistro/admin_app/qdistro_admin_app.py
setsid python3 "$APP_PY" < /dev/null > /tmp/admin-app.log 2>&1 &
disown
echo "$!"
