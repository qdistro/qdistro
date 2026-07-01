#!/bin/bash
# spec/20 Phase-9 §step 2 — detach a USB device from the qdistro-print VM.
#
# Polkit action: org.qdistro.print.detach-usb (auth_admin defaults).
set -euo pipefail

VM_NAME="qdistro-print"
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
            case "$spec" in *:*) ;; *) echo "malformed USB id (want vvvv:pppp): $spec" >&2; exit 2;; esac
            VID="${spec%%:*}"
            PID="${spec##*:}"
            shift
            ;;
        --bus-addr)
            shift
            spec="${1:?--bus-addr requires BUS.ADDR}"
            case "$spec" in *.*.*|.*|*.|*[!0-9.]*|"") echo "malformed bus address (want BUS.ADDR): $spec" >&2; exit 2;; *.*) ;; *) echo "malformed bus address (want BUS.ADDR): $spec" >&2; exit 2;; esac
            BUS="${spec%%.*}"
            ADDR="${spec##*.}"
            shift
            ;;
        --vm) echo "--vm is not supported; USB detach is pinned to qdistro-print" >&2; exit 2;;
        -h|--help)
            cat <<'USAGE'
qdistro-print-detach-usb — detach a USB device from the qdistro-print VM.

Usage:
  qdistro-print-detach-usb --vendor-product VVVV:PPPP
  qdistro-print-detach-usb --bus-addr BUS.ADDR

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
if [ -n "$VID" ]; then
    case "$VID:$PID" in
        [0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F]:[0-9a-fA-F][0-9a-fA-F][0-9a-fA-F][0-9a-fA-F]) ;;
        *) echo "[detach-usb] malformed USB id (want vvvv:pppp): $VID:$PID" >&2; exit 2;;
    esac
fi
if [ -n "$BUS" ]; then
    case "$BUS.$ADDR" in
        *[!0-9.]*|.*|*.) echo "[detach-usb] malformed bus address (want BUS.ADDR): $BUS.$ADDR" >&2; exit 2;;
    esac
fi
if ! command -v virsh >/dev/null 2>&1; then
    echo "[detach-usb] virsh not installed" >&2
    exit 1
fi
if ! command -v pkcheck >/dev/null 2>&1; then
    echo "[detach-usb] pkcheck (polkit) not installed; refusing USB detach" >&2
    exit 5
fi
if ! pkcheck --action-id "$ACTION" \
             --process "$$" \
             --allow-user-interaction; then
    echo "[detach-usb] polkit denied $ACTION" >&2
    exit 5
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
