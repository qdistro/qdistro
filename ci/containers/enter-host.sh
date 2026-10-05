#!/usr/bin/env bash
# Give tests a private bus without exposing either host bus socket.
set -euo pipefail
export LANG=C.UTF-8
export XDG_RUNTIME_DIR="$HOME/runtime"
mkdir -p "$HOME"
mkdir -p "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"
# The inner shell expands its private bus address and command arguments.
# shellcheck disable=SC2016
exec dbus-run-session -- bash -c '
    export DBUS_SYSTEM_BUS_ADDRESS="$DBUS_SESSION_BUS_ADDRESS"
    exec "$@"
' bash "$@"
