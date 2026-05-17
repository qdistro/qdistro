#!/bin/bash
# spec/20 Phase-9 §step 2 — detach a USB device from the qdistro-print VM.
#
# Polkit action: org.qdistro.print.detach-usb (auth_admin defaults).
set -euo pipefail

VM_NAME="${QDISTRO_PRINT_VM_NAME:-qdistro-print}"
LIBVIRT_URI="${QDISTRO_PRINT_LIBVIRT_URI:-qemu:///system}"
ACTION="org.qdistro.print.detach-usb"

VID=""
PID=""
BUS=""
ADDR=""
while [ $# -gt 0 ]; do
    case "$1" in
        --vendor-product)
            shift
            spec="${1:?--vendor-product requires VVVV:PPPP}"
            VID="${spec%%:*}"
            PID="${spec##*:}"
            shift
            ;;
        --bus-addr)
            shift
            spec="${1:?--bus-addr requires BUS.ADDR}"
            BUS="${spec%%.*}"
            ADDR="${spec##*.}"
            shift
            ;;
        --vm) shift; VM_NAME="$1"; shift;;
        -h|--help)
            cat <<'USAGE'
qdistro-print-detach-usb — detach a USB device from the qdistro-print VM.

Usage:
  qdistro-print-detach-usb --vendor-product VVVV:PPPP [--vm <name>]
  qdistro-print-detach-usb --bus-addr BUS.ADDR        [--vm <name>]

Polkit action: org.qdistro.print.detach-usb (auth_admin defaults).
USAGE
            exit 0;;
        *) echo "unknown arg: $1" >&2; exit 2;;
    esac
done

if [ -z "$VID$PID$BUS$ADDR" ]; then
    echo "usage: $0 --vendor-product VVVV:PPPP | --bus-addr BUS.ADDR" >&2
    exit 2
fi
if ! command -v virsh >/dev/null 2>&1; then
    echo "[detach-usb] virsh not installed" >&2
    exit 1
fi
if command -v pkcheck >/dev/null 2>&1 && [ -z "${QDISTRO_PRINT_USB_NO_POLKIT:-}" ]; then
    if ! pkcheck --action-id "$ACTION" \
                 --process "$$,$(date +%s%N)" \
                 --allow-user-interaction; then
        echo "[detach-usb] polkit denied $ACTION" >&2
        exit 5
    fi
fi

XML=$(mktemp /tmp/usb-detach-XXXXXX.xml)
trap 'rm -f "$XML"' EXIT
{
    echo "<hostdev mode='subsystem' type='usb' managed='yes'>"
    echo "  <source>"
    if [ -n "$VID" ]; then
        echo "    <vendor id='0x$VID'/>"
        echo "    <product id='0x$PID'/>"
    else
        echo "    <address bus='$BUS' device='$ADDR'/>"
    fi
    echo "  </source>"
    echo "</hostdev>"
} >"$XML"

if virsh -c "$LIBVIRT_URI" detach-device "$VM_NAME" "$XML" --live --persistent 2>&1; then
    echo "[detach-usb] detached USB device from '$VM_NAME'"
    exit 0
fi
echo "[detach-usb] virsh detach-device failed" >&2
exit 6
