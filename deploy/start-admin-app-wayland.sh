#!/bin/bash
# Desktop launcher for the installed approval UI. The labwc GUI test lane uses
# the separate start-admin-app.sh launcher because its XWayland display is
# driven by xdotool; the installed qdwin session runs native Wayland, and so
# does the qdwin GUI test lane, which calls THIS launcher.
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

# QDISTRO_ADMIN_APP_WAIT_PAINTED=1: return only once the window has painted
# its first frame, printing the app's pid. This is the same contract as the
# GUI test lane's X11 launcher (start-admin-app.sh): a caller that captures the
# screen right after launching must not get the bare desktop or a half-drawn
# window. A created, titled xdg_toplevel says nothing about a painted frame, so
# the app itself reports it: it creates QDISTRO_ADMIN_APP_READY_FILE after the
# first paint of its exposed window, whose backing-store flush is the
# wl_surface commit of that frame (_FirstPaintMarker in the app).
#
# What the app cannot report on Wayland is that the COMPOSITOR has processed
# that commit: QGuiApplication.sync(), the X11 launcher's round trip, is a
# no-op on Qt's Wayland platform (checked with WAYLAND_DEBUG on Qt 6.11), and
# PyQt6 exposes no handle to the app's wl_display. The compositor itself can:
# qdwin logs `mapped handle=` while processing a window's first buffer. The
# qdwin GUI lane's qdwin_start_admin_app (ci/lib/guest/gui-waiters.sh) waits
# for that line for this pid after this launcher returns.
#
# Opt-in, not the desktop default: a desktop launch keeps `exec`, so the pid
# the shell started IS the app (launch records and lineage key on it) and no
# helper outlives its purpose. In this mode the app is started detached, with
# its output in a private log (the launcher's own stdout carries only the pid:
# a detached child must not hold a caller's pipe open).
#
# Exit status: 0 painted; 3 the app died first, painted nothing within
# QDISTRO_ADMIN_APP_READY_TIMEOUT (60 s), or no private directory could be made
# for the marker (the app is then still started, but nothing is proved).
if [ "${QDISTRO_ADMIN_APP_WAIT_PAINTED:-0}" = 1 ]; then
    # $XDG_RUNTIME_DIR is this user's own 0700 directory, so no other uid can
    # plant or swap anything in it; mktemp still creates every name fresh.
    LOG=$(mktemp "$XDG_RUNTIME_DIR/qdistro-admin-app.XXXXXXXX.log" 2>/dev/null) || LOG=/dev/null
    READY_DIR=$(mktemp -d "$XDG_RUNTIME_DIR/qdistro-admin-app-ready.XXXXXXXX" 2>/dev/null) || READY_DIR=
    echo "qdistro admin approvals: app log $LOG" >&2
    if [ -z "$READY_DIR" ] || [ ! -d "$READY_DIR" ]; then
        # Never fall back to a fixed marker path such as /painted.
        unset QDISTRO_ADMIN_APP_READY_FILE
        setsid /usr/bin/python3 /usr/local/bin/qdistro-admin-approval-app "$@" \
            </dev/null >>"$LOG" 2>&1 &
        APP_PID=$!
        echo "qdistro admin approvals: no private directory for the first-paint marker; app (pid $APP_PID) started without the readiness wait" >&2
        echo "$APP_PID"
        exit 3
    fi
    READY_FILE=$READY_DIR/painted
    QDISTRO_ADMIN_APP_READY_FILE=$READY_FILE \
        setsid /usr/bin/python3 /usr/local/bin/qdistro-admin-approval-app "$@" \
        </dev/null >>"$LOG" 2>&1 &
    APP_PID=$!
    READY_TIMEOUT=${QDISTRO_ADMIN_APP_READY_TIMEOUT:-60}
    case "$READY_TIMEOUT" in ''|*[!0-9]*) READY_TIMEOUT=60 ;; esac
    deadline=$((SECONDS + READY_TIMEOUT))
    ready_rc=0
    until [ -e "$READY_FILE" ]; do
        if ! kill -0 "$APP_PID" 2>/dev/null; then
            echo "qdistro admin approvals: app (pid $APP_PID) exited before painting its window" >&2
            ready_rc=3
            break
        fi
        if [ "$SECONDS" -ge "$deadline" ]; then
            echo "qdistro admin approvals: app (pid $APP_PID) painted no window within ${READY_TIMEOUT}s" >&2
            ready_rc=3
            break
        fi
        sleep 0.1
    done
    rm -rf -- "$READY_DIR"
    echo "$APP_PID"
    exit "$ready_rc"
fi

exec /usr/bin/python3 /usr/local/bin/qdistro-admin-approval-app "$@"
