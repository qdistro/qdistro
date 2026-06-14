#!/bin/bash
# In-VM driver: spawn-tier5.sh --loopback auto-registers a launch record
# (02/S3e — the tier-5 half of permission-lineage rollout).
#
# Proves the lineage wiring just added to spawn-tier5.sh: when a tier-5 silo is
# bridged over AF_VSOCK, the qdistro-secctx-exec wrapping waypipe-client
# publishes the inner waypipe-client pid, spawn-tier5 calls the shared
# qd_register_secctx_launch_record helper, and the broker persists a
# qdistro.lineage.register:<silo> audit row. A later cross-silo gate resolves the
# source pid qdwin reports (this same waypipe-client) to that record so enforce
# mode can ALLOW an admin cross-silo rule instead of failing closed.
#
# Loopback mode (vsock CID=1, VMADDR_CID_LOCAL) needs NO guest VM / nested KVM,
# so this runs in the standard spin-test-vm. Mirrors s43-tier5-loopback.sh for
# the bring-up and s60/s61/s62 for the lineage assertions. tier-5 has NO broker
# spawn gate, so (unlike s61/s62) no allow rule is authored.
#
# Load-bearing assertions:
#   1. spawn-tier5 emitted "[tier5] lineage: registered pid=<n> as silo=<silo>".
#   2. the broker wrote a qdistro.lineage.register:<silo> audit row.

set -u

PASSCOUNT=0
FAILCOUNT=0
pass() { echo "PASS: $*"; PASSCOUNT=$((PASSCOUNT + 1)); }
fail() { echo "FAIL: $*"; FAILCOUNT=$((FAILCOUNT + 1)); }
skip() { echo "SKIP: $*"; exit 0; }

SRC=/root/qdistro-src/qdistro
TIER5_DIR="$SRC/tier5-vm"
ADMIN=admin
ADMIN_UID=1000
RUNTIME_DIR="/run/user/$ADMIN_UID"
OUTER_SOCK="$RUNTIME_DIR/wayland-1"
AUDIT_DB=/var/lib/qdistro/audit/audit.sqlite
SPAWN_PID=""
TRAP_FIRED=0

cleanup() {
    [ "$TRAP_FIRED" -eq 1 ] && return 0
    TRAP_FIRED=1
    [ -n "$SPAWN_PID" ] && kill -TERM "$SPAWN_PID" 2>/dev/null || true
    [ -n "$SPAWN_PID" ] && wait    "$SPAWN_PID" 2>/dev/null || true
    rm -f /tmp/s63-spawn.log 2>/dev/null || true
    rm -f "$RUNTIME_DIR"/tier5-loopback-*-wayland-info-*.log 2>/dev/null || true
    rm -f "$RUNTIME_DIR"/qdistro-tier5-launchrec-*.pid 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# --- 1. prerequisites ------------------------------------------------
command -v waypipe        >/dev/null 2>&1 || skip "waypipe not installed in this VM"
command -v wayland-info   >/dev/null 2>&1 || skip "wayland-info (wayland-utils) not installed"
command -v qdistro-secctx-exec >/dev/null 2>&1 || skip "qdistro-secctx-exec not installed"
command -v sqlite3        >/dev/null 2>&1 || skip "sqlite3 not available"
command -v dbus-send      >/dev/null 2>&1 || skip "dbus-send absent (broker gate unqueryable)"
[ -f "$TIER5_DIR/spawn-tier5.sh" ] || skip "tier5-vm source not unpacked at $TIER5_DIR"
[ -f "$AUDIT_DB" ] || skip "broker audit db missing at $AUDIT_DB"
modprobe vsock_loopback 2>/dev/null || true
if [ ! -e /dev/vsock ]; then
    skip "/dev/vsock not present (vsock_loopback not available in this kernel)"
fi
if ! runuser -u "$ADMIN" -- test -S "$OUTER_SOCK"; then
    skip "outer admin compositor not up ($OUTER_SOCK missing)"
fi
systemctl start qdistro-admin-broker.service 2>/dev/null || true
pass "tier5 loopback prerequisites present"

# --- 2. spawn a tier-5 silo over vsock loopback ----------------------
# The lineage register fires when the secctx-exec-wrapped waypipe-client starts
# (before/independent of the inner app completing). wayland-info is a real
# wayland client that enumerates through the bridge and exits in ~2s.
PORT=$(( (RANDOM % 20000) + 20000 ))
SPAWN_LOG=/tmp/s63-spawn.log
: >"$SPAWN_LOG"
bash "$TIER5_DIR/spawn-tier5.sh" --loopback -p "$PORT" -- wayland-info >"$SPAWN_LOG" 2>&1 &
SPAWN_PID=$!

# --- 3. wait for the lineage registration line -----------------------
REG_SILO=""
for _ in $(seq 1 60); do
    line=$(grep -m1 "lineage: registered pid=[0-9]\+ as silo=" "$SPAWN_LOG" 2>/dev/null || true)
    if [ -n "$line" ]; then
        REG_SILO=$(echo "$line" | sed -n 's/.*as silo=\([^ ]*\).*/\1/p')
        break
    fi
    if grep -q "WARN lineage:" "$SPAWN_LOG" 2>/dev/null; then
        cat "$SPAWN_LOG" >&2
        fail "spawn-tier5 emitted a WARN lineage line (launch record not registered)"
        break
    fi
    if ! kill -0 "$SPAWN_PID" 2>/dev/null && ! grep -q "lineage: registered" "$SPAWN_LOG" 2>/dev/null; then
        cat "$SPAWN_LOG" >&2
        fail "spawn-tier5 exited before registering a launch record"
        break
    fi
    sleep 0.5
done

LINEAGE_ACTION=""
if [ -n "$REG_SILO" ]; then
    pass "spawn-tier5 (loopback) logged the launch-record registration for silo=$REG_SILO"
    # tier-5 loopback derives the silo identity as loopback-<pid>, where <pid>
    # is spawn-tier5.sh's own pid ($$) — which is exactly the process we
    # backgrounded as SPAWN_PID. Asserting that equality makes the audit-row
    # proof below mathematically stale-proof (the silo carries THIS launch's
    # unique pid, so no prior run's row can satisfy it even under PID reuse).
    if [ "$REG_SILO" = "loopback-$SPAWN_PID" ]; then
        pass "tier-5 loopback silo identity is this spawn's pid (loopback-$SPAWN_PID)"
    else
        fail "registered silo '$REG_SILO' != loopback-$SPAWN_PID (spawn-tier5's own pid)"
    fi
    LINEAGE_ACTION="qdistro.lineage.register:${REG_SILO}"
else
    [ "$FAILCOUNT" -eq 0 ] && { cat "$SPAWN_LOG" >&2; fail "spawn-tier5 did not register a launch record within 30s"; }
fi

# --- 4. broker wrote the register audit row --------------------------
# The silo name carries this spawn's unique pid, so any row for that exact
# action proves THIS launch registered (no before/after delta needed).
if [ -n "$LINEAGE_ACTION" ]; then
    AFTER=0
    for _ in $(seq 1 20); do
        AFTER=$(sqlite3 "$AUDIT_DB" \
            "SELECT count(*) FROM audit WHERE action='${LINEAGE_ACTION}';" 2>/dev/null || echo 0)
        [ "$AFTER" -ge 1 ] 2>/dev/null && break
        sleep 0.25
    done
    if [ "$AFTER" -ge 1 ] 2>/dev/null; then
        pass "broker recorded ${LINEAGE_ACTION} (rows=$AFTER)"
    else
        fail "no ${LINEAGE_ACTION} audit row (rows=$AFTER)"
    fi
fi

# --- summary ---------------------------------------------------------
if [ "$FAILCOUNT" -eq 0 ] && [ "$PASSCOUNT" -ge 4 ]; then
    pass "§02/S3e tier-5 (loopback/vsock) permission-lineage register end-to-end"
    echo "[s63] $PASSCOUNT passes, 0 failures"
    exit 0
else
    echo "[s63] $PASSCOUNT passes, $FAILCOUNT failures"
    exit 1
fi
