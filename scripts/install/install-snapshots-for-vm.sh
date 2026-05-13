#!/bin/bash
# install-snapshots-for-vm.sh — idempotent install of the spec/19
# Phase-8 MVP snapshot bridge into the VM.
#
# Drops:
#   /usr/libexec/qdistro/qdistro_snapshots.py       # engine (+ broker)
#   /usr/libexec/qdistro/qdistro_snap_export_cli.py # CLI module
#   /usr/local/bin/qdistro-snap-export              # CLI shim
#   /etc/systemd/system/qdistro-backup.service      # daily encrypt-export
#   /etc/systemd/system/qdistro-backup.timer
#
# Source files staged at /root/snapshots-src/ by fresh-vm-bootstrap.sh.
#
# qdistro_snapshots is what the broker imports for SnapshotBefore /
# ListSnapshots / GetFiles. Without this script, the broker still
# starts and the methods raise SnapperUnavailable until the import
# resolves.
set -euo pipefail

SRC=${1:-/root/snapshots-src}
if [ ! -d "$SRC" ]; then
    echo "[install-snapshots] missing source dir $SRC" >&2
    exit 2
fi

DEST_LIB_QDISTRO=/usr/libexec/qdistro
DEST_BIN=/usr/local/bin
DEST_SYSD=/etc/systemd/system

install -d -m 0755 "$DEST_LIB_QDISTRO" "$DEST_BIN" "$DEST_SYSD"

install -m 0644 "$SRC/qdistro_snapshots.py"       "$DEST_LIB_QDISTRO/"
install -m 0644 "$SRC/qdistro_snap_export_cli.py" "$DEST_LIB_QDISTRO/"

cat >"$DEST_BIN/qdistro-snap-export" <<'CLI'
#!/bin/bash
exec /usr/bin/python3 /usr/libexec/qdistro/qdistro_snap_export_cli.py "$@"
CLI
chmod 0755 "$DEST_BIN/qdistro-snap-export"

install -m 0644 "$SRC/qdistro-backup.service" "$DEST_SYSD/qdistro-backup.service"
install -m 0644 "$SRC/qdistro-backup.timer"   "$DEST_SYSD/qdistro-backup.timer"

systemctl daemon-reload >/dev/null 2>&1 || true

echo "[install-snapshots] OK"
