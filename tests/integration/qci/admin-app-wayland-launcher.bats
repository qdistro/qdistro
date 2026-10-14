#!/usr/bin/env bats
#
# Host-only regressions for the launch log and first-paint marker of the
# SHIPPED admin-app launcher, deploy/start-admin-app-wayland.sh, in the
# QDISTRO_ADMIN_APP_WAIT_PAINTED=1 mode the GUI scenarios start it with
# (qdwin_start_admin_app).
#
# Ported from the retired launcher-log-path.bats, which pinned the same
# contract on the labwc lane's xcb launcher and qterminal TUI launcher:
#   - the log is PRIVATE (a fresh 0600 file in the user's own 0700 runtime
#     directory) and cannot be redirected through a planted symlink;
#   - the app still starts when no log can be made (logging must never keep
#     the launcher from starting);
#   - the first-paint marker lives in a FRESH private directory the launcher
#     removes afterwards, is never a path inherited from the caller, and when
#     no such directory can be made the app still starts but the launcher
#     reports (exit 3) that it proved nothing.
# The XDG-state-dir selection and getent-home tests retired with that
# launcher: this one never logs outside $XDG_RUNTIME_DIR.
#
# No VM: the launcher runs as-is against a stand-in app (its absolute app path
# rewritten), a fake `id` reporting uid 1000, and a real bound unix socket as
# the Wayland display.

bats_require_minimum_version 1.5.0

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    FAKEBIN="$BATS_TEST_TMPDIR/bin"
    RUNTIME="$BATS_TEST_TMPDIR/run"
    mkdir -p "$FAKEBIN"
    mkdir -m 0700 "$RUNTIME"
    printf '#!/bin/bash\n[ "$1" = -u ] && { echo 1000; exit 0; }\nexec /usr/bin/id "$@"\n' > "$FAKEBIN/id"
    chmod +x "$FAKEBIN/id"
    python3 -c 'import socket, sys; socket.socket(socket.AF_UNIX).bind(sys.argv[1])' \
        "$RUNTIME/wayland-test"
    # The stand-in app: logs a marker and the marker path it was handed, then
    # reports its first paint the way _FirstPaintMarker does.
    APP="$BATS_TEST_TMPDIR/stand-in-app.py"
    cat > "$APP" <<'PYEOF'
import os
print("LAUNCHED-MARKER", flush=True)
ready = os.environ.get("QDISTRO_ADMIN_APP_READY_FILE")
print("READY-FILE=" + (ready if ready is not None else "<unset>"), flush=True)
if ready:
    open(ready, "x").write("painted\n")
PYEOF
    LAUNCHER="$BATS_TEST_TMPDIR/start-admin-app-wayland.sh"
    sed "s#/usr/local/bin/qdistro-admin-approval-app#$APP#g" \
        "$REPO_ROOT/deploy/start-admin-app-wayland.sh" > "$LAUNCHER"
    [ "$(grep -c "$APP" "$LAUNCHER")" -eq 3 ]
}

run_launcher() {
    run --separate-stderr env PATH="$FAKEBIN:$PATH" XDG_RUNTIME_DIR="$RUNTIME" \
        WAYLAND_DISPLAY=wayland-test QDISTRO_ADMIN_APP_WAIT_PAINTED=1 \
        QDISTRO_ADMIN_APP_READY_TIMEOUT=10 "$@" bash "$LAUNCHER"
}

wait_for_marker() {
    local file=$1 i
    for i in $(seq 1 50); do
        grep -q LAUNCHED-MARKER "$file" 2>/dev/null && return 0
        sleep 0.1
    done
    echo "no marker in $file" >&2
    cat "$file" >&2 || true
    return 1
}

logged_path() {
    printf '%s\n' "$stderr" | sed -n 's/^qdistro admin approvals: app log //p' | tail -n1
}

@test "the app log is a fresh private file in the runtime dir; stdout is only the pid" {
    run_launcher
    [ "$status" -eq 0 ]
    [[ "$output" =~ ^[0-9]+$ ]]
    local log
    log=$(logged_path)
    [[ "$log" == "$RUNTIME"/qdistro-admin-app.*.log ]]
    [ -f "$log" ] && [ ! -L "$log" ]
    [ "$(stat -c '%u %a' "$log")" = "$(id -u) 600" ]
    wait_for_marker "$log"
    # Each launch gets its own name: a second launch never reuses the first.
    run_launcher
    [ "$status" -eq 0 ]
    [ "$(logged_path)" != "$log" ]
}

@test "a planted log symlink cannot redirect the log" {
    echo PRECIOUS > "$BATS_TEST_TMPDIR/victim"
    ln -s "$BATS_TEST_TMPDIR/victim" "$RUNTIME/qdistro-admin-app.log"
    ln -s "$BATS_TEST_TMPDIR/victim" "$RUNTIME/qdistro-admin-app.XXXXXXXX.log"
    run_launcher
    [ "$status" -eq 0 ]
    local log
    log=$(logged_path)
    [ ! -L "$log" ]
    wait_for_marker "$log"
    [ "$(cat "$BATS_TEST_TMPDIR/victim")" = PRECIOUS ]
}

@test "the launcher still starts the app when no log file can be made" {
    # Only the log's mktemp fails; the marker directory (mktemp -d) is made.
    printf '#!/bin/bash\n[ "$1" = -d ] && exec /usr/bin/mktemp "$@"\nexit 1\n' > "$FAKEBIN/mktemp"
    chmod +x "$FAKEBIN/mktemp"
    run_launcher
    [ "$status" -eq 0 ]
    [[ "$output" =~ ^[0-9]+$ ]]
    [[ "$stderr" == *"app log /dev/null"* ]]
}

@test "the app gets a fresh private marker path that the launcher removes" {
    run_launcher QDISTRO_ADMIN_APP_READY_FILE="$BATS_TEST_TMPDIR/inherited-painted"
    [ "$status" -eq 0 ]
    local log ready
    log=$(logged_path)
    wait_for_marker "$log"
    ready=$(sed -n 's/^READY-FILE=//p' "$log")
    [[ "$ready" == "$RUNTIME"/qdistro-admin-app-ready.*/painted ]]
    [ ! -e "$(dirname "$ready")" ]
    [ ! -e "$BATS_TEST_TMPDIR/inherited-painted" ]
}

@test "no directory for the first-paint marker: app still started, launcher exits 3" {
    # Every mktemp fails: neither a log nor a private marker directory. The
    # app must still start, never with a marker path built from an empty
    # directory or inherited from the caller, and the launcher must say it
    # could not prove the frame.
    printf '#!/bin/bash\nexit 1\n' > "$FAKEBIN/mktemp"
    chmod +x "$FAKEBIN/mktemp"
    local probe="$BATS_TEST_TMPDIR/app-env"
    sed -i "1a open('$probe', 'w').write(repr(os.environ.get('QDISTRO_ADMIN_APP_READY_FILE')))" "$APP"
    sed -i '1a import os' "$APP"
    run_launcher QDISTRO_ADMIN_APP_READY_FILE="$BATS_TEST_TMPDIR/inherited-painted"
    [ "$status" -eq 3 ]
    [[ "$output" =~ ^[0-9]+$ ]]
    [[ "$stderr" == *"no private directory for the first-paint marker"* ]]
    local i
    for i in $(seq 1 50); do [ -s "$probe" ] && break; sleep 0.1; done
    [ "$(cat "$probe")" = None ]
    [ ! -e "$BATS_TEST_TMPDIR/inherited-painted" ]
}

@test "the launcher refuses a non-admin uid and a missing Wayland display" {
    printf '#!/bin/bash\n[ "$1" = -u ] && { echo 4242; exit 0; }\nexec /usr/bin/id "$@"\n' > "$FAKEBIN/id"
    run_launcher
    [ "$status" -eq 1 ]
    [[ "$stderr" == *"launch from the admin (uid 1000) session"* ]]
    printf '#!/bin/bash\n[ "$1" = -u ] && { echo 1000; exit 0; }\nexec /usr/bin/id "$@"\n' > "$FAKEBIN/id"
    run_launcher WAYLAND_DISPLAY=wayland-none
    [ "$status" -eq 1 ]
    [[ "$stderr" == *"no Wayland display at $RUNTIME/wayland-none"* ]]
}
