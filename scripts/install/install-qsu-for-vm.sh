#!/bin/bash
# Idempotent qsu install for fresh-vm-bootstrap. Takes the qsu/
# source dir as $1 (default /root/qdistro-src/qdistro/qsu), copies
# qsu.py + qdistro_root_exec.py + the systemd unit pair into place,
# and enables the socket-activated service so end-to-end qsu tests
# can drive the real /run/qdistro-root-exec/sock path.
#
# Pre-reqs: python313 + dbus-python (already baked into baseweed).
#
# This sits next to install-broker-for-qdwin.sh — the broker MUST be
# running before the root-exec service can issue RequestPermissionAs,
# so install order is broker → qsu.
set -eu

QSU_SRC=${1:-/root/qdistro-src/qdistro/qsu}
DEST_LIB=/usr/local/lib/qdistro
DEST_BIN=/usr/local/bin
SYSTEMD_DIR=/etc/systemd/system
SOCKET_UNIT=$SYSTEMD_DIR/qdistro-root-exec.socket
SERVICE_UNIT=$SYSTEMD_DIR/qdistro-root-exec.service

if [ ! -d "$QSU_SRC" ]; then
    echo "ERROR: qsu source not found at $QSU_SRC" >&2
    echo "       pass the qsu/ dir as \$1 or untar qdistro to /root/qdistro-src/qdistro/" >&2
    exit 2
fi

install -d -o root -g root -m 0755 "$DEST_LIB"
install -d -o root -g root -m 0755 "$DEST_BIN"

# 1. Privileged-exec service (root-side D-Bus delegator + subprocess
#    streamer). Same install layout as the broker — under
#    /usr/local/lib/qdistro/ so an unprivileged user can't replace it.
install -o root -g root -m 0644 "$QSU_SRC/qdistro_root_exec.py" \
    "$DEST_LIB/qdistro_root_exec.py"

# 2. User-facing wrapper. /usr/local/bin/qsu is what humans type; it
#    just invokes qsu.py through python3 so we don't have to chase
#    shebangs across distros. Note: /proc/<pid>/exe will show python3
#    by the time the broker reads it; qsu.py sends caller_name="qsu"
#    in the request frame so the admin app can display the real tool.
install -o root -g root -m 0644 "$QSU_SRC/qsu.py" "$DEST_LIB/qsu.py"
cat >"$DEST_BIN/qsu" <<'EOF'
#!/bin/bash
exec /usr/bin/python3 /usr/local/lib/qdistro/qsu.py "$@"
EOF
chmod 0755 "$DEST_BIN/qsu"

# 3. Systemd unit pair.
install -m 0644 "$QSU_SRC/qdistro-root-exec.socket"  "$SOCKET_UNIT"
install -m 0644 "$QSU_SRC/qdistro-root-exec.service" "$SERVICE_UNIT"

systemctl daemon-reload
systemctl enable --now qdistro-root-exec.socket >/dev/null

# 4. Verify the socket is listening. Service is socket-activated so
#    .service unit may be inactive until first connect — that's fine.
for _ in 1 2 3 4 5; do
    if [ -S /run/qdistro-root-exec/sock ]; then
        break
    fi
    sleep 0.5
done
if [ ! -S /run/qdistro-root-exec/sock ]; then
    echo "ERROR: /run/qdistro-root-exec/sock did not appear" >&2
    journalctl -u qdistro-root-exec.socket --no-pager -n 20 >&2 || true
    exit 3
fi

echo "qsu ready (socket /run/qdistro-root-exec/sock + /usr/local/bin/qsu)"
