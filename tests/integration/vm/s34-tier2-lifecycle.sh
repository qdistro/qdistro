#!/bin/bash
# In-VM driver for tiered-isolation.bats:phase7-tier2-lifecycle.
#
# Verifies that two tier-2 containers can run concurrently and that
# stopping one cleanly removes its outer-side proxy toplevel without
# affecting the other.
#
# PASS lines:
#   "two containers running concurrently"
#   "stop(A) → toplevel_removed handle=<N>"
#   "container B still running after A stop"
#   "§Phase-7 tier-2 lifecycle (multi-container + stop-cleanup) end-to-end"

set -u

PASSCOUNT=0
FAILCOUNT=0

pass() { echo "PASS: $*"; PASSCOUNT=$((PASSCOUNT + 1)); }
fail() { echo "FAIL: $*"; FAILCOUNT=$((FAILCOUNT + 1)); }
skip() { echo "SKIP: $*"; exit 0; }

SRC=/root/qdistro-src/qdistro
# Stage tier2/ scripts under /tmp where the unprivileged admin user can
# reach them — /root is mode 0700 on the test VM.
TIER2_DIR=/tmp/qdistro-tier2
COMMON_LIB_DIR=/tmp/lib
if [ -d "$SRC/tier2" ]; then
    rm -rf "$TIER2_DIR" 2>/dev/null || true
    cp -r "$SRC/tier2" "$TIER2_DIR"
    chmod -R a+rX "$TIER2_DIR"
    find "$TIER2_DIR" -name '*.sh' -exec chmod a+rx {} +
fi
if [ -d "$SRC/lib" ]; then
    rm -rf "$COMMON_LIB_DIR" 2>/dev/null || true
    cp -r "$SRC/lib" "$COMMON_LIB_DIR"
    chmod -R a+rX "$COMMON_LIB_DIR"
fi
WORKLOAD=weston-terminal
IMAGE="qdistro/tier2-${WORKLOAD}:latest"
A=tier2-c-a
B=tier2-c-b

command -v podman >/dev/null 2>&1 || skip "podman not installed in this VM"
[ -d "$TIER2_DIR" ] || skip "tier2 source not unpacked at $TIER2_DIR"
[ -f "$COMMON_LIB_DIR/spawn-common.sh" ] || skip "spawn-common library not unpacked at $COMMON_LIB_DIR"
runuser -u admin -- podman image exists "$IMAGE" 2>/dev/null \
    || skip "$IMAGE absent — run s32 first to build it"

ADMIN_UID=1000
RUNTIME_DIR="/run/user/$ADMIN_UID"
runuser -u admin -- test -S "$RUNTIME_DIR/wayland-1" \
    || skip "outer compositor not up"

CURSOR=$(journalctl -n0 --show-cursor 2>/dev/null \
    | awk -F': ' '/-- cursor:/ {print $2}')

# Bring up A then B.
runuser -u admin -- podman rm -f "$A" "$B" >/dev/null 2>&1 || true

SPAWN_A_OUT=$(mktemp)
SPAWN_B_OUT=$(mktemp)

# (no detach — caller backgrounds the whole spawn)
    runuser -u admin -- bash "$TIER2_DIR/spawn-tier2.sh" \
        "$A" "$WORKLOAD" -- weston-terminal \
        >"$SPAWN_A_OUT" 2>/tmp/s34-spawn-a.log &
SPAWN_A_PID=$!

# Wait for A's container to be running before launching B (avoids
# secctx-listener filename collisions on slow VMs).
for _ in $(seq 1 30); do
    runuser -u admin -- podman ps --format '{{.Names}}' 2>/dev/null | grep -qx "$A" && break
    sleep 0.5
done

# (no detach — caller backgrounds the whole spawn)
    runuser -u admin -- bash "$TIER2_DIR/spawn-tier2.sh" \
        "$B" "$WORKLOAD" -- weston-terminal \
        >"$SPAWN_B_OUT" 2>/tmp/s34-spawn-b.log &
SPAWN_B_PID=$!

for _ in $(seq 1 30); do
    runuser -u admin -- podman ps --format '{{.Names}}' 2>/dev/null | grep -qx "$B" && break
    sleep 0.5
done

# Both running?
if runuser -u admin -- podman ps --format '{{.Names}}' 2>/dev/null | grep -qx "$A" \
   && runuser -u admin -- podman ps --format '{{.Names}}' 2>/dev/null | grep -qx "$B"; then
    pass "two containers running concurrently"
else
    fail "expected both '$A' and '$B' in 'podman ps'"
    runuser -u admin -- podman ps -a >&2
fi

# Wait for both advertise lines, capture handles.
HANDLE_A=""
HANDLE_B=""
deadline=$(( $(date +%s) + 30 ))
TOKEN_A=$(awk -F= '/^LAUNCH_TOKEN=/{print $2; exit}' "$SPAWN_A_OUT" 2>/dev/null)
TOKEN_B=$(awk -F= '/^LAUNCH_TOKEN=/{print $2; exit}' "$SPAWN_B_OUT" 2>/dev/null)

# Each toplevel_added line emitted by qdwin includes an opaque handle.
# We correlate handles to containers by ordering: A spawns first, so
# its advertise line is the first one seen since CURSOR. Subsequent
# advertise lines belong to B. Robust enough for two containers; the
# scalable design would tag advertise lines with secctx instance_id
# (future work tracked in doc/containers.md).
while [ "$(date +%s)" -lt "$deadline" ]; do
    mapfile -t lines < <(journalctl --after-cursor="$CURSOR" 2>/dev/null \
        | grep "qdwin/nested-proxy: created handle=" || true)
    if [ "${#lines[@]}" -ge 2 ]; then
        HANDLE_A=$(echo "${lines[0]}" | sed -n 's/.*handle=\([0-9]*\).*/\1/p')
        HANDLE_B=$(echo "${lines[1]}" | sed -n 's/.*handle=\([0-9]*\).*/\1/p')
        break
    fi
    sleep 0.5
done

if [ -z "$HANDLE_A" ] || [ -z "$HANDLE_B" ]; then
    fail "did not observe two distinct inner-toplevel advertise lines within 30s"
fi

# Stop A and look for toplevel_removed handle=$HANDLE_A.
runuser -u admin -- podman stop -t 2 "$A" >/dev/null 2>&1 || true

REMOVED_LINE=""
deadline=$(( $(date +%s) + 15 ))
while [ "$(date +%s)" -lt "$deadline" ]; do
    REMOVED_LINE=$(journalctl --after-cursor="$CURSOR" 2>/dev/null \
        | grep -m1 "qdwin/nested-proxy: destroy handle=$HANDLE_A" || true)
    [ -n "$REMOVED_LINE" ] && break
    sleep 0.5
done

if [ -n "$REMOVED_LINE" ]; then
    pass "stop(A) → toplevel_removed handle=$HANDLE_A"
else
    fail "no toplevel_removed for handle=$HANDLE_A within 15s after stop"
fi

# B should still be running.
if runuser -u admin -- podman ps --format '{{.Names}}' 2>/dev/null | grep -qx "$B"; then
    pass "container B still running after A stop"
else
    fail "container B unexpectedly stopped after stop(A)"
fi

# Cleanup.
runuser -u admin -- podman stop -t 2 "$B" >/dev/null 2>&1 || true
wait "$SPAWN_A_PID" "$SPAWN_B_PID" 2>/dev/null || true
rm -f "$SPAWN_A_OUT" "$SPAWN_B_OUT" /tmp/s34-spawn-a.log /tmp/s34-spawn-b.log 2>/dev/null || true

if [ "$FAILCOUNT" -eq 0 ]; then
    pass "§Phase-7 tier-2 lifecycle (multi-container + stop-cleanup) end-to-end"
    echo "[s34] $PASSCOUNT passes, 0 failures"
    exit 0
else
    echo "[s34] $PASSCOUNT passes, $FAILCOUNT failures"
    exit 1
fi
