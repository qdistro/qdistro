#!/bin/bash
# In-VM driver for the tier-2 podapps discovery bats test.
#
# Spawns a single weston-terminal tier-2 container (reusing the
# cached image), runs `tier2/podapps-scan.sh`, asserts that the
# /var/lib/qdistro/podapps/<container>/apps.json file is created
# and well-formed (parseable + at least one entry + the silo field
# matches "tier2/<container>").
#
# PASS lines:
#   "container running for scan"
#   "podapps cache file present"
#   "podapps cache parses + has entry"
#   "entry carries silo tier2/<container>"
#   "§Phase-7 tier-2 podapps discovery end-to-end"

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
CONTAINER=tier2-c-discovery
IMAGE="qdistro/tier2-${WORKLOAD}:latest"
CACHE_DIR="/var/lib/qdistro/podapps/$CONTAINER"

command -v podman >/dev/null 2>&1 || skip "podman not installed"
[ -d "$TIER2_DIR" ] || skip "tier2 source not unpacked at $TIER2_DIR"
[ -f "$COMMON_LIB_DIR/spawn-common.sh" ] || skip "spawn-common library not unpacked at $COMMON_LIB_DIR"
command -v python3 >/dev/null 2>&1 || skip "python3 needed for scan"

runuser -u admin -- podman image exists "$IMAGE" 2>/dev/null \
    || skip "$IMAGE absent — run s32 first to build it"

ADMIN_UID=1000
runuser -u admin -- test -S /run/user/$ADMIN_UID/wayland-1 \
    || skip "outer compositor not up"

# Spawn container detached, then scan.
runuser -u admin -- podman rm -f "$CONTAINER" >/dev/null 2>&1 || true
SPAWN_OUT=$(mktemp)
# (no detach — caller backgrounds the whole spawn)
    runuser -u admin -- bash "$TIER2_DIR/spawn-tier2.sh" \
        "$CONTAINER" "$WORKLOAD" -- weston-terminal \
        >"$SPAWN_OUT" 2>/tmp/s59-spawn.log &
SPAWN_PID=$!

for _ in $(seq 1 30); do
    runuser -u admin -- podman ps --format '{{.Names}}' 2>/dev/null | grep -qx "$CONTAINER" && break
    sleep 0.5
done

if runuser -u admin -- podman ps --format '{{.Names}}' 2>/dev/null | grep -qx "$CONTAINER"; then
    pass "container running for scan"
else
    fail "container did not start within 15s"
    exit 1
fi

# Run the scan as root (writes to /var/lib/qdistro).
rm -rf "$CACHE_DIR" 2>/dev/null || true
if QDISTRO_PODAPPS_AS_USER=admin bash "$TIER2_DIR/podapps-scan.sh" "$CONTAINER" >/tmp/s59-scan.log 2>&1; then
    :
else
    cat /tmp/s59-scan.log >&2
    fail "podapps-scan exited non-zero"
fi

if [ -f "$CACHE_DIR/apps.json" ]; then
    pass "podapps cache file present"
else
    fail "$CACHE_DIR/apps.json not created"
fi

if [ -f "$CACHE_DIR/apps.json" ] && python3 -c "
import json,sys
data=json.load(open('$CACHE_DIR/apps.json'))
sys.exit(0 if isinstance(data, list) and len(data) >= 1 else 1)
"; then
    pass "podapps cache parses + has entry"
else
    fail "cache JSON does not parse or has no entries"
fi

if [ -f "$CACHE_DIR/apps.json" ] && python3 -c "
import json,sys
data=json.load(open('$CACHE_DIR/apps.json'))
ok = any(e.get('silo') == 'tier2/$CONTAINER' for e in data)
sys.exit(0 if ok else 1)
"; then
    pass "entry carries silo tier2/$CONTAINER"
else
    fail "no entry with silo=tier2/$CONTAINER"
fi

# Cleanup.
runuser -u admin -- podman stop -t 2 "$CONTAINER" >/dev/null 2>&1 || true
wait "$SPAWN_PID" 2>/dev/null || true
rm -f "$SPAWN_OUT" /tmp/s59-spawn.log /tmp/s59-scan.log 2>/dev/null || true

if [ "$FAILCOUNT" -eq 0 ]; then
    pass "§Phase-7 tier-2 podapps discovery end-to-end"
    echo "[s59] $PASSCOUNT passes, 0 failures"
    exit 0
else
    echo "[s59] $PASSCOUNT passes, $FAILCOUNT failures"
    exit 1
fi
