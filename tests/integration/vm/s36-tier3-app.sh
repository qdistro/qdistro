#!/bin/bash
# In-VM driver for tiered-isolation.bats:phase7-tier3-app.
#
# Exercises spawn-tier3.sh user1 -- weston-terminal end-to-end. Unlike
# s35 (wayland-info, runs-and-exits), this test uses a long-running
# graphical client so we can observe the bridged toplevel from outside
# AND verify the inner process actually runs in the silo uid context.
#
# Load-bearing assertions per todo/qdwin-vm/tier3-spawn-design.md §s36:
#   1. "bridged toplevel reached the outer admin compositor"
#      — proven via the [tier3] toplevel-observed journal line that
#      qdshell's Tier3Apps singleton emits (shared with s38).
#   2. "weston-terminal genuinely runs as uid 1001"
#      — proven via `ps -o uid,user,comm -u user1` inspection after
#      the spawn has had a chance to start the inner process.
#
# Pairs with s35 (smoke) and s37 (multi-silo lifecycle).

set -u

PASSCOUNT=0
FAILCOUNT=0

pass() { echo "PASS: $*"; PASSCOUNT=$((PASSCOUNT + 1)); }
fail() { echo "FAIL: $*"; FAILCOUNT=$((FAILCOUNT + 1)); }
skip() { echo "SKIP: $*"; exit 0; }

# --- 1. stage tier3 source -------------------------------------------
SRC=/root/qdistro-src/qdistro
TIER3_DIR=/tmp/qdistro-tier3-src
if [ -d "$SRC/tier3" ]; then
    rm -rf "$TIER3_DIR" 2>/dev/null || true
    cp -r "$SRC/tier3" "$TIER3_DIR"
    chmod -R a+rX "$TIER3_DIR"
    find "$TIER3_DIR" -name '*.sh' -exec chmod a+rx {} +
fi
[ -d "$TIER3_DIR" ] || skip "tier3 source not unpacked at $TIER3_DIR"

command -v waypipe        >/dev/null 2>&1 || skip "waypipe not installed in this VM"
command -v weston-terminal >/dev/null 2>&1 || skip "weston-terminal not installed in this VM"
command -v runuser        >/dev/null 2>&1 || skip "runuser not available"

# --- 2. tier3 install ran (group + user1) ----------------------------
if ! getent group qdistro-tier3 >/dev/null; then
    skip "qdistro-tier3 group missing — install-tier3-for-vm.sh did not run"
fi
if ! id -u user1 >/dev/null 2>&1; then
    skip "silo user 'user1' missing — install-tier3-for-vm.sh did not create silos"
fi
SILO_UID=$(id -u user1)

# --- 3. outer admin compositor ---------------------------------------
ADMIN_UID=1000
RUNTIME_DIR="/run/user/$ADMIN_UID"
OUTER_SOCK="$RUNTIME_DIR/wayland-1"
if ! runuser -u admin -- test -S "$OUTER_SOCK"; then
    skip "outer admin compositor not up (wayland-1 socket missing)"
fi
pass "outer admin compositor up"

# --- 4. journal cursor + spawn ---------------------------------------
JCURSOR_FILE=$(mktemp)
journalctl --since=now -n0 --show-cursor >"$JCURSOR_FILE" 2>/dev/null || true
CURSOR=$(awk -F': ' '/-- cursor:/ {print $2}' "$JCURSOR_FILE")

SPAWN_LOG=/tmp/s36-spawn.log
: >"$SPAWN_LOG"

TIER3_NO_REAP=1 \
bash "$TIER3_DIR/spawn-tier3.sh" user1 -- weston-terminal >"$SPAWN_LOG" 2>&1 &
SPAWN_PID=$!

# --- 5. wait for the bridge to come up + the toplevel to be observed -
# Two signals to watch in parallel:
#   a) spawn-tier3.sh emits "bridge socket ready at ... (qdistro-tier3:0660)"
#   b) Tier3Apps in qdshell logs "[tier3] toplevel observed silo=user1"
# (b) requires the actual inner toplevel to have arrived through the
# bridge — i.e. weston-terminal started + opened its xdg_toplevel +
# waypipe forwarded it. (a) is just bridge-setup.
BRIDGE_READY=0
for _ in $(seq 1 40); do
    if grep -q "bridge socket ready at .* (qdistro-tier3:0660)" "$SPAWN_LOG" 2>/dev/null; then
        BRIDGE_READY=1; break
    fi
    if ! kill -0 "$SPAWN_PID" 2>/dev/null; then break; fi
    sleep 0.25
done
if [ "$BRIDGE_READY" = "0" ]; then
    cat "$SPAWN_LOG" >&2 || true
    kill -TERM "$SPAWN_PID" 2>/dev/null || true
    wait "$SPAWN_PID" 2>/dev/null || true
    fail "spawn-tier3.sh did not signal bridge-socket-ready within 10s"
    echo "[s36] $PASSCOUNT passes, $FAILCOUNT failures"
    exit 1
fi

jgrep_once() {
    local pat="$1"
    if [ -n "$CURSOR" ]; then
        journalctl --after-cursor="$CURSOR" 2>/dev/null | grep -m1 -E "$pat" || true
    else
        journalctl --since="-1min" 2>/dev/null | grep -m1 -E "$pat" || true
    fi
}

# Weston-terminal cold-start budget: ~5s on a warm VM, occasionally
# more under load. 30s ceiling matches the s32 pattern.
OBS=""
deadline=$(( $(date +%s) + 30 ))
while [ "$(date +%s)" -lt "$deadline" ]; do
    OBS=$(jgrep_once '\[tier3\] toplevel observed silo=user1 secctx=qdistro\.tier3\.user1 handle=[0-9]+')
    [ -n "$OBS" ] && break
    sleep 0.5
done

if [ -n "$OBS" ]; then
    pass "bridged toplevel reached the outer admin compositor"
else
    fail "no [tier3] observation for user1 toplevel within 30s (qdshell Tier3Apps may not be loaded, or waypipe bridge stalled)"
fi

# --- 6. inner process runs as silo uid -------------------------------
# weston-terminal forks several processes (the launcher, the actual
# client). At least one of them must be owned by the silo uid. Use
# `pgrep -u user1 weston-terminal` for a clean check.
INNER_PIDS=$(pgrep -u "$SILO_UID" weston-terminal 2>/dev/null || true)
if [ -n "$INNER_PIDS" ]; then
    # Pull out the first pid + report its real uid for proof.
    INNER_PID=$(echo "$INNER_PIDS" | head -1)
    REAL_UID=$(stat -c %u "/proc/$INNER_PID" 2>/dev/null || echo "?")
    if [ "$REAL_UID" = "$SILO_UID" ]; then
        pass "weston-terminal genuinely runs as uid $REAL_UID"
    else
        fail "weston-terminal pid $INNER_PID owned by uid $REAL_UID, not silo uid $SILO_UID"
    fi
else
    # Fall back: maybe weston-terminal got renamed by waypipe / arg-vec
    # tricks. Check anything that's a child of waypipe-server under
    # user1.
    ANY=$(pgrep -u "$SILO_UID" -af . 2>/dev/null | grep -v "^[0-9]\+ waypipe " | head -3 || true)
    if [ -n "$ANY" ]; then
        echo "$ANY" >&2
    fi
    fail "no weston-terminal process owned by uid $SILO_UID"
fi

# --- 7. tear the spawn down cleanly ----------------------------------
kill -TERM "$SPAWN_PID" 2>/dev/null || true
# Give the cleanup trap up to 5s to remove the bridge socket.
for _ in $(seq 1 20); do
    if ! kill -0 "$SPAWN_PID" 2>/dev/null; then break; fi
    sleep 0.25
done
if kill -0 "$SPAWN_PID" 2>/dev/null; then
    kill -KILL "$SPAWN_PID" 2>/dev/null || true
fi
wait "$SPAWN_PID" 2>/dev/null || true

rm -f "$SPAWN_LOG" "$JCURSOR_FILE" 2>/dev/null || true

if [ "$FAILCOUNT" -eq 0 ]; then
    pass "§Phase-7 tier-3 cross-uid graphical app end-to-end"
    echo "[s36] $PASSCOUNT passes, 0 failures"
    exit 0
else
    echo "[s36] $PASSCOUNT passes, $FAILCOUNT failures"
    exit 1
fi
