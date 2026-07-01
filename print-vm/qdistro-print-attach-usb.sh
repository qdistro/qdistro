#!/bin/bash
# spec/20 Phase-9 §step 2 — attach a USB device to the qdistro-print VM.
#
# Polkit-gated wrapper around `virsh attach-device`. Two ways to
# specify the device:
#
#   --vendor-product XXXX:YYYY      hex vendor:product (must be allowlisted)
#   --bus-addr BUS.ADDR             integer bus + addr (lsusb -d)
#
# Polkit action: org.qdistro.print.attach-usb (auth_admin defaults).
#
# Detach via qdistro-print-detach-usb.
set -euo pipefail

VM_NAME="qdistro-print"
LIBVIRT_URI="${QDISTRO_PRINT_LIBVIRT_URI:-qemu:///system}"
ACTION="org.qdistro.print.attach-usb"
MANIFEST=""

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
        --vm) echo "--vm is not supported; USB attach is pinned to qdistro-print" >&2; exit 2;;
        -h|--help)
            cat <<'USAGE'
qdistro-print-attach-usb — attach a USB device to the qdistro-print VM.

Usage:
  qdistro-print-attach-usb --vendor-product VVVV:PPPP
  qdistro-print-attach-usb --bus-addr BUS.ADDR

Polkit action: org.qdistro.print.attach-usb (auth_admin defaults).
USB IDs must appear in /etc/qdistro/printvm-manifest.json usbHostdevAllow.
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
        *) echo "[attach-usb] malformed USB id (want vvvv:pppp): $VID:$PID" >&2; exit 2;;
    esac
fi
if [ -n "$BUS" ]; then
    case "$BUS.$ADDR" in
        *[!0-9.]*|.*|*.) echo "[attach-usb] malformed bus address (want BUS.ADDR): $BUS.$ADDR" >&2; exit 2;;
    esac
fi

if ! command -v virsh >/dev/null 2>&1; then
    echo "[attach-usb] virsh not installed" >&2
    exit 1
fi
if ! command -v pkcheck >/dev/null 2>&1; then
    echo "[attach-usb] pkcheck (polkit) not installed; refusing USB attach" >&2
    exit 5
fi
if ! pkcheck --action-id "$ACTION" \
             --process "$$" \
             --allow-user-interaction; then
    echo "[attach-usb] polkit denied $ACTION" >&2
    exit 5
fi

if [ -n "$BUS" ]; then
    if ! command -v lsusb >/dev/null 2>&1; then
        echo "[attach-usb] lsusb not installed; cannot resolve BUS.ADDR for allowlist" >&2
        exit 7
    fi
    usb_line="$(lsusb -s "$BUS:$ADDR" 2>/dev/null | head -n1 || true)"
    id="$(printf '%s\n' "$usb_line" | sed -n 's/.* ID \([0-9A-Fa-f]\{4\}:[0-9A-Fa-f]\{4\}\).*/\1/p' | head -n1)"
    if [ -z "$id" ]; then
        echo "[attach-usb] cannot resolve USB device at bus $BUS address $ADDR" >&2
        exit 7
    fi
    VID="${id%%:*}"
    PID="${id##*:}"
fi

for candidate in "$(dirname "$0")/printvm-manifest.json" \
                 /etc/qdistro/printvm-manifest.json \
                 /usr/share/qdistro/print-vm/printvm-manifest.json \
                 ; do
    if [ -f "$candidate" ]; then
        MANIFEST="$candidate"
        break
    fi
done
if [ -z "$MANIFEST" ]; then
    echo "[attach-usb] print USB manifest missing; refusing USB attach" >&2
    exit 7
fi
if ! command -v python3 >/dev/null 2>&1; then
    echo "[attach-usb] python3 not installed; cannot evaluate USB allowlist" >&2
    exit 7
fi
if ! python3 - "$MANIFEST" "$VID" "$PID" <<'PY'
import json
import sys

manifest, vendor, product = sys.argv[1], sys.argv[2].lower(), sys.argv[3].lower()
with open(manifest, encoding="utf-8") as f:
    obj = json.load(f)
allowed = obj.get("usbHostdevAllow", [])
for entry in allowed:
    if isinstance(entry, str):
        parts = entry.lower().split(":", 1)
        if len(parts) == 2 and parts == [vendor, product]:
            sys.exit(0)
    elif isinstance(entry, dict):
        ev = str(entry.get("vendor", "")).lower().removeprefix("0x")
        ep = str(entry.get("product", "")).lower().removeprefix("0x")
        if (ev, ep) == (vendor, product):
            sys.exit(0)
sys.exit(1)
PY
then
    echo "[attach-usb] USB $VID:$PID not in print VM usbHostdevAllow; refusing" >&2
    exit 7
fi

# Build the XML hostdev fragment.
XML=$(mktemp /tmp/usb-hostdev-XXXXXX.xml)
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

if ! virsh -c "$LIBVIRT_URI" list --state-running --name 2>/dev/null \
        | grep -Fxq "$VM_NAME"; then
    echo "[attach-usb] '$VM_NAME' is not running; start it first" >&2
    exit 4
fi

if virsh -c "$LIBVIRT_URI" attach-device "$VM_NAME" "$XML" --live --persistent 2>&1; then
    echo "[attach-usb] attached USB device to '$VM_NAME'"
    exit 0
fi
echo "[attach-usb] virsh attach-device failed" >&2
exit 6
