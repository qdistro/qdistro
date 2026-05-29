#!/bin/bash
# Idempotent removable-media (qdistro-media-exec) install for
# fresh-vm-bootstrap. Takes the media/ source dir as $1 (default
# /root/qdistro-src/qdistro/media), copies qdistro_media_exec.py + the
# systemd unit pair into place, and enables the socket-activated service
# so removable-media bats can drive the real
# /run/qdistro-media-exec/sock path.
#
# Pre-reqs: python313 + dbus-python (baked into baseweed), udisks2 +
# dosfstools (for the loopback vfat test in s60-removable-media.sh).
#
# Sits next to install-qsu-for-vm.sh — the broker MUST be running before
# the media-exec service can issue RequestPermissionAs, so install order
# is broker -> media.
set -eu

MEDIA_SRC=${1:-/root/qdistro-src/qdistro/media}
DEST_LIB=/usr/local/lib/qdistro
SYSTEMD_DIR=/etc/systemd/system
SOCKET_UNIT=$SYSTEMD_DIR/qdistro-media-exec.socket
SERVICE_UNIT=$SYSTEMD_DIR/qdistro-media-exec.service

if [ ! -d "$MEDIA_SRC" ]; then
    echo "ERROR: media source not found at $MEDIA_SRC" >&2
    echo "       pass the media/ dir as \$1" >&2
    exit 2
fi

install -d -o root -g root -m 0755 "$DEST_LIB"

# Root-side brokered mount/unmount helper. Same install layout as the
# broker / root-exec — under /usr/local/lib/qdistro/ so an unprivileged
# user can't replace it.
install -o root -g root -m 0644 "$MEDIA_SRC/qdistro_media_exec.py" \
    "$DEST_LIB/qdistro_media_exec.py"

# Thin client qdshell invokes (unprivileged) to speak the media-exec
# wire protocol. World-readable; it holds no secrets and connects to the
# world-writable socket where SO_PEERCRED is authoritative.
install -o root -g root -m 0644 "$MEDIA_SRC/qdistro_media_exec_client.py" \
    "$DEST_LIB/qdistro_media_exec_client.py"

install -m 0644 "$MEDIA_SRC/qdistro-media-exec.socket"  "$SOCKET_UNIT"
install -m 0644 "$MEDIA_SRC/qdistro-media-exec.service" "$SERVICE_UNIT"

systemctl daemon-reload
systemctl enable --now qdistro-media-exec.socket >/dev/null

for _ in 1 2 3 4 5; do
    if [ -S /run/qdistro-media-exec/sock ]; then
        break
    fi
    sleep 0.5
done
if [ ! -S /run/qdistro-media-exec/sock ]; then
    echo "ERROR: /run/qdistro-media-exec/sock did not appear" >&2
    journalctl -u qdistro-media-exec.socket --no-pager -n 20 >&2 || true
    exit 3
fi

echo "qdistro-media-exec ready (socket /run/qdistro-media-exec/sock)"
