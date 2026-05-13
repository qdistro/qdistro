#!/bin/bash
# install-browser-bridge-for-vm.sh — idempotent install of the
# qdistro browser-bridge native-messaging host (spec/14 Phase-8 MVP)
# onto a fresh-clone VM.
#
# Sources come from /root/browser-bridge-src/ (staged by
# fresh-vm-bootstrap.sh from host:8765/browser_bridge/).
#
# Layout:
#   /usr/libexec/qdistro/qdistro_browser_bridge.py    # host module
#   /usr/libexec/qdistro/qdistro_browser_install.py   # install module
#   /usr/lib/qdistro/browser-bridge                   # exec-stub
#   /usr/local/bin/qdistro-browser-install            # admin CLI
#   /usr/share/qdistro/browser-extension/             # WebExtension src
#
# The bridge "binary" at /usr/lib/qdistro/browser-bridge is a tiny
# bash exec stub. The browser launches it; it execs the python module
# under /usr/libexec/qdistro/. Two layers because:
#   1. spec/14 nails the bridge path at /usr/lib/qdistro/browser-bridge
#      (it's pinned in the per-browser native-messaging manifest),
#   2. the python module + its siblings live alongside the daemon's
#      libexec/ tree per the existing qdistro install layout.
set -euo pipefail

SRC=${1:-/root/browser-bridge-src}
if [ ! -d "$SRC" ]; then
    echo "[install-browser-bridge] missing source dir $SRC" >&2
    exit 2
fi

DEST_LIB_QDISTRO=/usr/libexec/qdistro
DEST_LIB_BIN=/usr/lib/qdistro
DEST_BIN=/usr/local/bin
DEST_SHARE=/usr/share/qdistro/browser-extension

install -d -m 0755 "$DEST_LIB_QDISTRO" "$DEST_LIB_BIN" "$DEST_BIN" "$DEST_SHARE"

# Module + install module.
install -m 0644 "$SRC/qdistro_browser_bridge.py"  "$DEST_LIB_QDISTRO/"
install -m 0644 "$SRC/qdistro_browser_install.py" "$DEST_LIB_QDISTRO/"

# Exec stub at the bridge-spec pinned path.
cat >"$DEST_LIB_BIN/browser-bridge" <<'STUB'
#!/bin/bash
# qdistro browser-bridge native-messaging host stub. Spawned by
# Firefox / Chromium with the per-browser native-messaging manifest's
# `path` field pointing here. We exec the python module directly;
# stdin/stdout are the 4-byte length-prefix channel the browser
# already opened for us.
exec /usr/bin/python3 /usr/libexec/qdistro/qdistro_browser_bridge.py "$@"
STUB
chmod 0755 "$DEST_LIB_BIN/browser-bridge"

# Admin CLI.
cat >"$DEST_BIN/qdistro-browser-install" <<'CLI'
#!/bin/bash
# qdistro-browser-install — front for qdistro_browser_install.py.
exec /usr/bin/python3 /usr/libexec/qdistro/qdistro_browser_install.py "$@"
CLI
chmod 0755 "$DEST_BIN/qdistro-browser-install"

# WebExtension source tree (admin can pack via build-extension.sh
# inside this dir; per-user installs are out of scope for this
# script — they need browser-side "Load Temporary Add-on" or AMO sign).
if [ -d "$SRC/extension" ]; then
    cp -r "$SRC/extension/." "$DEST_SHARE/"
    [ -f "$DEST_SHARE/build-extension.sh" ] && \
        chmod 0755 "$DEST_SHARE/build-extension.sh"
fi

echo "[install-browser-bridge] OK"
