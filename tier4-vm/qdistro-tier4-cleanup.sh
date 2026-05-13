#!/bin/bash
# Tier-4 cleanup: destroy + undefine a libvirt domain spawned by
# qdistro-tier4-spawn. Idempotent.
#
# Usage:
#   qdistro-tier4-cleanup.sh <vm_name>
set -uo pipefail

VM_NAME="${1:?usage: $0 <vm_name>}"
ADMIN_USER="${TIER4_ADMIN_USER:-admin}"

if [ "$(id -u)" -ne 0 ]; then
    echo "[tier4-cleanup] FAIL: must run as root" >&2
    exit 2
fi

ADMIN_UID=$(id -u "$ADMIN_USER")
ADMIN_RUNTIME="/run/user/$ADMIN_UID"
ADMIN_HOME="$(getent passwd "$ADMIN_USER" | cut -d: -f6)"
LIBVIRT_URI="qemu:///session"

run_as_admin() {
    runuser -u "$ADMIN_USER" -- env \
        XDG_RUNTIME_DIR="$ADMIN_RUNTIME" \
        HOME="$ADMIN_HOME" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=$ADMIN_RUNTIME/bus" \
        LIBVIRT_DEFAULT_URI="$LIBVIRT_URI" \
        "$@"
}

if run_as_admin virsh dominfo "$VM_NAME" >/dev/null 2>&1; then
    run_as_admin virsh destroy "$VM_NAME" >/dev/null 2>&1 || true
    run_as_admin virsh undefine "$VM_NAME" >/dev/null 2>&1 || true
    echo "[tier4-cleanup] $VM_NAME destroyed + undefined"
else
    echo "[tier4-cleanup] $VM_NAME not present (already cleaned)"
fi
