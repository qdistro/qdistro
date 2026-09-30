#!/bin/bash
# Install the graphical admin approval UI for the uid-1000 qdwin session.
# DESTDIR allows a host-only layout test; production paths remain absolute.
set -euo pipefail

_QDO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
# shellcheck source=lib/qdistro-offline.sh
. "$_QDO_DIR/lib/qdistro-offline.sh"
resolve_offline_install

APP_SRC=${1:-/root/qdistro-src/admin_app}
[ -d "$APP_SRC" ] || { echo "ERROR: admin app source directory missing: $APP_SRC" >&2; exit 2; }
REPO_ROOT=$(cd "$APP_SRC/.." && pwd)
DESTDIR=${DESTDIR:-}
case "$DESTDIR" in
    ""|/*) ;;
    *) echo "ERROR: DESTDIR must be an absolute path" >&2; exit 2 ;;
esac
if [ "$DESTDIR" = / ]; then
    echo "ERROR: DESTDIR=/ is ambiguous; leave it unset for a live install" >&2
    exit 2
fi
if [ -z "$DESTDIR" ]; then
    [ "$(id -u)" = 0 ] || { echo "ERROR: root is required for a live install" >&2; exit 2; }
    OWN=(-o root -g root)
else
    OWN=()
fi

APP=$APP_SRC/qdistro_admin_app.py
DESKTOP=$APP_SRC/qdistro-admin-app.desktop
LAUNCHER=$REPO_ROOT/deploy/start-admin-app-wayland.sh
for source_file in "$APP" "$DESKTOP" "$LAUNCHER"; do
    [ -f "$source_file" ] || { echo "ERROR: missing admin app source: $source_file" >&2; exit 2; }
done

# Keep the app and its launcher outside /root/qdistro-src: that retained
# source tree is not readable by the admin session on the image. The broker
# already trusts this exact installed app path for uid-1000 Python peers.
install -d "${OWN[@]}" -m 0755 "$DESTDIR/usr/local/bin" \
    "$DESTDIR/usr/share/applications"
install "${OWN[@]}" -m 0755 "$APP" \
    "$DESTDIR/usr/local/bin/qdistro-admin-approval-app"
install "${OWN[@]}" -m 0755 "$LAUNCHER" \
    "$DESTDIR/usr/local/bin/qdistro-start-admin-app"
install "${OWN[@]}" -m 0644 "$DESKTOP" \
    "$DESTDIR/usr/share/applications/qdistro-admin-app.desktop"

# The image and bare bootstrap both install these from distro packages.
# Catch a packaging omission here while the strict image chain can fail.
if [ -z "$DESTDIR" ]; then
    /usr/bin/python3 -c 'import dbus, dbus.mainloop.glib, yaml; from PyQt6 import QtCore, QtGui, QtWidgets'
fi
echo "graphical admin approval UI installed for uid-1000 Wayland sessions"
