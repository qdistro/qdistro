#!/bin/bash
# V2 host-side gate for qdwin_shell_v1@v19 register_hotkey/unregister_hotkey.
#
# Spawns headless weston with qdwin-shell.so loaded as the shell,
# QDWIN_ALLOWED_UID set to the calling user so the test client can
# bind qdwin_shell_v1, and runs test_zwlr_layer_shell.py with
# QDWIN_WESTON_LOG pointing at the weston log so
# test_v19_register_hotkey_live can scrape it.
#
# Real key-press → hotkey_pressed event delivery is not exercised
# (headless weston has no input backend; see tests/host/
# AGENTS.md). This driver locks the request surface, the binding
# allocation, and the teardown path. Anything stronger needs a
# weston-test or ext-virtual-pointer-v1 plumbing.

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
QDWIN_INSTALL="${QDWIN_INSTALL:-/tmp/qdwin-host-install}"
QDWIN_SO="$QDWIN_INSTALL/lib/weston/qdwin-shell.so"
TEST_PY="$HERE/test_zwlr_layer_shell.py"

if [[ ! -f "$QDWIN_SO" ]]; then
    echo "[v2] qdwin-shell.so missing at $QDWIN_SO" >&2
    echo "[v2] run tests/host/lib.sh ht_require_build first" >&2
    exit 2
fi

RT="/tmp/v2-hotkey"
SOCK="wayland-v2-hotkey"
rm -rf "$RT" && mkdir -p "$RT" && chmod 0700 "$RT"
LOG="$RT/weston.log"

env "XDG_RUNTIME_DIR=$RT" "QDWIN_ALLOWED_UID=$(id -u)" \
    weston \
        --backend=headless --renderer=pixman \
        --shell="$QDWIN_SO" --width=1024 --height=640 \
        --log="$LOG" --socket="$SOCK" \
        > "$RT/out.log" 2>&1 &
WPID=$!

for _ in 1 2 3 4 5 6 7 8 9 10; do
    [[ -e "$RT/$SOCK" ]] && break
    sleep 0.3
done
if [[ ! -e "$RT/$SOCK" ]]; then
    echo "[v2] weston socket never appeared" >&2
    cat "$LOG" >&2 || true
    kill "$WPID" 2>/dev/null
    exit 7
fi

echo "[v2] weston pid=$WPID log=$LOG"

env "XDG_RUNTIME_DIR=$RT" "QDWIN_WESTON_LOG=$LOG" \
    python3 "$TEST_PY" "$SOCK"
RC=$?

kill "$WPID" 2>/dev/null
wait "$WPID" 2>/dev/null

if [[ $RC -eq 0 ]]; then
    echo "[v2] PASS — v19 register_hotkey/unregister_hotkey API live"
else
    echo "[v2] FAIL exit=$RC"
fi
exit $RC
