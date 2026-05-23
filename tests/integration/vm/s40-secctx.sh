#!/bin/bash
# In-VM driver for tiered-isolation.bats:phase7-secctx.
#
# Validates the wp_security_context_v1 wire path end-to-end:
# qdwin advertises the manager global → waypipe binds it → silo
# client (via qdistro-secctx-exec wrap inside spawn-tier3.sh) commits
# a secctx with engine + app_id + instance_id → qdwin's commit handler
# logs all three fields.
#
# Load-bearing assertions per design doc / dead-bats-entries.md §s40:
#   1. wp_security_context_manager_v1 enumerable on the outer
#      compositor (wayland-info global listing). Confirms qdwin's
#      wl_global_create succeeded + the wl_display_set_global_filter
#      doesn't hide it from unsandboxed observers.
#   2. waypipe must have bound it — proven *indirectly* by (3): if
#      a secctx commit reaches qdwin's handler, waypipe necessarily
#      bound the manager global to plant it. (Direct observation
#      would need waypipe --debug log scraping; brittle. The
#      derivation is canonical.)
#   3. qdwin's commit handler logs `qdwin/secctx: committed engine=
#      qdistro.tier3 app_id=qdistro.tier3.user1 instance_id=...` —
#      exact log line at qdwin/qdwin.c:11740.
#   4. The app_id matches spawn-tier3.sh's TIER3_SECCTX_APPID default
#      (qdistro.tier3.<silo>) — proves the wrapper's secctx triple
#      propagated through the wire intact.
#
# Pairs with s41 (toplevel_security_context event — the *post*-commit
# fan-out from qdwin to qdshell).

set -u

PASSCOUNT=0
FAILCOUNT=0

pass() { echo "PASS: $*"; PASSCOUNT=$((PASSCOUNT + 1)); }
fail() { echo "FAIL: $*"; FAILCOUNT=$((FAILCOUNT + 1)); }
skip() { echo "SKIP: $*"; exit 0; }

SPAWN_PID=""
TRAP_FIRED=0
cleanup() {
    [ "$TRAP_FIRED" -eq 1 ] && return 0
    TRAP_FIRED=1
    [ -n "$SPAWN_PID" ] && kill -TERM "$SPAWN_PID" 2>/dev/null || true
    [ -n "$SPAWN_PID" ] && wait    "$SPAWN_PID" 2>/dev/null || true
    rm -f /tmp/s40-spawn.log 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# --- 1. stage tier3 source -------------------------------------------
SRC=/root/qdistro-src/qdistro
TIER3_DIR=/tmp/qdistro-tier3-src
COMMON_LIB_DIR=/tmp/lib
if [ -d "$SRC/tier3" ]; then
    rm -rf "$TIER3_DIR" 2>/dev/null || true
    cp -r "$SRC/tier3" "$TIER3_DIR"
    chmod -R a+rX "$TIER3_DIR"
    find "$TIER3_DIR" -name '*.sh' -exec chmod a+rx {} +
fi
if [ -d "$SRC/lib" ]; then
    rm -rf "$COMMON_LIB_DIR" 2>/dev/null || true
    cp -r "$SRC/lib" "$COMMON_LIB_DIR"
    chmod -R a+rX "$COMMON_LIB_DIR"
fi
[ -d "$TIER3_DIR" ] || skip "tier3 source not unpacked at $TIER3_DIR"
[ -f "$COMMON_LIB_DIR/spawn-common.sh" ] || skip "spawn-common library not unpacked at $COMMON_LIB_DIR"

command -v waypipe       >/dev/null 2>&1 || skip "waypipe not installed in this VM"
command -v wayland-info  >/dev/null 2>&1 || skip "wayland-info (wayland-utils) not installed in this VM"
command -v runuser       >/dev/null 2>&1 || skip "runuser not available"

# --- 2. tier3 install ran --------------------------------------------
if ! getent group qdistro-tier3 >/dev/null; then
    skip "qdistro-tier3 group missing — install-tier3-for-vm.sh did not run"
fi
id -u user1 >/dev/null 2>&1 || skip "silo user 'user1' missing"

# --- 3. outer admin compositor ---------------------------------------
ADMIN_UID=1000
RUNTIME_DIR="/run/user/$ADMIN_UID"
OUTER_SOCK="$RUNTIME_DIR/wayland-1"
runuser -u admin -- test -S "$OUTER_SOCK" || skip "outer admin compositor not up"
pass "outer admin compositor up"

# --- 4. wp_security_context_manager_v1 advertised -------------------
# wayland-info enumerates globals on the outer compositor. The
# manager_v1 line should appear in the output. Run as admin (the
# socket is admin-owned).
WL_INFO=$(runuser -u admin -- env WAYLAND_DISPLAY=wayland-1 \
    XDG_RUNTIME_DIR="$RUNTIME_DIR" wayland-info 2>&1 || true)
if echo "$WL_INFO" | grep -q "wp_security_context_manager_v1"; then
    pass "wp_security_context_manager_v1 advertised by qdwin"
else
    echo "$WL_INFO" | head -40 >&2
    fail "wp_security_context_manager_v1 not in outer wayland globals"
fi

# --- 5. capture journal cursor --------------------------------------
JCURSOR_FILE=$(mktemp)
journalctl --since=now -n0 --show-cursor >"$JCURSOR_FILE" 2>/dev/null || true
CURSOR=$(awk -F': ' '/-- cursor:/ {print $2}' "$JCURSOR_FILE")

# --- 6. spawn a tier-3 client to trigger the secctx commit ----------
SPAWN_LOG=/tmp/s40-spawn.log
: >"$SPAWN_LOG"

TIER3_NO_REAP=1 \
bash "$TIER3_DIR/spawn-tier3.sh" user1 -- wayland-info >"$SPAWN_LOG" 2>&1 &
SPAWN_PID=$!

# Wait for spawn-tier3 to either finish (wayland-info exits in <2s
# once bridged) or for ~15s of total budget (covers waypipe startup +
# bridge + inner enum).
for _ in $(seq 1 60); do
    if ! kill -0 "$SPAWN_PID" 2>/dev/null; then break; fi
    sleep 0.25
done
if kill -0 "$SPAWN_PID" 2>/dev/null; then
    kill -TERM "$SPAWN_PID" 2>/dev/null || true
fi
wait "$SPAWN_PID" 2>/dev/null || true

# Drain qdwin's log buffer.
sleep 1

# --- 7. qdwin secctx commit log -------------------------------------
# Exact format (qdwin.c:11740):
#   qdwin/secctx: committed engine=qdistro.tier3 app_id=qdistro.tier3.user1 instance_id=<32hex> listen_fd=N close_fd=N
COMMIT_LINE=""
if [ -n "$CURSOR" ]; then
    COMMIT_LINE=$(journalctl --after-cursor="$CURSOR" 2>/dev/null \
        | grep -m1 "qdwin/secctx: committed.*app_id=qdistro\.tier3\.user1" || true)
else
    COMMIT_LINE=$(journalctl --since="-2min" 2>/dev/null \
        | grep -m1 "qdwin/secctx: committed.*app_id=qdistro\.tier3\.user1" || true)
fi

if [ -n "$COMMIT_LINE" ]; then
    # If the commit line landed at qdwin, then waypipe by definition
    # bound the manager global to plant it (the bind is what sets up
    # the per-client secctx state machine). Derived assertion.
    pass "waypipe bound wp_security_context_manager_v1"
    pass "silo client tagged with secctx"

    # Verify the engine field is the wrapper's default.
    if echo "$COMMIT_LINE" | grep -q "engine=qdistro\.tier3 "; then
        pass "secctx app_id propagated via wrapper default"
    else
        echo "$COMMIT_LINE" >&2
        fail "engine field not 'qdistro.tier3' in commit line"
    fi
else
    cat "$SPAWN_LOG" >&2 || true
    fail "no qdwin/secctx: committed line for app_id=qdistro.tier3.user1 within 15s"
fi

rm -f "$SPAWN_LOG" "$JCURSOR_FILE" 2>/dev/null || true

if [ "$FAILCOUNT" -eq 0 ]; then
    pass "§6.10 wp_security_context_v1 end-to-end"
    echo "[s40] $PASSCOUNT passes, 0 failures"
    exit 0
else
    echo "[s40] $PASSCOUNT passes, $FAILCOUNT failures"
    exit 1
fi
