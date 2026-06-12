#!/bin/bash
# task 4 piece 2 — define + start the qdistro-netvm (OpenWrt) libvirt domain.
#
# Mirrors install-print-vm.sh, with one extra, load-bearing step: BEFORE
# `virsh define`, the rendered domain XML is run through the manifest
# activation guard (qdistro_netvm_manifest). If the domain exposes any device
# or service netvm-manifest.json does not declare — a stray passthrough NIC, a
# vsock channel, a graphics framebuffer — definition is REFUSED. The net VM
# holds the network attack surface, so an undeclared device on it is exactly
# the silent privilege the manifest exists to forbid (doc/vm-definitions.md
# §Runtime Policy; doc/networking.md §"VM definition exception").
#
# Usage:
#   install-netvm.sh                 # validate + define + autostart
#   install-netvm.sh --start         # + start now
#   install-netvm.sh --force         # redefine even if existing
#   install-netvm.sh --remove        # destroy + undefine
#   install-netvm.sh --check         # validate the rendered XML only, no define
#
# Pre-reqs:
#   - the OpenWrt base image (build-netvm-image.sh) at $QDISTRO_NETVM_BASE_DISK
#   - a host-only libvirt network for the mgmt vif ($QDISTRO_NETVM_MGMT_NET);
#     create with `virsh net-define`/`net-start` of a NAT-or-isolated network.
#   - libvirtd; current user on the chosen URI (default qemu:///system).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"

VM_NAME="${QDISTRO_NETVM_VM_NAME:-qdistro-netvm}"
VM_MEM_KIB="${QDISTRO_NETVM_MEM_KIB:-262144}"      # 256 MiB (Probe 2: ~48 used)
LIBVIRT_URI="${QDISTRO_NETVM_LIBVIRT_URI:-qemu:///system}"
BASE_DISK="${QDISTRO_NETVM_BASE_DISK:-/var/lib/libvirt/images/qdistro-netvm-base.qcow2}"
MGMT_NET="${QDISTRO_NETVM_MGMT_NET:-qdistro-netvm-mgmt}"
MANIFEST="${QDISTRO_NETVM_MANIFEST:-$HERE/netvm-manifest.json}"
TMPL="$HERE/domain-template.xml"

FORCE=0; START=0; REMOVE=0; CHECK=0
while [ $# -gt 0 ]; do
    case "$1" in
        --force)  FORCE=1; shift;;
        --start)  START=1; shift;;
        --remove) REMOVE=1; shift;;
        --check)  CHECK=1; shift;;
        -h|--help) sed -n '2,30p' "$0"; exit 0;;
        *) echo "unknown arg: $1" >&2; exit 2;;
    esac
done

virsh_() { virsh -c "$LIBVIRT_URI" "$@"; }

if [ "$REMOVE" = 1 ]; then
    if virsh_ list --state-running --name 2>/dev/null | grep -Fxq "$VM_NAME"; then
        virsh_ destroy "$VM_NAME" >/dev/null 2>&1 || true
    fi
    if virsh_ list --all --name 2>/dev/null | grep -Fxq "$VM_NAME"; then
        virsh_ undefine "$VM_NAME" >/dev/null
        echo "[install-netvm] undefined '$VM_NAME'"
    else
        echo "[install-netvm] '$VM_NAME' not defined"
    fi
    exit 0
fi

# Pull the declared MACs out of the manifest so the domain and the guard agree
# on what is allowed (single source of truth).
read_manifest() { python3 -c "import json,sys; print(json.load(open('$MANIFEST'))$1)"; }
MGMT_MAC="$(read_manifest "['controlPlane']['mgmtMac']")"
WAN_MAC="$(read_manifest "['interfaces']['wanMac']")"

render_xml() {
    local out="$1" disk="$2"
    sed -e "s|__VM_NAME__|$VM_NAME|" \
        -e "s|__MEM_KIB__|$VM_MEM_KIB|" \
        -e "s|__MGMT_MAC__|$MGMT_MAC|" \
        -e "s|__WAN_MAC__|$WAN_MAC|" \
        -e "s|__MGMT_NET__|$MGMT_NET|" \
        -e "s|__DISK_PATH__|$disk|" \
        "$TMPL" >"$out"
}

# The activation guard. Always runs before any define — including --check.
guard() {
    local xml="$1"
    if ! PYTHONPATH="$REPO/session_manager:$REPO/broker" \
            python3 -m qdistro_netvm_manifest "$MANIFEST" "$xml"; then
        echo "[install-netvm] REFUSING to define: domain failed the manifest guard" >&2
        return 1
    fi
}

if [ "$CHECK" = 1 ]; then
    XML="$(mktemp /tmp/qdistro-netvm-XXXXXX.xml)"
    trap 'rm -f "$XML"' EXIT
    render_xml "$XML" "${BASE_DISK%-base.qcow2}.qcow2"
    guard "$XML"
    echo "[install-netvm] --check OK"
    exit 0
fi

if ! command -v virsh >/dev/null 2>&1; then
    echo "[install-netvm] virsh not installed" >&2; exit 1
fi

EXISTS=0
if virsh_ list --all --name 2>/dev/null | grep -Fxq "$VM_NAME"; then EXISTS=1; fi

if [ "$EXISTS" = 1 ] && [ "$FORCE" != 1 ]; then
    echo "[install-netvm] '$VM_NAME' already defined"
else
    if [ ! -f "$BASE_DISK" ]; then
        echo "[install-netvm] base disk missing: $BASE_DISK" >&2
        echo "[install-netvm] run build-netvm-image.sh first" >&2
        exit 3
    fi
    if [ "$EXISTS" = 1 ] && [ "$FORCE" = 1 ]; then
        virsh_ list --state-running --name 2>/dev/null | grep -Fxq "$VM_NAME" \
            && virsh_ destroy "$VM_NAME" >/dev/null 2>&1 || true
        virsh_ undefine "$VM_NAME" >/dev/null
    fi
    CLONE_DISK="$(dirname "$BASE_DISK")/${VM_NAME}.qcow2"
    if [ ! -f "$CLONE_DISK" ] || [ "$FORCE" = 1 ]; then
        rm -f "$CLONE_DISK"
        qemu-img create -f qcow2 -F qcow2 -b "$BASE_DISK" "$CLONE_DISK" >/dev/null
        chmod 0644 "$CLONE_DISK"
    fi
    XML="$(mktemp /tmp/qdistro-netvm-XXXXXX.xml)"
    trap 'rm -f "$XML"' EXIT
    render_xml "$XML" "$CLONE_DISK"
    # FAIL CLOSED: no define unless the guard passes.
    guard "$XML"
    virsh_ define "$XML" >/dev/null
    echo "[install-netvm] defined '$VM_NAME' (mgmt=$MGMT_MAC disk=$CLONE_DISK)"
fi

virsh_ autostart "$VM_NAME" >/dev/null 2>&1 || true

if [ "$START" = 1 ]; then
    if ! virsh_ list --state-running --name 2>/dev/null | grep -Fxq "$VM_NAME"; then
        virsh_ start "$VM_NAME" >/dev/null
        echo "[install-netvm] started '$VM_NAME'"
    else
        echo "[install-netvm] '$VM_NAME' already running"
    fi
fi
echo "[install-netvm] OK"
