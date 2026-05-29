#!/bin/bash
# In-VM driver: spawn-tier3.sh auto-registers a launch record (P1-1 rollout).
#
# Proves the registration half of permission lineage for tier-3: when a
# tier-3 app is launched via spawn-tier3.sh (which runs as root), the script
# reads the inner waypipe-client pid published by qdistro-secctx-exec and
# calls the broker's RegisterLaunch, binding (pid,starttime) -> silo. A later
# cross-silo gate resolves the source pid qdwin reports (this same
# waypipe-client) to that record, so enforce mode can ALLOW an
# admin-authored cross-silo rule instead of failing closed.
#
# Load-bearing assertions:
#   1. spawn-tier3.sh emitted the lineage line
#      "[tier3] lineage: registered waypipe-client pid=<n> as silo=user1".
#   2. the broker wrote a qdistro.lineage.register:user1 audit row (the
#      RegisterLaunch re-verified the live pid and stored the record).
#
# Pairs with s35/s36/s37 (tier3 bring-up) and the broker-level agent
# scenario permissions-gui/59-lineage-cross-silo-source-pid.md.

set -u

PASSCOUNT=0
FAILCOUNT=0
pass() { echo "PASS: $*"; PASSCOUNT=$((PASSCOUNT + 1)); }
fail() { echo "FAIL: $*"; FAILCOUNT=$((FAILCOUNT + 1)); }
skip() { echo "SKIP: $*"; exit 0; }

AUDIT_DB=/var/lib/qdistro/audit/audit.sqlite
BROKER_CONF=/etc/qdistro/broker.conf
SPAWN_PID=""
ENFORCE_WAS_SET=""
TRAP_FIRED=0
cleanup() {
    [ "$TRAP_FIRED" -eq 1 ] && return 0
    TRAP_FIRED=1
    [ -n "$SPAWN_PID" ] && kill -TERM "$SPAWN_PID" 2>/dev/null || true
    [ -n "$SPAWN_PID" ] && wait    "$SPAWN_PID" 2>/dev/null || true
    pkill -u user1 -x weston-terminal 2>/dev/null || true
    # Restore the broker's pre-test enforce posture.
    if [ "$ENFORCE_WAS_SET" = "0" ]; then
        sed -i '/^lineage_enforce/d' "$BROKER_CONF" 2>/dev/null || true
        systemctl restart qdistro-admin-broker.service 2>/dev/null || true
    fi
    rm -f /tmp/s60-spawn.log 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# --- 1. prerequisites ------------------------------------------------
SRC=/root/qdistro-src/qdistro
TIER3_DIR=/tmp/qdistro-tier3-src
COMMON_LIB_DIR=/tmp/lib
if [ -d "$SRC/tier3" ]; then
    rm -rf "$TIER3_DIR" 2>/dev/null || true
    cp -r "$SRC/tier3" "$TIER3_DIR"
    chmod -R a+rX "$TIER3_DIR"
    find "$TIER3_DIR" -name '*.sh' -exec chmod a+rx {} +
fi
[ -d "$TIER3_DIR" ] || skip "tier3 source not unpacked at $TIER3_DIR"
command -v waypipe         >/dev/null 2>&1 || skip "waypipe not installed in this VM"
command -v weston-terminal >/dev/null 2>&1 || skip "weston-terminal not installed in this VM"
command -v runuser         >/dev/null 2>&1 || skip "runuser not available"
command -v sqlite3         >/dev/null 2>&1 || skip "sqlite3 not available"
command -v qdistro-secctx-exec >/dev/null 2>&1 || skip "qdistro-secctx-exec not installed"
getent group qdistro-tier3 >/dev/null || skip "qdistro-tier3 group missing"
id -u user1 >/dev/null 2>&1 || skip "silo user 'user1' missing"
[ -f "$AUDIT_DB" ] || skip "broker audit db missing at $AUDIT_DB"
if ! runuser -u admin -- test -S "/run/user/1000/wayland-1"; then
    skip "outer admin compositor not up (wayland-1 socket missing)"
fi
pass "tier3 prerequisites present"

# --- 2. enforce mode -------------------------------------------------
# RegisterLaunch is wired unconditionally (the record is always written so
# enforce can be flipped without relaunching apps), but we run under enforce
# so the test reflects the production posture being validated.
if grep -q "^lineage_enforce" "$BROKER_CONF" 2>/dev/null; then
    ENFORCE_WAS_SET="1"
else
    ENFORCE_WAS_SET="0"
    install -d -m 0755 /etc/qdistro
    echo "lineage_enforce = true" >> "$BROKER_CONF"
    systemctl restart qdistro-admin-broker.service
    sleep 1
fi
pass "broker up (enforce posture set)"

# Baseline count of register rows for user1 so we assert a NEW one appears.
BEFORE=$(sqlite3 "$AUDIT_DB" \
    "SELECT count(*) FROM audit WHERE action='qdistro.lineage.register:user1';" \
    2>/dev/null || echo 0)

# --- 3. spawn the tier3 app ------------------------------------------
SPAWN_LOG=/tmp/s60-spawn.log
: >"$SPAWN_LOG"
TIER3_NO_REAP=1 \
bash "$TIER3_DIR/spawn-tier3.sh" user1 -- weston-terminal >"$SPAWN_LOG" 2>&1 &
SPAWN_PID=$!

# --- 4. wait for the lineage registration line -----------------------
# spawn-tier3 registers right after the bridge socket comes up (waypipe has
# exec'd by then). Allow a generous window for waypipe-client start.
REGISTERED=0
for _ in $(seq 1 60); do
    if grep -q "lineage: registered waypipe-client pid=[0-9]\+ as silo=user1" "$SPAWN_LOG" 2>/dev/null; then
        REGISTERED=1; break
    fi
    if grep -q "WARN lineage:" "$SPAWN_LOG" 2>/dev/null; then
        break
    fi
    if ! kill -0 "$SPAWN_PID" 2>/dev/null; then break; fi
    sleep 0.25
done
if [ "$REGISTERED" = "1" ]; then
    pass "spawn-tier3 logged the launch-record registration for silo=user1"
else
    cat "$SPAWN_LOG" >&2 || true
    fail "spawn-tier3 did not register a launch record within 15s"
fi

# --- 5. broker wrote the register audit row --------------------------
# Give the broker a moment to flush the audit row after RegisterLaunch.
AFTER=0
for _ in $(seq 1 20); do
    AFTER=$(sqlite3 "$AUDIT_DB" \
        "SELECT count(*) FROM audit WHERE action='qdistro.lineage.register:user1';" \
        2>/dev/null || echo 0)
    [ "$AFTER" -gt "$BEFORE" ] && break
    sleep 0.25
done
if [ "$AFTER" -gt "$BEFORE" ]; then
    pass "broker recorded qdistro.lineage.register:user1 (before=$BEFORE after=$AFTER)"
else
    fail "no new qdistro.lineage.register:user1 audit row (before=$BEFORE after=$AFTER)"
fi

# --- teardown via trap -----------------------------------------------
echo "[s60] $PASSCOUNT passes, $FAILCOUNT failures"
[ "$FAILCOUNT" -eq 0 ] || exit 1
exit 0
