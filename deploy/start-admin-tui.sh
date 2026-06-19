#!/bin/bash
# Launch qterminal inside the uid-1000 admin's running Wayland session
# with the qdistro admin TUI as its only child process. Mirrors the env
# plumbing of start-admin-app.sh so scenarios don't have to set
# XDG_RUNTIME_DIR / DBUS_SESSION_BUS_ADDRESS / WAYLAND_DISPLAY
# themselves.
#
# Self-elevating: if invoked as root (e.g. directly via `vm-exec`),
# re-exec under the uid-1000 user via runuser so the spawned TUI
# connects to the broker with the admin uid the broker enforces
# (DecideRequest is restricted to uid 1000). Why this matters: a
# common test mistake is to call the launcher from a root shell and
# see the TUI come up but every approve/deny rejected with
# `DecideRequest restricted to admin uid 1000; got 0`. Detecting the
# uid here keeps the scenario robust regardless of whether the OS
# account is named `admin` or `jan`.
set -u
if [ "$(id -u)" = "0" ]; then
    ADMIN_USER=$(getent passwd 1000 | cut -d: -f1)
    if [ -z "$ADMIN_USER" ]; then
        echo "start-admin-tui: no uid-1000 user on this VM" >&2
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
export XDG_SESSION_TYPE=wayland
export XDG_CURRENT_DESKTOP=${XDG_CURRENT_DESKTOP:-qdistro}
export DISPLAY=${DISPLAY:-:0}
setsid qterminal -e /usr/local/bin/qdistro-admin-tui \
    </dev/null >/tmp/qterminal-tui.log 2>&1 &
disown
echo "$!"
