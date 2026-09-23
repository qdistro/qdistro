#!/bin/bash
# Start a fresh headless qdwin + qdshell pair for a scenario.
#
# Usage:
#   start.sh <test-id> [--colors <map>] [--width N] [--height N]
#                       [--no-shell] [--no-terminal] [--pipewire]
#
# --pipewire additionally loads libweston's PipeWire BACKEND and configures one
# pipewire output, so subscribe_view_stream can actually be approved. Without
# it qdwin answers `denied "no free pipewire output"` and every stream-shaped
# assertion is vacuous. Requires a running PipeWire daemon on the host; the
# backend is pointed at the REAL user runtime dir for its socket while weston
# itself keeps the per-test one.
#
# After return:
#   - weston is running headless on socket "qdwin-test-<id>"
#   - qdwin-bystander is running as the shell helper, with its command FIFO
#     at $(ht_ctrl)
#   - one weston-terminal is running as a client (unless --no-terminal)
#
# Stdout is the test-id (so callers can `ID=$(start.sh foo)`).
# Per-test state lives under /tmp/qdwin-host-tests/<id>/.

set -eo pipefail
. "$(dirname "$0")/lib.sh"

TEST_ID=${1:?usage: start.sh <test-id> [opts]}
shift
COLORS="$(id -u)=#22aaff"
WIDTH=1024
HEIGHT=640
WANT_SHELL=1
WANT_TERMINAL=1
WANT_PIPEWIRE=0
ALLOWED_UID=$(id -u)
while [ $# -gt 0 ]; do
    case "$1" in
        --colors)      COLORS=$2; shift 2 ;;
        --width)       WIDTH=$2; shift 2 ;;
        --height)      HEIGHT=$2; shift 2 ;;
        --no-shell)    WANT_SHELL=0; shift ;;
        --no-terminal) WANT_TERMINAL=0; shift ;;
        --pipewire)    WANT_PIPEWIRE=1; shift ;;
        # Override qdwin's allowed_uid. Shell bootstrap, locker, and
        # layer-shell bind gates key on it; secctx manager binds do not
        # authorize by uid except when QDWIN_SECCTX_OPEN=1 is set.
        # Default: this uid.
        --allowed-uid) ALLOWED_UID=$2; shift 2 ;;
        *) echo "[start.sh] unknown opt $1" >&2; exit 2 ;;
    esac
done

ht_require_build

DIR=$(ht_dir "$TEST_ID")
RUNTIME=$(ht_runtime "$TEST_ID")
WLOG=$(ht_log_weston "$TEST_ID")
SLOG=$(ht_log_qdshell "$TEST_ID")
CLOG=$(ht_log_clients "$TEST_ID")
CTRL=$(ht_ctrl "$TEST_ID")
SOCK=$(ht_socket "$TEST_ID")
SHOTS=$(ht_shots "$TEST_ID")

rm -rf "$DIR"
mkdir -p "$RUNTIME" "$SHOTS"
chmod 0700 "$RUNTIME"
: >"$WLOG"; : >"$SLOG"; : >"$CLOG"

export XDG_RUNTIME_DIR="$RUNTIME"
export QDWIN_ALLOWED_UID=$ALLOWED_UID
# Headless dev/test harness: the locker probe is not the installed
# /usr/bin/qdlocker, so qdwin's production-default mandatory-exe locker
# policy would reject it. Consciously drop to the uid-only locker policy
# here (the exact dev/test opt-out the flag exists for). Tests that need
# to exercise the mandatory-exe default itself set the policy explicitly
# and must NOT inherit this — they pass QDWIN_ALLOWED_LOCKER_ANY= (empty
# but SET) to disable it (see 07-locker-bind-gate S5). Only default when the
# variable is entirely UNSET: the ${VAR+set} test (no colon) treats an
# explicit empty value as "set", so the caller's empty value disables the
# opt-out instead of being re-defaulted to 1.
if [ -z "${QDWIN_ALLOWED_LOCKER_ANY+set}" ]; then
    QDWIN_ALLOWED_LOCKER_ANY=1
fi
export QDWIN_ALLOWED_LOCKER_ANY
# Host GUI scenarios use weston-screenshooter for assertions. qdwin keeps the
# screenshooter interface production-disabled unless this explicit dev/test
# flag is present.
export QDWIN_ENABLE_SCREENSHOOTER=1

# Optional PipeWire backend. The [pipewire] section is only read once that
# backend is loaded, and the backend resolves its socket through
# PIPEWIRE_RUNTIME_DIR — which must stay the user's real runtime dir, since the
# per-test XDG_RUNTIME_DIR has no pipewire-0 in it.
PW_ARGS=()
if [ "$WANT_PIPEWIRE" = 1 ]; then
    if [ ! -S "/run/user/$(id -u)/pipewire-0" ]; then
        echo "[start.sh] --pipewire: no pipewire daemon at /run/user/$(id -u)/pipewire-0" >&2
        exit 8
    fi
    printf '[core]\nbackend=headless\nrequire-input=false\n\n[pipewire]\nnum-outputs=1\n' \
        > "$DIR/weston.ini"
    PW_ARGS=(--backend=pipewire --config="$DIR/weston.ini")
    export PIPEWIRE_RUNTIME_DIR="/run/user/$(id -u)"
fi

# weston headless + qdwin-shell.so
weston \
    --backend=headless \
    "${PW_ARGS[@]}" \
    --renderer=pixman \
    --shell="$QDWIN_INSTALL/lib/weston/qdwin-shell.so" \
    --width="$WIDTH" --height="$HEIGHT" \
    --debug \
    --log="$WLOG" --socket="$SOCK" >/dev/null 2>&1 &
WPID=$!
ht_pid_save "$TEST_ID" weston "$WPID"

# Wait for the wayland socket to appear (covers the bind→listen race).
for _ in $(seq 1 30); do
    [ -S "$RUNTIME/$SOCK" ] && break
    sleep 0.1
done
[ -S "$RUNTIME/$SOCK" ] || { echo "[start.sh] weston socket never appeared" >&2; cat "$WLOG" >&2; exit 7; }

if [ "$WANT_TERMINAL" = 1 ]; then
    WAYLAND_DISPLAY="$SOCK" weston-terminal >>"$CLOG" 2>&1 &
    ht_pid_save "$TEST_ID" terminal $!
    sleep 1.2
fi

if [ "$WANT_SHELL" = 1 ]; then
    QDWIN_BYSTANDER_FIFO="$CTRL" WAYLAND_DISPLAY="$SOCK" "$QDWIN_BYSTANDER" \
        >>"$SLOG" 2>&1 &
    ht_pid_save "$TEST_ID" qdshell $!
    # Wait for the command FIFO to be created.
    for _ in $(seq 1 30); do
        [ -p "$CTRL" ] && break
        sleep 0.1
    done
    [ -p "$CTRL" ] || { echo "[start.sh] command FIFO never appeared" >&2; cat "$SLOG" >&2; exit 8; }
fi

echo "$TEST_ID"
