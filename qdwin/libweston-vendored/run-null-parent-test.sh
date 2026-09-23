#!/bin/bash
# V1 host-side gate for the vendored libweston-16 NULL-parent xdg_popup
# patch.
#
# Spawns a fresh headless weston with qdwin-shell.so loaded, runs
# qdwin/test_zwlr_layer_shell.py against it, and asserts
# the test_null_parent_popup case passes. Loops twice — first against
# stock libweston (expect xdg_wm_base#3 fired), second against the
# vendored .so via LD_LIBRARY_PATH (expect no error). The full
# zwlr_layer_shell_v1 test set runs both times.
#
# QDWIN_USE_VENDORED_LIBWESTON=1 is propagated to both weston (so it
# picks up the patched .so) and the test client (so it inverts its
# expected outcome). Single env knob, two consumers.
#
# Exit 0 only if BOTH passes are clean.

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
VEND_PREFIX="/tmp/qdwin-libweston-prefix"
VEND_LIB="$VEND_PREFIX/lib64"
QDWIN_INSTALL="${QDWIN_INSTALL:-/tmp/qdwin-host-install}"
QDWIN_SO="$QDWIN_INSTALL/lib/weston/qdwin-shell.so"
TEST_PY="$REPO/qdwin/test_zwlr_layer_shell.py"

if [[ ! -f "$QDWIN_SO" ]]; then
    echo "[v1] qdwin-shell.so missing at $QDWIN_SO" >&2
    echo "[v1] run tests/host/lib.sh ht_require_build first" >&2
    exit 2
fi
if [[ ! -f "$VEND_LIB/libweston-16.so.0.0.0" || ! -f "$VEND_LIB/libweston-16/headless-backend.so" ]]; then
    echo "[v1] vendored libweston not installed at $VEND_PREFIX" >&2
    echo "[v1] run $HERE/build-libweston.sh first" >&2
    exit 2
fi

run_one() {
    local mode="$1"  # "stock" or "vendored"
    local rt="/tmp/v1-${mode}"
    local sock="wayland-v1-${mode}"
    rm -rf "$rt" && mkdir -p "$rt" && chmod 0700 "$rt"

    local env_pfx=()
    env_pfx+=(env "XDG_RUNTIME_DIR=$rt")
    if [[ "$mode" == "vendored" ]]; then
        env_pfx+=("LD_LIBRARY_PATH=$VEND_LIB${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}")
        env_pfx+=("QDWIN_USE_VENDORED_LIBWESTON=1")
    fi

    "${env_pfx[@]}" weston \
        --backend=headless --renderer=pixman \
        --shell="$QDWIN_SO" --width=1024 --height=640 \
        --log="$rt/weston.log" --socket="$sock" \
        > "$rt/out.log" 2>&1 &
    local wpid=$!

    local i
    for i in 1 2 3 4 5 6 7 8 9 10; do
        [[ -e "$rt/$sock" ]] && break
        sleep 0.3
    done
    if [[ ! -e "$rt/$sock" ]]; then
        echo "[v1/$mode] weston socket never appeared" >&2
        cat "$rt/weston.log" >&2 || true
        kill "$wpid" 2>/dev/null
        return 7
    fi

    # Confirm which libweston is loaded.
    local actual_lib
    actual_lib=$(pmap "$wpid" 2>/dev/null | grep -oE '[^ ]*libweston-16\.so[^ ]*' | head -1)
    echo "[v1/$mode] weston pid=$wpid libweston=$actual_lib"

    "${env_pfx[@]}" python3 "$TEST_PY" "$sock"
    local rc=$?

    kill "$wpid" 2>/dev/null
    wait "$wpid" 2>/dev/null
    return $rc
}

set +e
run_one stock
RC_STOCK=$?
echo "--- stock pass exited $RC_STOCK ---"
run_one vendored
RC_VEND=$?
echo "--- vendored pass exited $RC_VEND ---"
set -e

if [[ $RC_STOCK -ne 0 || $RC_VEND -ne 0 ]]; then
    echo "[v1] FAIL stock=$RC_STOCK vendored=$RC_VEND"
    exit 1
fi
echo "[v1] PASS — stock rejects, vendored accepts get_popup(NULL)"
