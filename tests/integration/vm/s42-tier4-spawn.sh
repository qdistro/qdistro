#!/bin/bash
# In-VM driver for tiered-isolation.bats:phase7-tier4-spawn.
#
# Smoke-test spawn-tier4.sh's two test modes:
#   - TIER4_DOMAIN_DEFINE_ONLY=1 — define the libvirt domain from
#     domain-template.xml, then exit without starting it. Confirms
#     XML substitution + virsh define work.
#   - TIER4_NO_VIEWER=1 — define + start the domain, skip
#     waypipe-client launch. Confirms qemu/libvirt actually boots the
#     domain (it'll halt at "no bootable device" — that's expected
#     since the template has no disk).
#
# Expected PASS count on success: 5.
# PASS strings here MUST match assert_output_contains in the bats
# @test phase7-tier4-spawn block, including the final summary line.

set -u

PASSCOUNT=0
FAILCOUNT=0

pass() { echo "PASS: $*"; PASSCOUNT=$((PASSCOUNT + 1)); }
fail() { echo "FAIL: $*"; FAILCOUNT=$((FAILCOUNT + 1)); }
skip() { echo "SKIP: $*"; exit 0; }

SRC=/root/qdistro-src/qdistro
TIER4_DIR=/tmp/qdistro-tier4
if [ -d "$SRC/tier4-vm" ]; then
    rm -rf "$TIER4_DIR" 2>/dev/null || true
    cp -r "$SRC/tier4-vm" "$TIER4_DIR"
    chmod -R a+rX "$TIER4_DIR"
    find "$TIER4_DIR" -name '*.sh' -exec chmod a+rx {} +
fi
[ -d "$TIER4_DIR" ] || skip "tier4-vm source not unpacked at $TIER4_DIR"

command -v virsh >/dev/null 2>&1 || skip "virsh not installed in this VM"
command -v qemu-system-x86_64 >/dev/null 2>&1 || skip "qemu-system-x86_64 not installed"
[ -e /dev/kvm ] || skip "/dev/kvm not present (nested-virt not enabled for this VM)"

ADMIN_UID=1000
if ! runuser -u admin -- test -S "/run/user/$ADMIN_UID/wayland-1"; then
    skip "outer admin compositor not up (wayland-1 socket missing)"
fi
pass "outer admin compositor up"

VM_NAME_DEFINE="qdistro-tier4-define-$$"
VM_NAME_RUN="qdistro-tier4-run-$$"

cleanup() {
    for vm in "$VM_NAME_DEFINE" "$VM_NAME_RUN"; do
        runuser -u admin -- virsh destroy "$vm" >/dev/null 2>&1 || true
        runuser -u admin -- virsh undefine "$vm" >/dev/null 2>&1 || true
    done
}
trap cleanup EXIT

# --- mode 1: TIER4_DOMAIN_DEFINE_ONLY ---
DEFINE_LOG=/tmp/s42-define.log
if TIER4_DOMAIN_DEFINE_ONLY=1 \
   bash "$TIER4_DIR/spawn-tier4.sh" "$VM_NAME_DEFINE" >"$DEFINE_LOG" 2>&1; then
    if runuser -u admin -- virsh dominfo "$VM_NAME_DEFINE" >/dev/null 2>&1; then
        pass "define-only mode created domain"
    else
        cat "$DEFINE_LOG" >&2
        fail "spawn-tier4 define-only returned 0 but virsh dominfo $VM_NAME_DEFINE fails"
    fi
else
    cat "$DEFINE_LOG" >&2
    fail "spawn-tier4 TIER4_DOMAIN_DEFINE_ONLY=1 exited non-zero"
fi

# --- mode 2: TIER4_NO_VIEWER ---
RUN_LOG=/tmp/s42-run.log
TIER4_NO_VIEWER=1 \
    bash "$TIER4_DIR/spawn-tier4.sh" "$VM_NAME_RUN" >"$RUN_LOG" 2>&1 &
RUN_PID=$!

# spawn-tier4.sh in NO_VIEWER mode is expected to either define+start
# and then exit (depending on impl), or block until the domain stops.
# Treat both shapes — wait up to 30s for the domain to reach running.
DOMAIN_RUNNING=0
for _ in $(seq 1 60); do
    if runuser -u admin -- virsh dominfo "$VM_NAME_RUN" >/dev/null 2>&1; then
        if runuser -u admin -- virsh domstate "$VM_NAME_RUN" 2>/dev/null \
            | grep -qw running; then
            DOMAIN_RUNNING=1; break
        fi
    fi
    sleep 0.5
done

if [ "$DOMAIN_RUNNING" = "1" ]; then
    pass "tier-4 no-viewer mode brought up domain"
    pass "virsh confirms domain is running"
else
    cat "$RUN_LOG" >&2
    fail "domain $VM_NAME_RUN never reached running state within 30s"
fi

# Let the wrapper finish (if it's still going).
kill -TERM "$RUN_PID" 2>/dev/null || true
wait "$RUN_PID" 2>/dev/null || true

rm -f "$DEFINE_LOG" "$RUN_LOG"

if [ "$FAILCOUNT" -eq 0 ]; then
    pass "§Phase-7 tier-4 spawn smoke end-to-end"
    echo "[s42] $PASSCOUNT passes, 0 failures"
    exit 0
else
    echo "[s42] $PASSCOUNT passes, $FAILCOUNT failures"
    exit 1
fi
