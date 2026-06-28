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
# Stage tier5-vm AND lib as siblings so spawn-tier5.sh's `../lib/spawn-common.sh`
# source resolves (gen_launch_token / domain_xml_tmpfile live there). A flat copy
# of just tier5-vm leaves `../lib` pointing at /tmp/lib and the wrapper aborts at
# virsh define. Mirrors permissions-gui/21 + s49.
if [ -d "$SRC/tier5-vm" ]; then
    rm -rf "$TIER5_DIR" 2>/dev/null || true
    mkdir -p "$TIER5_DIR"
    cp -r "$SRC/tier5-vm" "$TIER5_DIR/tier5-vm"
    cp -r "$SRC/lib" "$TIER5_DIR/lib"
    chmod -R a+rX "$TIER5_DIR"
    find "$TIER5_DIR" -name '*.sh' -exec chmod a+rx {} +
fi
SPAWN="$TIER5_DIR/tier5-vm/spawn-tier5.sh"
[ -f "$SPAWN" ] || skip "tier5-vm source not unpacked at $SPAWN"

BASE=/var/lib/libvirt/images/qdistro-tier5-base.qcow2
[ -f "$BASE" ] || skip "tier-5 base image $BASE not built"
command -v virsh >/dev/null 2>&1 || skip "virsh not installed"
[ -e /dev/kvm ] || skip "/dev/kvm not present"

ADMIN_UID=1000
RUNTIME_DIR="/run/user/$ADMIN_UID"
if ! runuser -u admin -- test -S "/run/user/$ADMIN_UID/wayland-1"; then
    skip "outer admin compositor not up"
fi

VM_NAME="qdistro-tier5-cleanup-$$"
OVERLAY="/home/admin/.local/share/libvirt/images/$VM_NAME.qcow2"
SPAWN_LOG=/tmp/s48-spawn.log
: >"$SPAWN_LOG"

# Baseline host-side waypipe pids (admin owns the tier-5 waypipe client). We
# diff against this after spawn so we only assert on OUR waypipe, never another
# concurrent tier-5 VM's.
waypipe_pids() { pgrep -u admin -x waypipe 2>/dev/null | sort -un; }
WAYPIPE_BASELINE="$(waypipe_pids)"

# 512 MiB guest (TIER5_MEM_KIB): the ~4 GiB bats VM has only ~1.3 GiB free, so a
# 1.5 GiB nested guest is unstable here; 512 MiB fits (same value as
# s45-tier5-vm.sh / s47-tier5-audio.sh). Nested-CI accommodation; prod keeps 1.5G.
TIER5_MEM_KIB=524288 \
    bash "$SPAWN" --vm "$VM_NAME" \
    -- weston-terminal >"$SPAWN_LOG" 2>&1 &
SPAWN_PID=$!

# Wait until the domain is running and overlay exists, then we can trigger
# teardown. Budget 120s (nested-on-nested boot under CI; match s45/s47).
deadline=$(( $(date +%s) + 120 ))
READY=0
while [ "$(date +%s)" -lt "$deadline" ]; do
    if runuser -u admin -- env XDG_RUNTIME_DIR="$RUNTIME_DIR" virsh domstate "$VM_NAME" 2>/dev/null \
        | grep -qw running && [ -f "$OVERLAY" ]; then
        READY=1; break
    fi
    sleep 0.5
done
if [ "$READY" = "1" ]; then
    pass "domain $VM_NAME running and overlay $OVERLAY exists"
    # Our waypipe = current admin waypipe pids minus the pre-spawn baseline.
    NEW_WAYPIPE="$(comm -13 <(printf '%s\n' "$WAYPIPE_BASELINE") \
                            <(waypipe_pids))"
    # Must observe at least one new waypipe, else the orphan assertion below is
    # vacuous (empty set => trivially "no orphan", a false pass). A tier-5 spawn
    # ALWAYS starts a host-side waypipe client, so an empty delta is a real defect.
    if [ -z "$NEW_WAYPIPE" ]; then
        cat "$SPAWN_LOG" >&2 || true
        fail "no new host-side waypipe observed after spawn — cannot test orphan reaping"
        kill -TERM "$SPAWN_PID" 2>/dev/null || true
        wait "$SPAWN_PID" 2>/dev/null || true
        runuser -u admin -- env XDG_RUNTIME_DIR="$RUNTIME_DIR" virsh destroy "$VM_NAME" >/dev/null 2>&1 || true
        runuser -u admin -- env XDG_RUNTIME_DIR="$RUNTIME_DIR" virsh undefine "$VM_NAME" >/dev/null 2>&1 || true
        rm -f "$OVERLAY"
        echo "[s48] $PASSCOUNT passes, $FAILCOUNT failures"
        exit 1
    fi
else
    cat "$SPAWN_LOG" >&2 || true
    kill -KILL "$SPAWN_PID" 2>/dev/null || true
    fail "domain never reached running state within 120s — cannot test teardown"
    runuser -u admin -- env XDG_RUNTIME_DIR="$RUNTIME_DIR" virsh destroy "$VM_NAME" >/dev/null 2>&1 || true
    runuser -u admin -- env XDG_RUNTIME_DIR="$RUNTIME_DIR" virsh undefine "$VM_NAME" >/dev/null 2>&1 || true
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
waypipe_alive() {
    local p
    for p in $NEW_WAYPIPE; do
        kill -0 "$p" 2>/dev/null && return 0
    done
    return 1
}
deadline=$(( $(date +%s) + 10 ))
DOMAIN_GONE=0
OVERLAY_GONE=0
WAYPIPE_GONE=0
while [ "$(date +%s)" -lt "$deadline" ]; do
    if ! runuser -u admin -- env XDG_RUNTIME_DIR="$RUNTIME_DIR" virsh dominfo "$VM_NAME" >/dev/null 2>&1; then
        DOMAIN_GONE=1
    fi
    [ -f "$OVERLAY" ] || OVERLAY_GONE=1
    waypipe_alive || WAYPIPE_GONE=1
    [ "$DOMAIN_GONE" = "1" ] && [ "$OVERLAY_GONE" = "1" ] \
        && [ "$WAYPIPE_GONE" = "1" ] && break
    sleep 0.5
done

[ "$DOMAIN_GONE" = "1" ] \
    && pass "libvirt domain $VM_NAME destroyed + undefined by trap" \
    || fail "domain $VM_NAME still defined 10s after SIGTERM (orphan domain leak)"

[ "$OVERLAY_GONE" = "1" ] \
    && pass "per-VM overlay $OVERLAY unlinked by trap" \
    || fail "overlay $OVERLAY still present 10s after SIGTERM (orphan disk leak)"

# The load-bearing tier-5 waypipe is qdistro-secctx-exec's exec'd fork child.
# A teardown that kills only the wrapper parent ($CLIENT_PID) orphans it
# (perm-gui/21 + this driver). Assert it is reaped too.
ORPHAN_PIDS=""
for p in $NEW_WAYPIPE; do
    kill -0 "$p" 2>/dev/null && ORPHAN_PIDS="$ORPHAN_PIDS $p"
done
[ "$WAYPIPE_GONE" = "1" ] && [ -z "$ORPHAN_PIDS" ] \
    && pass "host-side waypipe reaped by trap (no orphan)" \
    || fail "orphaned host-side waypipe pid(s)$ORPHAN_PIDS alive 10s after SIGTERM (waypipe leak)"

# Final belt-and-braces cleanup in case of partial leak.
runuser -u admin -- env XDG_RUNTIME_DIR="$RUNTIME_DIR" virsh destroy "$VM_NAME" >/dev/null 2>&1 || true
runuser -u admin -- env XDG_RUNTIME_DIR="$RUNTIME_DIR" virsh undefine "$VM_NAME" >/dev/null 2>&1 || true
for p in $NEW_WAYPIPE; do kill -KILL "$p" 2>/dev/null || true; done
rm -f "$OVERLAY" "$SPAWN_LOG"

if [ "$FAILCOUNT" -eq 0 ]; then
    pass "§Phase-7 tier-5 SIGTERM teardown reclaims all per-VM resources"
    echo "[s48] $PASSCOUNT passes, 0 failures"
    exit 0
else
    echo "[s48] $PASSCOUNT passes, $FAILCOUNT failures"
    exit 1
fi
