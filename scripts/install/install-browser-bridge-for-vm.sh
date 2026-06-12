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
#   /usr/libexec/qdistro/qdistro_*_daemon.py          # 9e session daemons
#   /usr/lib/qdistro/browser-bridge                   # exec-stub
#   /usr/local/bin/qdistro-browser-install            # admin CLI
#   /usr/share/qdistro/browser-extension/             # WebExtension src
#   /etc/systemd/user/qdistro-{downloads,mpris,...}.service
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
# $2: optional path to the outer ``qdbrowser/qdbrowser/`` python
# package. If present (or auto-located next to $SRC), its .py files
# are staged to /usr/local/lib/qdistro/qdbrowser/ so probes can
# ``from qdbrowser.pwd_autofill import ...`` (see s105 in
# tests/integration/vm/, search-path lines 432-434).
QDBROWSER_PKG="${2:-}"
BROWSER_DAEMONS_SRC="${3:-}"
if [ -z "$QDBROWSER_PKG" ]; then
    for cand in \
        "$SRC/../../qdbrowser/qdbrowser" \
        "$SRC/../qdbrowser/qdbrowser" \
        "/root/qdistro-src/qdbrowser/qdbrowser" \
        "/root/qdbrowser-src/qdbrowser"; do
        if [ -f "$cand/__init__.py" ]; then QDBROWSER_PKG="$cand"; break; fi
    done
fi
if [ -z "$BROWSER_DAEMONS_SRC" ]; then
    for cand in \
        "$SRC/../browser_daemons" \
        "$SRC/../../qdistro/browser_daemons" \
        "/root/qdistro-src/qdistro/browser_daemons"; do
        if [ -f "$cand/qdistro_downloads_daemon.py" ]; then
            BROWSER_DAEMONS_SRC="$cand"
            break
        fi
    done
fi
if [ ! -d "$SRC" ]; then
    echo "[install-browser-bridge] missing source dir $SRC" >&2
    exit 2
fi

DEST_LIB_QDISTRO=/usr/libexec/qdistro
DEST_LIB_BIN=/usr/lib/qdistro
DEST_BIN=/usr/local/bin
DEST_SHARE=/usr/share/qdistro/browser-extension
DEST_QDBROWSER=/usr/local/lib/qdistro/qdbrowser
DEST_USER_SYSD=/etc/systemd/user

install -d -m 0755 \
    "$DEST_LIB_QDISTRO" "$DEST_LIB_BIN" "$DEST_BIN" "$DEST_SHARE" \
    "$DEST_USER_SYSD"

# Module + install module. qdistro_browser_allowlist.py is the shared
# parent-exe allowlist + admin opt-in resolver imported by BOTH the bridge
# entry gate AND the 9e/pwd daemon identity gates (P0-4 follow-up), so it
# must land alongside them — the daemons import it defensively and fall back
# to the Firefox+Chromium baseline if it is missing.
install -m 0644 "$SRC/qdistro_browser_allowlist.py" "$DEST_LIB_QDISTRO/"
install -m 0644 "$SRC/qdistro_browser_bridge.py"  "$DEST_LIB_QDISTRO/"
install -m 0644 "$SRC/qdistro_browser_install.py" "$DEST_LIB_QDISTRO/"

# Phase-9e per-user desktop-integration daemons. The bridge forwards
# download/media/notification/compositor events to these well-known
# SESSION-bus services, so installing only the native-messaging host
# leaves every Phase-9e unit test and runtime feature with no bus owner.
if [ -z "$BROWSER_DAEMONS_SRC" ] || [ ! -d "$BROWSER_DAEMONS_SRC" ]; then
    echo "[install-browser-bridge] missing browser_daemons source dir" >&2
    exit 2
fi
install -m 0644 \
    "$BROWSER_DAEMONS_SRC/qdistro_browser_daemon_identity.py" \
    "$DEST_LIB_QDISTRO/"
for daemon in downloads mpris notifications compositor; do
    install -m 0755 \
        "$BROWSER_DAEMONS_SRC/qdistro_${daemon}_daemon.py" \
        "$DEST_LIB_QDISTRO/"
    install -m 0644 \
        "$BROWSER_DAEMONS_SRC/qdistro-${daemon}.service" \
        "$DEST_USER_SYSD/"
done
systemctl daemon-reload 2>/dev/null || true
systemctl --global enable \
    qdistro-downloads.service \
    qdistro-mpris.service \
    qdistro-notifications.service \
    qdistro-compositor.service >/dev/null 2>&1 || {
    echo "[install-browser-bridge] could not globally enable browser daemon user units" >&2
    exit 3
}
echo "[install-browser-bridge] installed Phase-9e browser daemons -> $DEST_LIB_QDISTRO"

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

# Stage the outer qdbrowser python package, so probes (and the bridge
# orchestrator) can ``import qdbrowser.pwd_autofill`` even when the
# qdbrowser pip-install path didn't run on this VM. Mirrors the
# sys.path search in tests/integration/vm/s105-browser-pwd-autofill.sh.
if [ -n "$QDBROWSER_PKG" ] && [ -d "$QDBROWSER_PKG" ]; then
    install -d -m 0755 "$DEST_QDBROWSER"
    find "$QDBROWSER_PKG" -maxdepth 1 -name '*.py' -print0 \
        | xargs -0 -I{} install -m 0644 {} "$DEST_QDBROWSER/"
    if [ -d "$QDBROWSER_PKG/plugins" ]; then
        install -d -m 0755 "$DEST_QDBROWSER/plugins"
        find "$QDBROWSER_PKG/plugins" -maxdepth 1 -name '*.py' -print0 \
            | xargs -0 -I{} install -m 0644 {} "$DEST_QDBROWSER/plugins/"
    fi
    echo "[install-browser-bridge] staged qdbrowser python pkg -> $DEST_QDBROWSER"
else
    echo "[install-browser-bridge] WARN: outer qdbrowser pkg not found; pwd_autofill probes will ModuleNotFoundError"
fi

echo "[install-browser-bridge] OK"
