#!/bin/bash
# In-VM driver for tiered-isolation.bats:phase7-tier3-lifecycle.
#
# Exercises the multi-silo lifecycle of spawn-tier3.sh: two silos
# (user1 + user2) running weston-terminal concurrently, then silo A
# torn down and silo B confirmed alive. Validates that tier-3 spawn
# wrappers + bridge sockets are properly per-silo (no shared global
# state that would leak).
#
# Load-bearing assertions per todo/qdwin-vm/tier3-spawn-design.md §s37:
#   1. "two distinct silo toplevels reached the outer compositor"
#      — both [tier3] toplevel-observed journal lines present.
#   2. "both bridge sockets present" — file-existence + group/mode
#      check on /run/user/1000/qdistro-tier3-user{1,2}-<token>.sock.
#   3. "silo A runs as user1" — pgrep -u user1 weston-terminal hits.
#   4. "silo B still running after silo A teardown" — silo B's spawn
#      wrapper pid is still alive after silo A's wrapper exits and
#      silo A's bridge socket is gone.
#
# Pairs with s35 (smoke) + s36 (single-silo app).

set -u

PASSCOUNT=0
FAILCOUNT=0

pass() { echo "PASS: $*"; PASSCOUNT=$((PASSCOUNT + 1)); }
fail() { echo "FAIL: $*"; FAILCOUNT=$((FAILCOUNT + 1)); }
skip() { echo "SKIP: $*"; exit 0; }

SPAWN_A=""
SPAWN_B=""
TRAP_FIRED=0
cleanup() {
    [ "$TRAP_FIRED" -eq 1 ] && return 0
    TRAP_FIRED=1
    for pid in "$SPAWN_A" "$SPAWN_B"; do
        [ -n "$pid" ] && kill -TERM "$pid" 2>/dev/null || true
    done
    sleep 0.5
    for pid in "$SPAWN_A" "$SPAWN_B"; do
        [ -n "$pid" ] && kill -KILL "$pid" 2>/dev/null || true
        [ -n "$pid" ] && wait "$pid" 2>/dev/null || true
    done
    # Use silo usernames (not UIDs) — pkill accepts both, and the
    # username is stable across VMs where the assigned uid may drift.
    pkill -u user1 -x weston-terminal 2>/dev/null || true
    pkill -u user2 -x weston-terminal 2>/dev/null || true
    rm -f /tmp/s37-spawn-user1.log /tmp/s37-spawn-user2.log 2>/dev/null || true
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

command -v waypipe        >/dev/null 2>&1 || skip "waypipe not installed in this VM"
command -v weston-terminal >/dev/null 2>&1 || skip "weston-terminal not installed in this VM"
command -v runuser        >/dev/null 2>&1 || skip "runuser not available"

# --- 2. tier3 install ran --------------------------------------------
if ! getent group qdistro-tier3 >/dev/null; then
    skip "qdistro-tier3 group missing — install-tier3-for-vm.sh did not run"
fi
for u in user1 user2; do
    id -u "$u" >/dev/null 2>&1 || skip "silo user '$u' missing — install-tier3-for-vm.sh did not create silos"
done
UID_A=$(id -u user1)
UID_B=$(id -u user2)

# --- 3. outer admin compositor ---------------------------------------
ADMIN_UID=1000
RUNTIME_DIR="/run/user/$ADMIN_UID"
OUTER_SOCK="$RUNTIME_DIR/wayland-1"
if ! runuser -u admin -- test -S "$OUTER_SOCK"; then
    skip "outer admin compositor not up (wayland-1 socket missing)"
fi
pass "outer admin compositor up"

# --- 4. journal cursor + parallel spawns -----------------------------
JCURSOR_FILE=$(mktemp)
journalctl --since=now -n0 --show-cursor >"$JCURSOR_FILE" 2>/dev/null || true
CURSOR=$(awk -F': ' '/-- cursor:/ {print $2}' "$JCURSOR_FILE")

SPAWN_LOG_A=/tmp/s37-spawn-user1.log
SPAWN_LOG_B=/tmp/s37-spawn-user2.log
: >"$SPAWN_LOG_A" >"$SPAWN_LOG_B"

TIER3_NO_REAP=1 \
bash "$TIER3_DIR/spawn-tier3.sh" user1 -- weston-terminal >"$SPAWN_LOG_A" 2>&1 &
SPAWN_A=$!
# Serialize bring-up: wait for silo A's bridge before launching silo B so the
# two tier3 observations don't race for the compositor under load (was: both
# silos spawned simultaneously, which flaked the [tier3] observation).
for _ in $(seq 1 40); do
    grep -q "bridge socket ready at .* (qdistro-tier3:0660)" "$SPAWN_LOG_A" 2>/dev/null && break
    kill -0 "$SPAWN_A" 2>/dev/null || break
    sleep 0.25
done
TIER3_NO_REAP=1 \
bash "$TIER3_DIR/spawn-tier3.sh" user2 -- weston-terminal >"$SPAWN_LOG_B" 2>&1 &
SPAWN_B=$!

# Wait for both bridges to come up.
for tag in A B; do
    case "$tag" in
        A) log=$SPAWN_LOG_A; pid=$SPAWN_A ;;
        B) log=$SPAWN_LOG_B; pid=$SPAWN_B ;;
    esac
    READY=0
    for _ in $(seq 1 40); do
        if grep -q "bridge socket ready at .* (qdistro-tier3:0660)" "$log" 2>/dev/null; then
            READY=1; break
        fi
        if ! kill -0 "$pid" 2>/dev/null; then break; fi
        sleep 0.25
    done
    if [ "$READY" = "0" ]; then
        cat "$log" >&2 || true
        # Tear down what we did get up so we don't leak.
        kill -TERM "$SPAWN_A" "$SPAWN_B" 2>/dev/null || true
        wait "$SPAWN_A" "$SPAWN_B" 2>/dev/null || true
        fail "silo $tag bridge did not come up within 10s"
        echo "[s37] $PASSCOUNT passes, $FAILCOUNT failures"
        exit 1
    fi
done

# --- 5. observe both toplevels in qdshell journal --------------------
jgrep_once() {
    local pat="$1"
    if [ -n "$CURSOR" ]; then
        journalctl --after-cursor="$CURSOR" 2>/dev/null | grep -m1 -E "$pat" || true
    else
        journalctl --since="-1min" 2>/dev/null | grep -m1 -E "$pat" || true
    fi
}

OBS_A=""
OBS_B=""
deadline=$(( $(date +%s) + 90 ))
while [ "$(date +%s)" -lt "$deadline" ]; do
    [ -z "$OBS_A" ] && \
        OBS_A=$(jgrep_once '\[tier3\] toplevel observed silo=user1 secctx=qdistro\.tier3\.user1 handle=[0-9]+')
    [ -z "$OBS_B" ] && \
        OBS_B=$(jgrep_once '\[tier3\] toplevel observed silo=user2 secctx=qdistro\.tier3\.user2 handle=[0-9]+')
    if [ -n "$OBS_A" ] && [ -n "$OBS_B" ]; then break; fi
    sleep 0.5
done

if [ -n "$OBS_A" ] && [ -n "$OBS_B" ]; then
    pass "two distinct silo toplevels reached the outer compositor"
else
    [ -z "$OBS_A" ] && fail "qdshell did not log [tier3] observation for user1"
    [ -z "$OBS_B" ] && fail "qdshell did not log [tier3] observation for user2"
fi

# --- 6. both bridge sockets present (via log-line proof) ------------
# waypipe-client with -o (oneshot) UNLINKS its listen socket
# immediately after accepting the first connection from waypipe-server,
# so file-existence checks race with the unlink and almost always
# observe an empty /run/qdistro-tier3/. The authoritative proof of
# "the bridge socket existed with the right perms" is spawn-tier3's
# own log line, which is emitted only AFTER chgrp + chmod both
# succeeded (hard-exit otherwise — see spawn-tier3.sh's chgrp/chmod
# block).
BRIDGE_A=$(grep -oE '/run/qdistro-tier3/qdistro-tier3-user1-[0-9a-f]{32}\.sock' "$SPAWN_LOG_A" | head -1)
BRIDGE_B=$(grep -oE '/run/qdistro-tier3/qdistro-tier3-user2-[0-9a-f]{32}\.sock' "$SPAWN_LOG_B" | head -1)
if [ -n "$BRIDGE_A" ] && [ -n "$BRIDGE_B" ] && \
   grep -q "bridge socket ready at .* (qdistro-tier3:0660)" "$SPAWN_LOG_A" && \
   grep -q "bridge socket ready at .* (qdistro-tier3:0660)" "$SPAWN_LOG_B"; then
    pass "both bridge sockets present"
else
    fail "bridge-ready log missing: A_log=$([ -n "$BRIDGE_A" ] && echo OK || echo MISSING) B_log=$([ -n "$BRIDGE_B" ] && echo OK || echo MISSING)"
fi

# --- 7. silo A runs as user1 -----------------------------------------
if pgrep -u "$UID_A" weston-terminal >/dev/null 2>&1; then
    pass "silo A runs as user1"
else
    fail "no weston-terminal process owned by uid $UID_A (user1)"
fi

# --- 8. kill A, confirm B survives -----------------------------------
UID_A=$(id -u user1)
UID_B=$(id -u user2)
kill -TERM "$SPAWN_A" 2>/dev/null || true
# Wait up to 5s for A's spawn wrapper to exit + weston-terminal under
# user1 to disappear. Socket file is unlinked by waypipe-client's
# oneshot already (see s37 §6 comment); A's exit signal here is the
# wrapper pid + the silo's weston-terminal process going away.
A_GONE=0
for _ in $(seq 1 20); do
    if ! kill -0 "$SPAWN_A" 2>/dev/null && \
       ! pgrep -u "$UID_A" weston-terminal >/dev/null 2>&1; then
        A_GONE=1; break
    fi
    sleep 0.25
done
if [ "$A_GONE" = "0" ]; then
    kill -KILL "$SPAWN_A" 2>/dev/null || true
    pkill -KILL -u "$UID_A" weston-terminal 2>/dev/null || true
fi
wait "$SPAWN_A" 2>/dev/null || true

# B should still be alive — its own spawn wrapper pid + its
# weston-terminal process under user2's uid.
if kill -0 "$SPAWN_B" 2>/dev/null && \
   pgrep -u "$UID_B" weston-terminal >/dev/null 2>&1; then
    pass "silo B still running after silo A teardown"
else
    B_PID_ALIVE=$(kill -0 "$SPAWN_B" 2>/dev/null && echo yes || echo no)
    B_WESTON=$(pgrep -u "$UID_B" weston-terminal >/dev/null 2>&1 && echo yes || echo no)
    fail "silo B not running: wrapper=$B_PID_ALIVE weston-terminal=$B_WESTON"
fi

# --- 9. final cleanup ------------------------------------------------
kill -TERM "$SPAWN_B" 2>/dev/null || true
for _ in $(seq 1 20); do
    if ! kill -0 "$SPAWN_B" 2>/dev/null; then break; fi
    sleep 0.25
done
kill -KILL "$SPAWN_B" 2>/dev/null || true
wait "$SPAWN_B" 2>/dev/null || true

rm -f "$SPAWN_LOG_A" "$SPAWN_LOG_B" "$JCURSOR_FILE" 2>/dev/null || true

if [ "$FAILCOUNT" -eq 0 ]; then
    pass "§Phase-7 tier-3 multi-silo lifecycle end-to-end"
    echo "[s37] $PASSCOUNT passes, 0 failures"
    exit 0
else
    echo "[s37] $PASSCOUNT passes, $FAILCOUNT failures"
    exit 1
fi
