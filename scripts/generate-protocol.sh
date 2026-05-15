#!/bin/bash
# generate-protocol.sh — regenerate qdlocker/qdlocker/protocol/qdwin_locker_v1/
# from the vendored XML at protocol/qdwin-locker-v1.xml.
#
# Run after editing the XML or after pulling a new qdlocker tree
# without the generated files. Output is committed under
# qdlocker/qdlocker/protocol/ so a runtime install doesn't need
# pywayland-scanner.
#
# Requires: pywayland >= 0.4.18 (python3 -m pywayland.scanner).

set -euo pipefail

REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

WAYLAND_XML="${WAYLAND_XML:-/usr/share/wayland/wayland.xml}"

if ! python3 -c "import pywayland" >/dev/null 2>&1; then
    echo "generate-protocol: pywayland not installed (pip install pywayland)" >&2
    exit 2
fi
if [ ! -f "$WAYLAND_XML" ]; then
    echo "generate-protocol: wayland.xml not found at $WAYLAND_XML" >&2
    echo "  set WAYLAND_XML=/path/to/wayland.xml (usually /usr/share/wayland/)" >&2
    exit 3
fi

OUT="qdlocker/protocol"
rm -rf "$OUT/qdwin_locker_v1" "$OUT/wayland"
python3 -m pywayland.scanner \
    -i "$WAYLAND_XML" protocol/qdwin-locker-v1.xml \
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
