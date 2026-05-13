#!/bin/bash
# spawn-print-vm.sh — boot the qdistro-print VM on demand.
#
# Invoked by qdistro-print-proxy when the vsock backend is unreachable
# AND QDISTRO_PRINT_VM_SPAWN points here. Idempotent — if the domain
# is already running, exit 0 immediately.
#
# Phase-9 §step 1 ships this helper; the actual qdistro-print VM
# image is built in a follow-up task. Until then, this script behaves
# correctly as a no-op when the libvirt domain doesn't exist (returns
# rc=0 with a SKIP banner so the proxy doesn't loop forever; backend
# connect will then fail naturally and the app sees "no printers").
#
# Env:
#   QDISTRO_PRINT_VM_NAME       libvirt domain name (default: qdistro-print)
#   QDISTRO_PRINT_VM_TIMEOUT_S  poll timeout for boot (default: 25)
#
# Exit codes:
#   0  — VM is running (or domain absent → SKIP cleanly)
#   1  — virsh missing
#   2  — VM start failed
set -uo pipefail

VM=${QDISTRO_PRINT_VM_NAME:-qdistro-print}
TIMEOUT_S=${QDISTRO_PRINT_VM_TIMEOUT_S:-25}

if ! command -v virsh >/dev/null 2>&1; then
    echo "[spawn-print-vm] virsh not installed — SKIP" >&2
    exit 1
fi

# Domain absent? clean SKIP.
if ! virsh -c qemu:///system list --all --name 2>/dev/null | grep -Fxq "$VM"; then
    echo "[spawn-print-vm] domain '$VM' not defined — SKIP (Phase-9 §step 2 will ship the image)" >&2
    exit 0
fi

# Already running? done.
if virsh -c qemu:///system list --state-running --name 2>/dev/null \
        | grep -Fxq "$VM"; then
    echo "[spawn-print-vm] '$VM' already running"
    exit 0
fi

if ! virsh -c qemu:///system start "$VM" >/dev/null 2>&1; then
    echo "[spawn-print-vm] virsh start '$VM' failed" >&2
    exit 2
fi
echo "[spawn-print-vm] started '$VM'; waiting for boot ($TIMEOUT_S s)"

# Poll until the domain is "running" + qga / vsock comes up. Without a
# qga-driven probe we just sleep up to the timeout in 1s steps.
i=0
while [ "$i" -lt "$TIMEOUT_S" ]; do
    state=$(virsh -c qemu:///system domstate "$VM" 2>/dev/null || echo unknown)
    if [ "$state" = "running" ]; then
        # Best-effort: probe the vsock backend port to confirm cupsd
        # is up. Skipped if `socat` / `nc -U` aren't available.
        if command -v socat >/dev/null 2>&1; then
            if socat -u /dev/null VSOCK-CONNECT:3:631 >/dev/null 2>&1; then
                echo "[spawn-print-vm] vsock backend reachable on port 631"
                exit 0
            fi
        else
            # Trust the running state once we hit half the timeout.
            if [ "$i" -ge $((TIMEOUT_S / 2)) ]; then
                echo "[spawn-print-vm] '$VM' running (vsock probe skipped)"
                exit 0
            fi
        fi
    fi
    sleep 1
    i=$((i + 1))
done

echo "[spawn-print-vm] timed out waiting for '$VM' to be reachable" >&2
exit 0  # Don't fail the proxy connect — let it surface a backend error.
