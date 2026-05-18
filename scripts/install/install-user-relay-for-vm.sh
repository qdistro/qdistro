#!/bin/bash
# Idempotent install for qdistro-user-relay (per-uid session-bus relay).
#
# Drops qdistro_user_relay.py under /usr/local/lib/qdistro/, installs
# the dbus system-bus policy (org.qdistro.UserRelay.conf), and the
# systemd template `qdistro-user-relay@<uid>.service`. The template
# is started on-demand by qdshell-session-launcher when a silo comes
# up, not enabled here — there's no point in starting a uid's relay
# before that uid's session bus exists.
#
# Usage: $0 [SRC]     # SRC defaults to /root/qdistro-src/qdistro/user_relay
set -eu

SRC=${1:-/root/qdistro-src/qdistro/user_relay}
DEST_LIB=/usr/local/lib/qdistro
SYSTEMD_DIR=/etc/systemd/system
POLICY=/etc/dbus-1/system.d/org.qdistro.UserRelay.conf
UNIT_TEMPLATE=$SYSTEMD_DIR/qdistro-user-relay@.service

if [ ! -d "$SRC" ]; then
    echo "ERROR: user-relay source not found at $SRC" >&2
    exit 2
fi

install -d -o root -g root -m 0755 "$DEST_LIB"

install -o root -g root -m 0644 "$SRC/qdistro_user_relay.py" \
    "$DEST_LIB/qdistro_user_relay.py"

install -m 0644 "$SRC/org.qdistro.UserRelay.conf" "$POLICY"
install -m 0644 "$SRC/qdistro-user-relay@.service" "$UNIT_TEMPLATE"

systemctl reload dbus-broker.service 2>/dev/null \
    || systemctl reload dbus.service 2>/dev/null \
    || true

systemctl daemon-reload

echo "qdistro-user-relay template installed; start per-uid with: " \
     "systemctl start qdistro-user-relay@<uid>.service"
