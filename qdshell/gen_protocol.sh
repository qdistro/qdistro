#!/bin/bash
# Generate pywayland client bindings for qdwin_shell_v1.
#
# Output: compositor/qdshell/protocol/qdwin_shell_v1.py
#
# Uses `python3 -m pywayland.scanner`, not the /usr/bin/pywayland-scanner
# shebang — Tumbleweed's installed shebang imports a non-existent
# module (see memory pywayland_scanner_tumbleweed.md).
#
# Run once after every change to qdwin/qdwin-shell-v1.xml.

set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
# Default: the monorepo's qdwin/qdwin/qdwin-shell-v1.xml (in-tree layout).
# Override via QDWIN_PROTO_XML when running from an installed copy of
# qdshell that's been split off from the source tree.
protocol_xml="${QDWIN_PROTO_XML:-$here/../qdwin/qdwin/qdwin-shell-v1.xml}"
nested_xml="${QDWIN_NESTED_XML:-$(dirname "$protocol_xml")/qdwin-nested-v1.xml}"
wayland_xml="/usr/share/wayland/wayland.xml"
out="$here/protocol"

if [ ! -f "$protocol_xml" ]; then
    echo "gen_protocol: $protocol_xml not found (set QDWIN_PROTO_XML to override)" >&2
    exit 2
fi
if [ ! -f "$wayland_xml" ]; then
    echo "gen_protocol: wayland core protocol at $wayland_xml not found" >&2
    exit 3
fi

mkdir -p "$out"
# -i accepts a list; passing wayland.xml alongside the qdwin-private
# XMLs resolves wl_surface/wl_buffer imports in generated code.
# qdwin-nested-v1.xml (§6.8 S0) is additive and only present from
# 2026-04-25; treat absence as "not yet synced" rather than an error.
protocols_to_generate=("$protocol_xml")
if [ -f "$nested_xml" ]; then
    protocols_to_generate+=("$nested_xml")
fi
python3 -m pywayland.scanner --with-protocols \
    -i "$wayland_xml" "${protocols_to_generate[@]}" \
    -o "$out"

# __init__.py so `from protocol.qdwin_shell_v1 import …` works.
touch "$out/__init__.py"
echo "gen_protocol: bindings generated at $out"
ls -1 "$out" | sed 's/^/  /'
