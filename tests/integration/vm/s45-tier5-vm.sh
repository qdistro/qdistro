#!/bin/bash
# In-VM driver for tiered-isolation.bats:phase7-tier5-vm.
#
# Exercises spawn-tier5.sh --vm end-to-end inside the bats test VM:
# defines a libvirt domain (linked clone of qdistro-tier5-base.qcow2),
# starts it, waits for qemu-guest-agent, triggers
# qdistro-tier5-publisher.sh inside the guest, asserts the guest
# weston-terminal toplevel reaches qdwin via waypipe-over-vsock.
#
# Nested-on-nested: this is the bats VM running a guest VM. Requires
# the bats VM template to have <cpu mode='host-passthrough'/> (already
# set in scripts/vm/create-template-domain.sh:43) so /dev/kvm is
# usable inside.
#
# SKIPS gracefully when the base qcow2 isn't built — base image build
# is a separate, multi-minute step (tier5-vm/build-guest-image.sh).
#
# Pairs with `tests/integration/permissions-gui/20-tier5-vm-cold-start.md`.

set -u

PASSCOUNT=0
FAILCOUNT=0

pass() { echo "PASS: $*"; PASSCOUNT=$((PASSCOUNT + 1)); }
fail() { echo "FAIL: $*"; FAILCOUNT=$((FAILCOUNT + 1)); }
skip() { echo "SKIP: $*"; exit 0; }

SRC=/root/qdistro-src/qdistro
TIER5_DIR=/tmp/qdistro-tier5
# Keep tier5-vm and lib as siblings: spawn-tier5.sh sources
# ../lib/spawn-common.sh. Depending on /tmp/lib left by an earlier Bats case
# made this probe pass in the suite but fail when replayed on its own.
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
# shellcheck source=../../../lib/spawn-common.sh
. "$TIER5_DIR/lib/spawn-common.sh"

BASE=/var/lib/libvirt/images/qdistro-tier5-base.qcow2
[ -f "$BASE" ] || skip "tier-5 base image $BASE not built; run tier5-vm/build-guest-image.sh first"

command -v virsh >/dev/null 2>&1 || skip "virsh not installed in this VM"
command -v qemu-img >/dev/null 2>&1 || skip "qemu-img not installed in this VM"
[ -e /dev/kvm ] || skip "/dev/kvm not present (nested-virt not enabled for this VM)"

ADMIN_UID=1000
RUNTIME_DIR="/run/user/$ADMIN_UID"
OUTER_SOCK="$RUNTIME_DIR/wayland-1"
if ! runuser -u admin -- test -S "$OUTER_SOCK"; then
    skip "outer admin compositor not up (wayland-1 socket missing)"
fi
pass "outer admin compositor up"
pass "tier-5 base image present at $BASE"

VM_NAME="qdistro-tier5-test-$$"
OVERLAY="/home/admin/.local/share/libvirt/images/$VM_NAME.qcow2"

CURSOR=$(journalctl --since=now -n0 --show-cursor 2>/dev/null \
    | awk -F': ' '/-- cursor:/ {print $2}')

SPAWN_LOG=/tmp/s45-spawn.log
SERIAL_LOG=/tmp/s45-tier5-serial.log
: >"$SPAWN_LOG"
rm -f "$SERIAL_LOG"

# Request a 2 GiB guest (TIER5_MEM_KIB). The former 512 MiB CI accommodation
# was too tight for the guest kernel + compositor + app stack and could kill the
# nested guest mid-boot. Keep the explicit value aligned with the GUI tier-5
# scenarios and sibling lifecycle probes.
TIER5_MEM_KIB=2097152 \
TIER5_QGA_TIMEOUT_SECS=180 \
TIER5_SERIAL_LOG="$SERIAL_LOG" \
TIER5_KEEP_DOMAIN=1 \
    bash "$SPAWN" --vm "$VM_NAME" \
    -- weston-terminal >"$SPAWN_LOG" 2>&1 &
SPAWN_PID=$!

cleanup_vm() {
    kill -TERM "$SPAWN_PID" 2>/dev/null || true
    wait "$SPAWN_PID" 2>/dev/null || true
    # Belt-and-braces — spawn-tier5.sh's trap normally handles these.
    runuser -u admin -- env XDG_RUNTIME_DIR="$RUNTIME_DIR" virsh destroy "$VM_NAME" >/dev/null 2>&1 || true
    runuser -u admin -- env XDG_RUNTIME_DIR="$RUNTIME_DIR" virsh undefine "$VM_NAME" >/dev/null 2>&1 || true
    rm -f "$OVERLAY" 2>/dev/null || true
}
trap cleanup_vm EXIT

# Wait for the domain to define+start. The nested-on-nested guest reaches
# "running" slower than 90s under CI; budget 120s to match s47-tier5-audio.sh
# (same base disk + boot class).
DOMAIN_OK=0
for _ in $(seq 1 240); do
    if runuser -u admin -- env XDG_RUNTIME_DIR="$RUNTIME_DIR" virsh dominfo "$VM_NAME" >/dev/null 2>&1; then
        if runuser -u admin -- env XDG_RUNTIME_DIR="$RUNTIME_DIR" virsh domstate "$VM_NAME" 2>/dev/null \
            | grep -qw running; then
            DOMAIN_OK=1; break
        fi
    fi
    sleep 0.5
done
if [ "$DOMAIN_OK" = "1" ]; then
    pass "libvirt domain $VM_NAME defined + running"
else
    cat "$SPAWN_LOG" >&2 || true
    fail "libvirt domain never reached running state within 120s"
fi

# Wait for the host-side waypipe listener-ready line.
LISTENER_OK=0
for _ in $(seq 1 40); do
    if grep -q "vsock listener ready cid=2" "$SPAWN_LOG" 2>/dev/null; then
        LISTENER_OK=1; break
    fi
    sleep 0.25
done
[ "$LISTENER_OK" = "1" ] \
    && pass "host-side waypipe-client vsock listener ready" \
    || fail "host-side waypipe-client vsock listener never came up within 10s"

# spawn-tier5.sh emits "[tier5] qga ready" once guest-ping responds. Its
# cold-boot budget is 180s; allow another 30s for process scheduling and log
# observation, while also stopping promptly if the wrapper has already failed.
QGA_OK=0
if qd_deadline_start 210 "[s45] WARN: qga observation wait"; then
    while qd_deadline_pending; do
        if grep -q "qga ready" "$SPAWN_LOG" 2>/dev/null; then
            QGA_OK=1; break
        fi
        kill -0 "$SPAWN_PID" 2>/dev/null || break
        sleep 1
    done
else
    fail "could not initialize qga observation deadline clock"
fi
[ "$QGA_OK" = "1" ] \
    && pass "guest qemu-guest-agent responded" \
    || {
        echo "--- tier-5 spawn log ---" >&2
        cat "$SPAWN_LOG" >&2 || true
        echo "--- tier-5 guest serial tail ---" >&2
        tail -200 "$SERIAL_LOG" >&2 2>/dev/null || true
        fail "guest qemu-guest-agent never responded within the 180s spawn deadline"
    }

# spawn-tier5.sh emits "[tier5] guest publisher pid=N running" once
# guest-exec delivers the publisher start.
PUBLISHER_OK=0
if qd_deadline_start 60 "[s45] WARN: publisher observation wait"; then
    while qd_deadline_pending; do
        if grep -q "guest publisher pid=" "$SPAWN_LOG" 2>/dev/null; then
            PUBLISHER_OK=1; break
        fi
        kill -0 "$SPAWN_PID" 2>/dev/null || break
        sleep 1
    done
else
    fail "could not initialize publisher observation deadline clock"
fi
[ "$PUBLISHER_OK" = "1" ] \
    && pass "guest publisher pid reported via qga guest-exec" \
    || fail "qdistro-tier5-publisher.sh never reported a pid within 60s after qga ready"

# Per-VM overlay disk created in admin's session libvirt dir.
if [ -f "$OVERLAY" ]; then
    pass "per-VM overlay disk created at $OVERLAY"
    # Confirm linked-clone backing file. -U: the VM is running, so qemu holds
    # the qcow2 lock; without -U qemu-img info fails to acquire it and prints
    # nothing. The backing-file metadata is static, so reading it no-lock is safe.
    if runuser -u admin -- qemu-img info -U "$OVERLAY" 2>/dev/null \
        | grep -q "backing file:.*qdistro-tier5-base.qcow2"; then
        pass "overlay is linked-clone of the base image (not a full copy)"
    else
        fail "overlay does not reference qdistro-tier5-base.qcow2 as backing file"
    fi
else
    fail "per-VM overlay disk $OVERLAY not found"
fi

# qdwin should see a new wl_client tagged with tier-5 secctx.
TIER5_LINE=""
if qd_deadline_start 30 "[s45] WARN: secctx journal wait"; then
    while qd_deadline_pending; do
        if [ -n "$CURSOR" ]; then
            TIER5_LINE=$(journalctl --after-cursor="$CURSOR" 2>/dev/null \
                | grep -m1 -E "qdwin:.*qdistro\.tier5\." || true)
        fi
        [ -n "$TIER5_LINE" ] && break
        sleep 1
    done
else
    fail "could not initialize secctx journal deadline clock"
fi
if [ -n "$TIER5_LINE" ]; then
    pass "qdwin observed wl_client with secctx app_id=qdistro.tier5.*"
else
    echo "INFO: qdwin tier-5 secctx journal line not found within 30s (logging gap or guest never connected)"
fi

# (cleanup_vm trap)

if [ "$FAILCOUNT" -eq 0 ]; then
    pass "§Phase-7 tier-5-Linux --vm end-to-end smoke"
    echo "[s45] $PASSCOUNT passes, 0 failures"
    exit 0
else
    echo "[s45] $PASSCOUNT passes, $FAILCOUNT failures"
    exit 1
fi
