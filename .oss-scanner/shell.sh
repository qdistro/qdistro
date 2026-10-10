#!/usr/bin/env bash
# Private bus/headless Qt and an ordinary UID for user-facing tests.
set -euo pipefail
cd "$(dirname "$0")/.."
export QT_QPA_PLATFORM=offscreen PYTEST_QT_API=pyqt6 QT_API=pyqt6 QDISTRO_REQUIRE_PYQT6=1
if [ "$(id -u)" = 0 ]; then
    exec setpriv --reuid=scanner --regid=scanner --init-groups \
        env HOME=/home/scanner bash "$0" "$@"
fi
export XDG_RUNTIME_DIR="$HOME/scanner-runtime"
mkdir -p "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"
if [ "$#" = 0 ]; then set -- bash; fi
# The child shell expands its own private bus and arguments.
# shellcheck disable=SC2016
exec dbus-run-session -- bash -c '
    export DBUS_SYSTEM_BUS_ADDRESS="$DBUS_SESSION_BUS_ADDRESS"
    exec "$@"
' bash "$@"
