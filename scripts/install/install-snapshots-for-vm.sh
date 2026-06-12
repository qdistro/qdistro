#!/bin/bash
# install-snapshots-for-vm.sh — idempotent install of the spec/19
# Phase-8 MVP snapshot bridge into the VM.
#
# Drops:
#   /usr/libexec/qdistro/qdistro_snapshots.py        # engine (+ broker)
#   /usr/libexec/qdistro/qdistro_snap_export_cli.py  # OLD CLI module (kept)
#   /usr/libexec/qdistro/qdistro_backup_manifest.py  # signed-manifest layer
#   /usr/libexec/qdistro/qdistro_backup_cli.py       # backup/verify/restore CLI
#   /usr/libexec/qdistro/qdistro_backup_service.py   # daily-service DRIVER
#   /usr/local/bin/qdistro-snap-export               # OLD CLI shim (kept)
#   /usr/local/bin/qdistro-backup                    # backup/verify/restore shim
#   /usr/local/bin/qdistro-backup-run                # driver shim (live path)
#   /etc/systemd/system/qdistro-backup.service       # daily signed-manifest backup
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

# The signed-manifest backup CLI encrypts blobs via `rage` ($QDISTRO_RAGE,
# default "rage"). rage-encryption (Rust age impl) provides /usr/bin/rage +
# rage-keygen. install-deps.sh bakes it into the image; install it here too so
# a freshly bootstrapped VM (cloned from a pre-rage bake) still has the backup
# encryptor present. Best-effort: a network hiccup here is surfaced loudly by
# the backup-btrfs-e2e.bats setup, not silently swallowed by skipping the lane.
if ! command -v rage >/dev/null 2>&1; then
    zypper -n --no-gpg-checks install rage-encryption >/dev/null 2>&1 \
        || echo "[install-snapshots] WARN: rage-encryption install failed; " \
                "backups cannot encrypt until 'rage' is present" >&2
fi

install -d -m 0755 "$DEST_LIB_QDISTRO" "$DEST_BIN" "$DEST_SYSD"

install -m 0644 "$SRC/qdistro_snapshots.py"        "$DEST_LIB_QDISTRO/"
install -m 0644 "$SRC/qdistro_snap_export_cli.py"  "$DEST_LIB_QDISTRO/"
install -m 0644 "$SRC/qdistro_backup_manifest.py"  "$DEST_LIB_QDISTRO/"
install -m 0644 "$SRC/qdistro_backup_cli.py"       "$DEST_LIB_QDISTRO/"
install -m 0644 "$SRC/qdistro_backup_service.py"   "$DEST_LIB_QDISTRO/"

cat >"$DEST_BIN/qdistro-snap-export" <<'CLI'
#!/bin/bash
exec /usr/bin/python3 /usr/libexec/qdistro/qdistro_snap_export_cli.py "$@"
CLI
chmod 0755 "$DEST_BIN/qdistro-snap-export"

# Signed-manifest backup/verify/restore CLI (engine) + the daily-service driver.
cat >"$DEST_BIN/qdistro-backup" <<'CLI'
#!/bin/bash
exec /usr/bin/python3 /usr/libexec/qdistro/qdistro_backup_cli.py "$@"
CLI
chmod 0755 "$DEST_BIN/qdistro-backup"

cat >"$DEST_BIN/qdistro-backup-run" <<'CLI'
#!/bin/bash
exec /usr/bin/python3 /usr/libexec/qdistro/qdistro_backup_service.py "$@"
CLI
chmod 0755 "$DEST_BIN/qdistro-backup-run"

install -m 0644 "$SRC/qdistro-backup.service" "$DEST_SYSD/qdistro-backup.service"
install -m 0644 "$SRC/qdistro-backup.timer"   "$DEST_SYSD/qdistro-backup.timer"

# Operator template — copying it to /etc/qdistro/backup.conf is what ENABLES the
# daily backup (the unit ConditionPathExists-skips until then). Never overwrite a
# real backup.conf.
if [ -f "$SRC/backup.conf.example" ]; then
    install -d -m 0755 /etc/qdistro
    install -m 0644 "$SRC/backup.conf.example" /etc/qdistro/backup.conf.example
fi

systemctl daemon-reload >/dev/null 2>&1 || true

echo "[install-snapshots] OK"
