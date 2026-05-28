#!/bin/bash
# Install the tier-4 (per-app VM, waypipe-over-AF_VSOCK with host-side
# control wrapper) host-side control modules into a qdistro VM.
# Idempotent.
#
# tier4-vm/spawn-tier4.sh resolves its host-side control script from the
# source tree first ($SCRIPT_DIR/tier4_control.py) and FALLS BACK to
# /usr/share/qdistro/tier4-vm/tier4_control.py for production/installed
# images (see spawn-tier4.sh ~lines 863-866). Nothing populated that
# fallback path before this installer, so the installed image could
# never satisfy it. This installer lands the host-side python modules
# there so the documented fallback works.
#
# Lands:
#   - /usr/share/qdistro/tier4-vm/tier4_control.py — the control wrapper
#     spawn-tier4.sh falls back to.
#   - /usr/share/qdistro/tier4-vm/tier4_chrome.py — sibling module
#     tier4_control.py imports at runtime (close_vm / orphan reap), via
#     importlib spec_from_file_location(__file__).with_name(...). The
#     installed copy is non-functional without it, so it ships too.
#
# This installer deliberately does NOT create a /usr/local/bin launcher
# wrapper or a polkit policy: spawn-tier4.sh's only /usr/share/qdistro/
# tier4-vm/ reference is the tier4_control.py fallback above, and no
# tier4 spawn-wrapper convention exists in the tree (unlike tier3/tier5).
# The guest image/domain-template path /usr/share/qdistro/tier4-vm-guest/
# is handled elsewhere and is intentionally left alone.
#
# Usage:
#   bash install-tier4-host-for-vm.sh <qdistro-src-root>
#
# Where <qdistro-src-root> is the directory containing tier4-vm/.
# Typically /root/qdistro-src/qdistro in the bats VM (per
# fresh-vm-bootstrap.sh's $SRC).
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "[install-tier4] must run as root" >&2
    exit 2
fi

SRC_ROOT="${1:?usage: $0 <qdistro-src-root>}"
TIER4_DIR="$SRC_ROOT/tier4-vm"

if [ ! -d "$TIER4_DIR" ]; then
    echo "[install-tier4] FAIL: $TIER4_DIR not found" >&2
    exit 3
fi

# --- host-side control modules ---
# tier4_control.py is the fallback spawn-tier4.sh resolves; tier4_chrome.py
# is its runtime sibling import. Both ship as 0644 — they are imported,
# not exec'd directly (spawn-tier4.sh invokes them through python).
DEST_DIR=/usr/share/qdistro/tier4-vm
install -d "$DEST_DIR"
for mod in tier4_control.py tier4_chrome.py; do
    src="$TIER4_DIR/$mod"
    if [ ! -f "$src" ]; then
        echo "[install-tier4] FAIL: $src not found" >&2
        exit 3
    fi
    install -m 0644 "$src" "$DEST_DIR/$mod"
    echo "[install-tier4] installed $DEST_DIR/$mod ← $src"
done

echo "[install-tier4] done."
