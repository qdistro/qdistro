#!/bin/sh
# qdistro-start-user-app — launch a Qt XWayland app as another uid
# on admin's compositor display.
#
# Usage:
#   qdistro-start-user-app <user> <command> [args...]
#
# Ensures xhost SI allowlist is active for <user> on DISPLAY=:0, then
# runs the target command with DISPLAY/XDG_RUNTIME_DIR/
# DBUS_SESSION_BUS_ADDRESS set so a fresh Qt XWayland client as that uid can:
#   1. reach the X server admin's XWayland is already running,
#   2. find its own session bus at /run/user/<uid>/bus (needs linger),
#   3. spawn detached (setsid + /dev/null redirection) so vm-exec
#      returns immediately without killing the GUI process.
#
# Caveat (loud): shared XWayland means any uid in the allowlist can
# keylog any other via XTest. This is a Phase-4 expedient, not the
# production model. See .

set -eu

if [ "$#" -lt 2 ]; then
    echo "usage: $0 <user> <command> [args...]" >&2
    exit 2
fi

USER_NAME="$1"
shift
UID_N=$(id -u "$USER_NAME") || {
    echo "[qdistro-start-user-app] no such user: $USER_NAME" >&2
    exit 1
}

XDG_DIR="/run/user/${UID_N}"
if [ ! -d "$XDG_DIR" ]; then
    echo "[qdistro-start-user-app] $XDG_DIR missing — enable-linger for $USER_NAME?" >&2
    exit 1
fi

if ! command -v xhost >/dev/null 2>&1; then
    echo "[qdistro-start-user-app] xhost missing; install xhost package" >&2
    exit 1
fi

runuser -u admin -- env DISPLAY=:0 xhost "+SI:localuser:${USER_NAME}" >/dev/null

# setsid + </dev/null + detach: the GUI process outlives the vm-exec
# guest-agent call. Same pattern the Phase-3 scenario launchers use.
LOG=/tmp/qdistro-user-app.${USER_NAME}.log
setsid runuser -u "$USER_NAME" -- env \
    DISPLAY=:0 \
    XDG_RUNTIME_DIR="$XDG_DIR" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=${XDG_DIR}/bus" \
    QT_QPA_PLATFORM=xcb \
    QT_LOGGING_RULES='qt.qpa.xcb.*=false' \
    "$@" </dev/null >"$LOG" 2>&1 &
echo "[qdistro-start-user-app] launched PID=$! (log: $LOG)"
