#!/bin/bash
# generate-protocol.sh — regenerate qdlocker/qdlocker/protocol/qdwin_locker_v1/
# from qdwin's canonical XML.
#
# qdwin owns the qdwin_locker_v1 protocol; it installs the XML to
# $datadir/qdistro/protocols/qdwin-locker-v1.xml and emits
# `qdistro-protocols.pc`. This script discovers the XML via
# pkg-config; for an uninstalled-qdwin dev tree, override with
# QDWIN_LOCKER_XML=/path/to/qdwin-locker-v1.xml or set
# QDISTRO_PROTOCOLS_DIR=/path/to/share/qdistro/protocols.
#
# Output is committed under qdlocker/qdlocker/protocol/ so a runtime
# install doesn't need pywayland-scanner.
#
# Requires: pywayland >= 0.4.18 (python3 -m pywayland.scanner).

set -euo pipefail

REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

WAYLAND_XML="${WAYLAND_XML:-/usr/share/wayland/wayland.xml}"

if [ -z "${QDWIN_LOCKER_XML:-}" ]; then
    if [ -n "${QDISTRO_PROTOCOLS_DIR:-}" ]; then
        QDWIN_LOCKER_XML="$QDISTRO_PROTOCOLS_DIR/qdwin-locker-v1.xml"
    elif command -v pkg-config >/dev/null && pkg-config --exists qdistro-protocols; then
        QDWIN_LOCKER_XML="$(pkg-config --variable=pkgdatadir qdistro-protocols)/qdwin-locker-v1.xml"
    elif [ -f "$REPO/../qdwin/qdwin/qdwin-locker-v1.xml" ]; then
        # Sibling-checkout fallback for dev trees without an installed qdwin.
        QDWIN_LOCKER_XML="$REPO/../qdwin/qdwin/qdwin-locker-v1.xml"
    fi
fi

if ! python3 -c "import pywayland" >/dev/null 2>&1; then
    echo "generate-protocol: pywayland not installed (pip install pywayland)" >&2
    exit 2
fi
if [ ! -f "$WAYLAND_XML" ]; then
    echo "generate-protocol: wayland.xml not found at $WAYLAND_XML" >&2
    echo "  set WAYLAND_XML=/path/to/wayland.xml (usually /usr/share/wayland/)" >&2
    exit 3
fi
if [ -z "${QDWIN_LOCKER_XML:-}" ] || [ ! -f "$QDWIN_LOCKER_XML" ]; then
    echo "generate-protocol: qdwin-locker-v1.xml not found" >&2
    echo "  set QDWIN_LOCKER_XML=/path/to/qdwin-locker-v1.xml," >&2
    echo "  or install qdwin (provides qdistro-protocols.pc)," >&2
    echo "  or set QDISTRO_PROTOCOLS_DIR=/path/to/share/qdistro/protocols" >&2
    exit 3
fi

echo "generate-protocol: using $QDWIN_LOCKER_XML"

OUT="qdlocker/protocol"
mkdir -p "$OUT"
rm -rf "$OUT/qdwin_locker_v1" "$OUT/wayland"
python3 -m pywayland.scanner \
    -i "$WAYLAND_XML" "$QDWIN_LOCKER_XML" \
    -o "$OUT/"

# pywayland.scanner emits the wayland core protocol too; drop it
# (pywayland ships pywayland.protocol.wayland out of the box).
rm -rf "$OUT/wayland"

# Rewrite EVERY generated file's `from ..wayland import` line to
# point at pywayland's installed copy, so we don't have to vendor
# wayland.xml output. The scanner emits one .py per interface, so
# both qdwin_locker_v1.py and qdwin_locker_surface_v1.py need the
# patch — earlier drafts only fixed the first file and the second
# silently broke at import time.
sed -i 's|^from \.\.wayland import|from pywayland.protocol.wayland import|' \
    "$OUT/qdwin_locker_v1/"*.py

echo "generate-protocol: wrote"
find "$OUT/qdwin_locker_v1" -type f | sed 's/^/  /'
