#!/bin/bash
# Tier-5 cleanup: destroy + undefine a libvirt domain spawned by
# qdistro-tier5-spawn --vm, and remove its linked-clone disk.
# Idempotent.
#
# Usage:
#   qdistro-tier5-cleanup.sh <vm_name>
set -uo pipefail

VM_NAME="${1:?usage: $0 <vm_name>}"
ADMIN_USER="${TIER5_ADMIN_USER:-admin}"
ADMIN_HOME_DEFAULT="$(getent passwd "$ADMIN_USER" 2>/dev/null | cut -d: -f6)"
DISK_DIR="${TIER5_DISK_DIR:-$ADMIN_HOME_DEFAULT/.local/share/libvirt/images}"
DISK="$DISK_DIR/$VM_NAME.qcow2"

if [ "$(id -u)" -ne 0 ]; then
    echo "[tier5-cleanup] FAIL: must run as root" >&2
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

# qemu_pids_for_domain — same contract as spawn-tier5.sh: 0 pids live,
# 1 confirmed dead, 2 scan unreliable. domstate can report "shut off"
# for a LIVE qemu under nested virt, so unlinking the overlay is gated
# on a proven-dead scan, never on the destroy/undefine attempts above.
qemu_pids_for_domain() {
    local name="${1:-$VM_NAME}" p c out="" raw unread=""
    run_as_admin true >/dev/null 2>&1 || return 2
    raw=$(run_as_admin pgrep -f -- "-name guest=$name," 2>/dev/null)
    case $? in 0) ;; 1) return 1 ;; *) return 2 ;; esac
    for p in $raw; do
        # An unreadable /proc/<pid>/comm is ambiguous, never "dead".
        c=$(cat "/proc/$p/comm" 2>/dev/null) || { unread=1; continue; }
        case "$c" in qemu-system-*|qemu-kvm) out="$out $p" ;; esac
    done
    [ -n "$out" ] || { [ -n "$unread" ] && return 2; return 1; }
    printf '%s' "$out"
}

# Teardown itself is best-effort against our own domain; only the FINAL
# gate below (confirmed-absent definition + confirmed-dead qemu) may
# unlink the overlay. A dominfo failure is read precisely — "not found"
# is absence; any other error is ambiguity, never absence (sol r7).
if _derr=$(run_as_admin virsh dominfo "$VM_NAME" 2>&1 >/dev/null); then
    run_as_admin virsh destroy "$VM_NAME" >/dev/null 2>&1 || true
    # destroy can no-op under a lying domstate — signal leftover qemu
    # directly (mirrors reap_domain's fallback), then demand a
    # confirmed-dead scan before touching the overlay.
    _qp=$(qemu_pids_for_domain) || true
    if [ -n "$_qp" ]; then
        echo "[tier5-cleanup] destroy left qemu alive for $VM_NAME (pids:$_qp); killing" >&2
        run_as_admin kill $_qp 2>/dev/null || true
        for _ in 1 2 3 4 5 6; do
            sleep 0.5
            _qp=$(qemu_pids_for_domain) || true
            [ -n "$_qp" ] || break
        done
        [ -n "$_qp" ] && run_as_admin kill -9 $_qp 2>/dev/null || true
    fi
    if ! run_as_admin virsh undefine "$VM_NAME" >/dev/null 2>&1; then
        echo "[tier5-cleanup] WARN: undefine $VM_NAME failed" >&2
    fi
    echo "[tier5-cleanup] $VM_NAME destroyed + undefined"
elif printf '%s' "$_derr" \
        | grep -qiE 'not found|no domain|does not exist'; then
    echo "[tier5-cleanup] $VM_NAME not present"
else
    echo "[tier5-cleanup] WARN: cannot query $VM_NAME — libvirt error" >&2
fi

if [ -f "$DISK" ]; then
    # Both proofs are required before the overlay goes: NO startable
    # definition remains (a still-defined domain can be started against
    # a disk we are about to delete) AND no backing qemu lives.
    if _derr2=$(run_as_admin virsh dominfo "$VM_NAME" 2>&1 >/dev/null); then
        echo "[tier5-cleanup] FAIL: $VM_NAME still defined — keeping $DISK" >&2
        exit 5
    elif ! printf '%s' "$_derr2" \
            | grep -qiE 'not found|no domain|does not exist'; then
        echo "[tier5-cleanup] FAIL: cannot verify $VM_NAME is gone" >&2
        echo "        — keeping $DISK" >&2
        exit 5
    fi
    qemu_pids_for_domain; _lv=$?
    if [ "$_lv" -eq 1 ]; then
        rm -f "$DISK"
        echo "[tier5-cleanup] removed $DISK"
    else
        echo "[tier5-cleanup] FAIL: $VM_NAME qemu live or unverifiable" >&2
        echo "        — keeping $DISK" >&2
        exit 5
    fi
fi
