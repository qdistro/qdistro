#!/bin/bash
# In-VM driver for tiered-isolation.bats:phase7-tier5-graceful-shutdown.
#
# Exercises the configurable, PER-APP tier-5 lifecycle that generalizes s48's
# hard reap:
#   1. Per-app policy resolution. A sectioned lifecycle config with a global
#      default of `force` and an `[app:<key>]` override of `graceful` must, when
#      the wrapper is launched with `--policy-key <key>`, resolve to graceful —
#      proving --policy-key + the sectioned parser + per-app-over-global
#      precedence all work end-to-end through the REAL spawn-tier5.sh.
#   2. Graceful teardown on app-exit. When the published app exits, the wrapper's
#      EXIT trap reaps the domain via `virsh shutdown` (ACPI, then qga agent, then
#      destroy) instead of an immediate destroy, and STILL reclaims the domain +
#      overlay (no orphans). On a base image without a power-button handler the
#      ACPI step is a no-op and the qga agent shutdown does the graceful poweroff;
#      either way resources must be reclaimed.
#
# Pairs with s48-tier5-close-cleanup.sh (the force/SIGTERM regression net) and
# permissions-gui/21 (which pins force). Keep both green.

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
RUNTIME_DIR="/run/user/$ADMIN_UID"
if ! runuser -u admin -- test -S "/run/user/$ADMIN_UID/wayland-1"; then
    skip "outer admin compositor not up"
fi
virsh_admin() { runuser -u admin -- env XDG_RUNTIME_DIR="$RUNTIME_DIR" virsh "$@"; }

VM_NAME="qdistro-tier5-graceful-$$"
OVERLAY="/home/admin/.local/share/libvirt/images/$VM_NAME.qcow2"
POLICY_KEY="tier5/s49graceful"
# Hermetic, root-owned (accepted) lifecycle config: global=force, per-app=graceful
# so a correct per-app resolution is unambiguously distinguishable from global.
CONF=/tmp/s49-lifecycle.conf
cat >"$CONF" <<EOF
SHUTDOWN_METHOD=force
SHUTDOWN_GRACE_SECS=20
IDLE_SHUTDOWN_SECS=0
LOWMEM_MB=0

[app:$POLICY_KEY]
SHUTDOWN_METHOD=graceful
EOF
chmod 600 "$CONF"

SPAWN_LOG=/tmp/s49-spawn.log
: >"$SPAWN_LOG"

# 512 MiB nested guest (CI accommodation, same as s45/s47/s48). Scrub any
# inherited lifecycle env vars: env has HIGHER precedence than the config file,
# so an ambient TIER5_SHUTDOWN_METHOD (etc.) would mask the per-app resolution
# this test is asserting and false-fail it.
env -u TIER5_SHUTDOWN_METHOD -u TIER5_SHUTDOWN_GRACE_SECS \
    -u TIER5_IDLE_SHUTDOWN_SECS -u TIER5_LOWMEM_MB -u TIER5_POLICY_KEY \
    TIER5_MEM_KIB=524288 TIER5_LIFECYCLE_CONF="$CONF" \
    bash "$TIER5_DIR/spawn-tier5.sh" --vm "$VM_NAME" --policy-key "$POLICY_KEY" \
    -- weston-terminal >"$SPAWN_LOG" 2>&1 &
SPAWN_PID=$!

# Wait until the domain is running, the overlay exists, AND the wrapper has
# emitted its resolved lifecycle line (printed once the guest is up).
deadline=$(( $(date +%s) + 120 ))
READY=0
while [ "$(date +%s)" -lt "$deadline" ]; do
    if virsh_admin domstate "$VM_NAME" 2>/dev/null | grep -qw running \
       && [ -f "$OVERLAY" ] \
       && grep -q "lifecycle\[$POLICY_KEY\]:" "$SPAWN_LOG" 2>/dev/null; then
        READY=1; break
    fi
    sleep 0.5
done
if [ "$READY" != "1" ]; then
    cat "$SPAWN_LOG" >&2 || true
    kill -KILL "$SPAWN_PID" 2>/dev/null || true
    virsh_admin destroy "$VM_NAME" >/dev/null 2>&1 || true
    virsh_admin undefine "$VM_NAME" >/dev/null 2>&1 || true
    rm -f "$OVERLAY" "$CONF"
    fail "domain never reached running + lifecycle-line state within 120s"
    echo "[s49] $PASSCOUNT passes, $FAILCOUNT failures"
    exit 1
fi
pass "domain $VM_NAME running and overlay exists"

# --- 1. per-app policy resolution: per-app `graceful` beat global `force` ---
LIFELINE=$(grep "lifecycle\[$POLICY_KEY\]:" "$SPAWN_LOG" | tail -1)
echo "[s49] resolved: $LIFELINE"
if printf '%s' "$LIFELINE" | grep -q 'method=graceful'; then
    pass "per-app policy resolved (--policy-key $POLICY_KEY -> method=graceful, overriding global force)"
else
    fail "per-app policy did NOT resolve to graceful: $LIFELINE"
fi

# --- 2. graceful teardown on app-exit ---
# Make the published app exit inside the NESTED guest via its guest agent. The
# wrapper's wait loop then detects the exit and runs the EXIT-trap graceful reap.
virsh_admin qemu-agent-command --timeout 5 "$VM_NAME" \
    '{"execute":"guest-exec","arguments":{"path":"/usr/bin/pkill","arg":["-f","weston-terminal"]}}' \
    >/dev/null 2>&1 || true

# Budget: app-exit detection (poll) + graceful shutdown (ACPI grace -> qga agent)
# + undefine + unlink. Generous for a 512 MiB nested guest under CI.
deadline=$(( $(date +%s) + 60 ))
GRACEFUL_SEEN=0; DOMAIN_GONE=0; OVERLAY_GONE=0
while [ "$(date +%s)" -lt "$deadline" ]; do
    grep -q 'graceful shutdown (ACPI)' "$SPAWN_LOG" 2>/dev/null && GRACEFUL_SEEN=1
    virsh_admin dominfo "$VM_NAME" >/dev/null 2>&1 || DOMAIN_GONE=1
    [ -f "$OVERLAY" ] || OVERLAY_GONE=1
    [ "$DOMAIN_GONE" = "1" ] && [ "$OVERLAY_GONE" = "1" ] && break
    sleep 1
done
wait "$SPAWN_PID" 2>/dev/null || true

[ "$GRACEFUL_SEEN" = "1" ] \
    && pass "wrapper attempted graceful shutdown (logged 'graceful shutdown (ACPI)')" \
    || fail "no graceful-shutdown log line — reap did not take the graceful path"
[ "$DOMAIN_GONE" = "1" ] \
    && pass "libvirt domain $VM_NAME reclaimed after graceful reap" \
    || fail "domain $VM_NAME still defined after graceful reap (orphan domain leak)"
[ "$OVERLAY_GONE" = "1" ] \
    && pass "per-VM overlay unlinked after graceful reap" \
    || fail "overlay $OVERLAY still present after graceful reap (orphan disk leak)"

# Defensive cleanup.
kill -TERM "$SPAWN_PID" 2>/dev/null || true
virsh_admin destroy "$VM_NAME" >/dev/null 2>&1 || true
virsh_admin undefine "$VM_NAME" >/dev/null 2>&1 || true
rm -f "$OVERLAY" "$SPAWN_LOG" "$CONF"

if [ "$FAILCOUNT" -eq 0 ]; then
    pass "§Phase-7 tier-5 per-app graceful shutdown resolves + reclaims all resources"
    echo "[s49] $PASSCOUNT passes, 0 failures"
    exit 0
else
    echo "[s49] $PASSCOUNT passes, $FAILCOUNT failures"
    exit 1
fi
