#!/bin/bash
# spec/20 Phase-9 §step 2 — define + start the qdistro-print libvirt
# domain on the host.
#
# Usage:
#   install-print-vm.sh                 # define + autostart, no-op if already
#   install-print-vm.sh --force         # redefine even if existing
#   install-print-vm.sh --start         # define + start now
#   install-print-vm.sh --remove        # destroy + undefine
#   install-print-vm.sh --diag --force  # add VNC-on-loopback for boot-smoke debug
#
# Pre-reqs:
#   - /var/lib/libvirt/images/qdistro-print-base.qcow2 (build via
#     build-print-image.sh).
#   - libvirtd running, current user allowed on qemu:///system OR
#     QDISTRO_PRINT_LIBVIRT_URI overridden (e.g. qemu:///session).
#
# Idempotent. Default values:
#   QDISTRO_PRINT_VM_NAME       qdistro-print
#   QDISTRO_PRINT_VM_CID        4 (matches the proxy default)
#   QDISTRO_PRINT_VM_MEM_KIB    524288 (512 MiB)
#   QDISTRO_PRINT_LIBVIRT_URI   qemu:///system
#
# Symbols referenced from the proxy / phase8 bats:
#   QDISTRO_PRINT_BACKEND=vsock + QDISTRO_PRINT_VSOCK_CID=4 → connect.
set -euo pipefail

VM_NAME="${QDISTRO_PRINT_VM_NAME:-qdistro-print}"
VM_CID="${QDISTRO_PRINT_VM_CID:-4}"
VM_MEM_KIB="${QDISTRO_PRINT_VM_MEM_KIB:-524288}"
LIBVIRT_URI="${QDISTRO_PRINT_LIBVIRT_URI:-qemu:///system}"
BASE_DISK="${QDISTRO_PRINT_BASE_DISK:-/var/lib/libvirt/images/qdistro-print-base.qcow2}"

FORCE=0
START=0
REMOVE=0
DIAG=0
while [ $# -gt 0 ]; do
    case "$1" in
        --force)  FORCE=1; shift;;
        --start)  START=1; shift;;
        --remove) REMOVE=1; shift;;
        --diag)   DIAG=1; shift;;
        -h|--help)
            sed -n '2,28p' "$0"; exit 0;;
        *) echo "unknown arg: $1" >&2; exit 2;;
    esac
done

if ! command -v virsh >/dev/null 2>&1; then
    echo "[install-print-vm] virsh not installed" >&2
    exit 1
fi

if [ "$REMOVE" = 1 ]; then
    if virsh -c "$LIBVIRT_URI" list --state-running --name 2>/dev/null \
            | grep -Fxq "$VM_NAME"; then
        virsh -c "$LIBVIRT_URI" destroy "$VM_NAME" >/dev/null 2>&1 || true
    fi
    if virsh -c "$LIBVIRT_URI" list --all --name 2>/dev/null \
            | grep -Fxq "$VM_NAME"; then
        virsh -c "$LIBVIRT_URI" undefine "$VM_NAME" >/dev/null
        echo "[install-print-vm] undefined '$VM_NAME'"
    else
        echo "[install-print-vm] '$VM_NAME' not defined"
    fi
    exit 0
fi

EXISTS=0
if virsh -c "$LIBVIRT_URI" list --all --name 2>/dev/null \
        | grep -Fxq "$VM_NAME"; then
    EXISTS=1
fi

if [ "$EXISTS" = 1 ] && [ "$FORCE" != 1 ]; then
    echo "[install-print-vm] '$VM_NAME' already defined"
else
    if [ ! -f "$BASE_DISK" ]; then
        echo "[install-print-vm] base disk missing: $BASE_DISK" >&2
        echo "[install-print-vm] run build-print-image.sh first" >&2
        exit 3
    fi
    if [ "$EXISTS" = 1 ] && [ "$FORCE" = 1 ]; then
        if virsh -c "$LIBVIRT_URI" list --state-running --name 2>/dev/null \
                | grep -Fxq "$VM_NAME"; then
            virsh -c "$LIBVIRT_URI" destroy "$VM_NAME" >/dev/null 2>&1 || true
        fi
        virsh -c "$LIBVIRT_URI" undefine "$VM_NAME" >/dev/null
    fi
    # Create a per-VM linked-clone qcow2 backed by the base disk so
    # admin-side rebuilds don't need to copy the whole image. The
    # clone disk lives next to the base.
    CLONE_DISK="$(dirname "$BASE_DISK")/${VM_NAME}.qcow2"
    if [ ! -f "$CLONE_DISK" ] || [ "$FORCE" = 1 ]; then
        rm -f "$CLONE_DISK"
        qemu-img create -f qcow2 -F qcow2 -b "$BASE_DISK" "$CLONE_DISK" \
            >/dev/null
        chmod 0644 "$CLONE_DISK"
    fi
    # Random MAC (locally administered, unicast).
    HEX=$(od -An -N3 -tx1 /dev/urandom | tr -d ' \n')
    MAC="52:54:00:${HEX:0:2}:${HEX:2:2}:${HEX:4:2}"
    TMPL="$(dirname "$0")/domain-template.xml"
    XML=$(mktemp /tmp/qdistro-print-XXXXXX.xml)
    sed -e "s|__VM_NAME__|$VM_NAME|" \
        -e "s|__MAC__|$MAC|" \
        -e "s|__MEM_KIB__|$VM_MEM_KIB|" \
        -e "s|__CID__|$VM_CID|" \
        -e "s|__DISK_PATH__|$CLONE_DISK|" \
        "$TMPL" >"$XML"
    if [ "$DIAG" = 1 ]; then
        # --diag inserts a VNC-on-loopback graphics device so an admin
        # can attach a viewer (`virsh -c qemu:///session vncdisplay $VM`
        # → `vncviewer 127.0.0.1:N`) and watch the boot when the
        # file-redirected serial log is silent. Headless boot is the
        # production path; this is for boot-smoke debugging only.
        sed -i "s|</devices>|  <graphics type='vnc' port='-1' autoport='yes' listen='127.0.0.1'/>\n    <video><model type='virtio' heads='1'/></video>\n  </devices>|" "$XML"
    fi
    virsh -c "$LIBVIRT_URI" define "$XML" >/dev/null
    rm -f "$XML"
    echo "[install-print-vm] defined '$VM_NAME' (cid=$VM_CID, disk=$CLONE_DISK)"
fi

# Autostart so the VM reboots with the host. Idempotent.
virsh -c "$LIBVIRT_URI" autostart "$VM_NAME" >/dev/null 2>&1 || true

if [ "$START" = 1 ]; then
    if ! virsh -c "$LIBVIRT_URI" list --state-running --name 2>/dev/null \
            | grep -Fxq "$VM_NAME"; then
        virsh -c "$LIBVIRT_URI" start "$VM_NAME" >/dev/null
        echo "[install-print-vm] started '$VM_NAME'"
    else
        echo "[install-print-vm] '$VM_NAME' already running"
    fi
fi

echo "[install-print-vm] OK"
