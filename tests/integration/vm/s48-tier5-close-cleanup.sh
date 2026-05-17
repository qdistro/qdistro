#!/bin/bash
# In-VM driver for tiered-isolation.bats:phase7-tier5-close-cleanup.
#
# Asserts spawn-tier5.sh's teardown path: signal the wrapper, then
# verify the libvirt domain is destroyed AND the per-VM overlay qcow2
# is unlinked. This is the orphan-resource budget for tier-5 — anything
# left around is a leak.
#
# Pairs with `tests/integration/permissions-gui/21-tier5-close-cleanup.md`
# which drives the same teardown via the close button on the toplevel.

set -u

PASSCOUNT=0
FAILCOUNT=0

pass() { echo "PASS: $*"; PASSCOUNT=$((PASSCOUNT + 1)); }
fail() { echo "FAIL: $*"; FAILCOUNT=$((FAILCOUNT + 1)); }
skip() { echo "SKIP: $*"; exit 0; }

SRC=/root/qdistro-src/qdistro
TIER5_DIR=/tmp/qdistro-tier5
if [ -d "$SRC/tier5-vm" ]; then
    rm -rf "$TIER5_DIR" 2>/dev/null || true
    cp -r "$SRC/tier5-vm" "$TIER5_DIR"
    chmod -R a+rX "$TIER5_DIR"
    find "$TIER5_DIR" -name '*.sh' -exec chmod a+rx {} +
fi
[ -d "$TIER5_DIR" ] || skip "tier5-vm source not unpacked at $TIER5_DIR"

BASE=/var/lib/libvirt/images/qdistro-tier5-base.qcow2
[ -f "$BASE" ] || skip "tier-5 base image $BASE not built"
command -v virsh >/dev/null 2>&1 || skip "virsh not installed"
[ -e /dev/kvm ] || skip "/dev/kvm not present"

ADMIN_UID=1000
if ! runuser -u admin -- test -S "/run/user/$ADMIN_UID/wayland-1"; then
    skip "outer admin compositor not up"
fi

VM_NAME="qdistro-tier5-cleanup-$$"
OVERLAY="/home/admin/.local/share/libvirt/images/$VM_NAME.qcow2"
SPAWN_LOG=/tmp/s48-spawn.log
: >"$SPAWN_LOG"

bash "$TIER5_DIR/spawn-tier5.sh" --vm "$VM_NAME" \
    -- weston-terminal >"$SPAWN_LOG" 2>&1 &
SPAWN_PID=$!

# Wait until the domain is running and overlay exists, then we can
# trigger teardown.
deadline=$(( $(date +%s) + 90 ))
READY=0
while [ "$(date +%s)" -lt "$deadline" ]; do
    if runuser -u admin -- virsh domstate "$VM_NAME" 2>/dev/null \
        | grep -qw running && [ -f "$OVERLAY" ]; then
        READY=1; break
    fi
    sleep 0.5
done
if [ "$READY" = "1" ]; then
    pass "domain $VM_NAME running and overlay $OVERLAY exists"
else
    cat "$SPAWN_LOG" >&2 || true
    kill -KILL "$SPAWN_PID" 2>/dev/null || true
    fail "domain never reached running state within 90s — cannot test teardown"
    runuser -u admin -- virsh destroy "$VM_NAME" >/dev/null 2>&1 || true
    runuser -u admin -- virsh undefine "$VM_NAME" >/dev/null 2>&1 || true
    rm -f "$OVERLAY"
    echo "[s48] $PASSCOUNT passes, $FAILCOUNT failures"
    exit 1
fi

# Trigger teardown: SIGTERM the wrapper. The trap inside spawn-tier5.sh
# should: kill waypipe halves, virsh destroy, virsh undefine, rm overlay
# (because TIER5_KEEP_DOMAIN is unset).
kill -TERM "$SPAWN_PID" 2>/dev/null || true
wait "$SPAWN_PID" 2>/dev/null || true

# Allow up to 10s for the trap's virsh destroy/undefine to complete.
deadline=$(( $(date +%s) + 10 ))
DOMAIN_GONE=0
OVERLAY_GONE=0
while [ "$(date +%s)" -lt "$deadline" ]; do
    if ! runuser -u admin -- virsh dominfo "$VM_NAME" >/dev/null 2>&1; then
        DOMAIN_GONE=1
    fi
    [ -f "$OVERLAY" ] || OVERLAY_GONE=1
    [ "$DOMAIN_GONE" = "1" ] && [ "$OVERLAY_GONE" = "1" ] && break
    sleep 0.5
done

[ "$DOMAIN_GONE" = "1" ] \
    && pass "libvirt domain $VM_NAME destroyed + undefined by trap" \
    || fail "domain $VM_NAME still defined 10s after SIGTERM (orphan domain leak)"

[ "$OVERLAY_GONE" = "1" ] \
    && pass "per-VM overlay $OVERLAY unlinked by trap" \
    || fail "overlay $OVERLAY still present 10s after SIGTERM (orphan disk leak)"

# Final belt-and-braces cleanup in case of partial leak.
runuser -u admin -- virsh destroy "$VM_NAME" >/dev/null 2>&1 || true
runuser -u admin -- virsh undefine "$VM_NAME" >/dev/null 2>&1 || true
rm -f "$OVERLAY" "$SPAWN_LOG"

if [ "$FAILCOUNT" -eq 0 ]; then
    pass "§Phase-7 tier-5 SIGTERM teardown reclaims all per-VM resources"
    echo "[s48] $PASSCOUNT passes, 0 failures"
    exit 0
else
    echo "[s48] $PASSCOUNT passes, $FAILCOUNT failures"
    exit 1
fi
