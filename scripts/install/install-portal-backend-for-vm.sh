#!/bin/bash
# install-portal-backend-for-vm.sh -- idempotent install of the qdistro
# xdg-desktop-portal backend onto a VM or image build.
set -euo pipefail

SRC=${1:-/root/qdistro-src/qdistro}
if [ ! -d "$SRC" ]; then
    echo "[install-portal-backend] missing source dir $SRC" >&2
    exit 2
fi

DEST_LIB=/usr/lib/qdistro/daemons
DEST_PORTALS=/usr/share/xdg-desktop-portal/portals
DEST_PORTAL_CFG=/usr/share/xdg-desktop-portal
DEST_DBUS=/usr/share/dbus-1/services
DEST_USER_SYSD=/etc/systemd/user

install -d -m 0755 \
    "$DEST_LIB" "$DEST_PORTALS" "$DEST_PORTAL_CFG" \
    "$DEST_DBUS" "$DEST_USER_SYSD"

install -m 0755 "$SRC/daemons/qdistro_portal_backend.py" \
    "$DEST_LIB/qdistro_portal_backend.py"
install -m 0755 "$SRC/daemons/qdistro_portal_frontend.py" \
    "$DEST_LIB/qdistro_portal_frontend.py"
install -m 0644 "$SRC/broker/qdistro_proc_identity.py" \
    "$DEST_LIB/qdistro_proc_identity.py"
install -m 0644 "$SRC/broker/qdistro_resolver.py" \
    "$DEST_LIB/qdistro_resolver.py"
install -m 0644 "$SRC/deploy/portals/qdistro.portal" \
    "$DEST_PORTALS/qdistro.portal"
install -m 0644 "$SRC/deploy/portals/qdistro-portals.conf" \
    "$DEST_PORTAL_CFG/qdistro-portals.conf"
install -m 0644 \
    "$SRC/deploy/dbus-1/services/org.freedesktop.impl.portal.qdistro.service" \
    "$DEST_DBUS/org.freedesktop.impl.portal.qdistro.service"
install -m 0644 "$SRC/deploy/systemd/services/qdistro-portal-backend.service" \
    "$DEST_USER_SYSD/qdistro-portal-backend.service"

echo "[install-portal-backend] installed qdistro xdg-desktop-portal backend/frontend core"
