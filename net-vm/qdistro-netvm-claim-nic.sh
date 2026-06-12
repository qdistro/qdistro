#!/bin/bash
# task 4 piece 5 — NetworkManager → net-VM NIC handoff and recovery.
#
# The migration window (doc/networking.md §"Escape hatches"): NetworkManager
# releases the real NIC, the net VM claims it (PCI passthrough or USB redirect),
# and ONE host-claimable recovery ethernet stays available for when the net VM
# is wedged. Ownership of the recovery path is strict: normally down, admin-only,
# never forwarded to a silo, audited on enable, disabled again when recovery ends
# — a recovery path silos can route through is a bypass, not an escape hatch.
#
# Every claim is checked against netvm-manifest.json's allowlist: a NIC not on
# pciPassthrough / usbHostdevAllow is REFUSED (the hostile-hardware boundary;
# the manifest is the single source of truth, same guard the activation gate
# uses). All actions are audited.
#
# Usage:
#   qdistro-netvm-claim-nic.sh claim-pci  <DDDD:BB:SS.F>
#   qdistro-netvm-claim-nic.sh claim-usb  <vendor>:<product>
#   qdistro-netvm-claim-nic.sh release-pci <DDDD:BB:SS.F>   # back to host/NM
#   qdistro-netvm-claim-nic.sh recover-up   <ifname>        # enable hatch (audited)
#   qdistro-netvm-claim-nic.sh recover-down <ifname>        # disable hatch
#
# Needs root + a real NIC; runs on the host, not in any VM.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
VM_NAME="${QDISTRO_NETVM_VM_NAME:-qdistro-netvm}"
LIBVIRT_URI="${QDISTRO_NETVM_LIBVIRT_URI:-qemu:///system}"
MANIFEST="${QDISTRO_NETVM_MANIFEST:-$HERE/netvm-manifest.json}"
AUDIT="${QDISTRO_NETVM_AUDIT:-/var/lib/qdistro/audit/netvm_nic.log}"

audit() {  # audit <verb> <target> <result>
    install -d -m 0750 "$(dirname "$AUDIT")" 2>/dev/null || true
    printf '%s\t%s\t%s\t%s\tuid=%s\n' \
        "$(date -u +%FT%TZ)" "$1" "$2" "$3" "$(id -u)" >>"$AUDIT" 2>/dev/null || true
}

die() { echo "[netvm-nic] $*" >&2; audit "${ACTION:-?}" "${TARGET:-?}" "error:$*"; exit 1; }

# All allowlist checks pass untrusted args to Python via ARGV (sys.argv), never
# by interpolating them into the heredoc source — a malicious TARGET/vendor/
# product must not become Python code before the allowlist even runs (codex
# finding 6). The case branches additionally regex-validate the args first.
PY_USB='import json,sys
from qdistro_netvm_manifest import parse, validate_usb_attach
m=parse(json.load(open(sys.argv[1])))
sys.exit(0 if validate_usb_attach(m, sys.argv[2], sys.argv[3]) else 1)'
PY_PCI='import json,sys
from qdistro_netvm_manifest import parse
m=parse(json.load(open(sys.argv[1])))
sys.exit(0 if sys.argv[2].lower() in m.pci_allow else 1)'

usb_allowed() {  # usb_allowed vendor product
    PYTHONPATH="$REPO/session_manager:$REPO/broker" \
        python3 -c "$PY_USB" "$MANIFEST" "$1" "$2"
}
pci_allowed() {  # pci_allowed addr
    PYTHONPATH="$REPO/session_manager:$REPO/broker" \
        python3 -c "$PY_PCI" "$MANIFEST" "$1"
}

valid_pci() { case "$1" in [0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F]:[0-9a-fA-F][0-9a-fA-F]:[0-9a-fA-F][0-9a-fA-F].[0-9a-fA-F]) return 0;; *) return 1;; esac; }
valid_usb() { case "$1" in [0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F]:[0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F]) return 0;; *) return 1;; esac; }
valid_ifname() { case "$1" in ""|*[!A-Za-z0-9_.-]*) return 1;; *) return 0;; esac; }

[ "$(id -u)" -eq 0 ] || die "must run as root"
command -v virsh >/dev/null || die "virsh not installed"

ACTION="${1:-}"; TARGET="${2:-}"
case "$ACTION" in
claim-pci)
    [ -n "$TARGET" ] || die "usage: claim-pci <DDDD:BB:SS.F>"
    valid_pci "$TARGET" || die "malformed PCI address: $TARGET"
    pci_allowed "$TARGET" || die "PCI $TARGET not in manifest pciPassthrough — refusing"
    # 1. NetworkManager releases the device (find its netdev by PCI addr).
    ifn="$(ls "/sys/bus/pci/devices/$TARGET/net" 2>/dev/null | head -n1 || true)"
    if [ -n "$ifn" ] && command -v nmcli >/dev/null; then
        nmcli device set "$ifn" managed no || true
        nmcli device disconnect "$ifn" 2>/dev/null || true
        audit claim-pci "$TARGET" "nm-released:$ifn"
    fi
    # 2. Rebind the device to vfio-pci so QEMU can pass it through.
    if [ -e "/sys/bus/pci/devices/$TARGET/driver" ]; then
        echo "$TARGET" >"/sys/bus/pci/devices/$TARGET/driver/unbind" 2>/dev/null || true
    fi
    vid="$(cat "/sys/bus/pci/devices/$TARGET/vendor")"; did="$(cat "/sys/bus/pci/devices/$TARGET/device")"
    modprobe vfio-pci 2>/dev/null || true
    echo "${vid#0x} ${did#0x}" >/sys/bus/pci/drivers/vfio-pci/new_id 2>/dev/null || true
    # 3. Attach to the running net VM as a managed PCI hostdev.
    dom="${TARGET%%:*}"; rest="${TARGET#*:}"; bus="${rest%%:*}"; sf="${rest#*:}"
    slot="${sf%%.*}"; func="${sf#*.}"
    xml="$(mktemp /tmp/netvm-pci-XXXXXX.xml)"
    cat >"$xml" <<EOF
<hostdev mode='subsystem' type='pci' managed='yes'>
  <source><address domain='0x$dom' bus='0x$bus' slot='0x$slot' function='0x$func'/></source>
</hostdev>
EOF
    virsh -c "$LIBVIRT_URI" attach-device "$VM_NAME" "$xml" --live || { rm -f "$xml"; die "attach-device failed"; }
    rm -f "$xml"
    audit claim-pci "$TARGET" ok
    echo "[netvm-nic] claimed PCI $TARGET for $VM_NAME"
    ;;
claim-usb)
    [ -n "$TARGET" ] || die "usage: claim-usb <vendor>:<product>"
    valid_usb "$TARGET" || die "malformed USB id (want vvvv:pppp): $TARGET"
    vendor="${TARGET%%:*}"; product="${TARGET#*:}"
    usb_allowed "$vendor" "$product" || die "USB $TARGET not in manifest usbHostdevAllow — refusing"
    xml="$(mktemp /tmp/netvm-usb-XXXXXX.xml)"
    cat >"$xml" <<EOF
<hostdev mode='subsystem' type='usb'>
  <source><vendor id='0x$vendor'/><product id='0x$product'/></source>
</hostdev>
EOF
    virsh -c "$LIBVIRT_URI" attach-device "$VM_NAME" "$xml" --live || { rm -f "$xml"; die "attach-device failed"; }
    rm -f "$xml"
    audit claim-usb "$TARGET" ok
    echo "[netvm-nic] claimed USB $TARGET for $VM_NAME"
    ;;
release-pci)
    [ -n "$TARGET" ] || die "usage: release-pci <DDDD:BB:SS.F>"
    valid_pci "$TARGET" || die "malformed PCI address: $TARGET"
    dom="${TARGET%%:*}"; rest="${TARGET#*:}"; bus="${rest%%:*}"; sf="${rest#*:}"
    slot="${sf%%.*}"; func="${sf#*.}"
    xml="$(mktemp /tmp/netvm-pci-XXXXXX.xml)"
    cat >"$xml" <<EOF
<hostdev mode='subsystem' type='pci' managed='yes'>
  <source><address domain='0x$dom' bus='0x$bus' slot='0x$slot' function='0x$func'/></source>
</hostdev>
EOF
    virsh -c "$LIBVIRT_URI" detach-device "$VM_NAME" "$xml" --live 2>/dev/null || true
    rm -f "$xml"
    # Hand back to the host: rebind to the real driver via PCI rescan.
    echo 1 >"/sys/bus/pci/devices/$TARGET/remove" 2>/dev/null || true
    echo 1 >/sys/bus/pci/rescan 2>/dev/null || true
    audit release-pci "$TARGET" ok
    echo "[netvm-nic] released PCI $TARGET back to host"
    ;;
recover-up)
    # Bring the recovery ethernet up on the HOST. Admin-only, audited, and
    # explicitly NOT routed to any silo netns/vif — it is a host management path
    # for when the net VM is wedged, nothing more.
    [ -n "$TARGET" ] || die "usage: recover-up <ifname>"
    valid_ifname "$TARGET" || die "invalid ifname: $TARGET"
    command -v nmcli >/dev/null && nmcli device set "$TARGET" managed yes || true
    ip link set "$TARGET" up || die "cannot bring $TARGET up"
    audit recover-up "$TARGET" "ENABLED — host recovery path active"
    echo "[netvm-nic] recovery ethernet $TARGET UP (remember: recover-down when done)"
    ;;
recover-down)
    [ -n "$TARGET" ] || die "usage: recover-down <ifname>"
    valid_ifname "$TARGET" || die "invalid ifname: $TARGET"
    ip link set "$TARGET" down || true
    command -v nmcli >/dev/null && nmcli device set "$TARGET" managed no || true
    audit recover-down "$TARGET" "disabled"
    echo "[netvm-nic] recovery ethernet $TARGET DOWN"
    ;;
*)
    sed -n '2,30p' "$0"; exit 2;;
esac
