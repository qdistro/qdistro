#!/bin/bash
# install-snapshots-for-vm.sh — idempotent install of the spec/19
# Phase-8 MVP snapshot bridge into the VM.
#
# Drops:
#   /usr/libexec/qdistro/qdistro_snapshots.py        # engine (+ broker)
#   /usr/libexec/qdistro/qdistro_backup_manifest.py  # signed-manifest layer
#   /usr/libexec/qdistro/qdistro_backup_cli.py       # backup/verify/restore CLI
#   /usr/libexec/qdistro/qdistro_backup_service.py   # daily-service DRIVER
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

# J25: profile gate for the rage-encryption install below.
_IS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
# shellcheck source=lib/qdistro-profile.sh
. "$_IS_DIR/lib/qdistro-profile.sh"
resolve_profile || exit 2

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
    # J25: rage is the BACKUP-ENCRYPTION tool — installing it from an unsigned
    # mirror could substitute a weakened build. GPG checking is profile-gated
    # (dev may skip; hardened verifies). And in hardened profiles a failed
    # install is FATAL: a snapshot/backup feature that silently can't encrypt
    # is worse than a loud stop. dev keeps the best-effort warn (a disposable
    # VM surfaces it in the backup-btrfs-e2e lane).
    rage_gpg_flags=()
    is_dev && rage_gpg_flags=( --no-gpg-checks )
    if ! zypper -n "${rage_gpg_flags[@]}" install rage-encryption; then
        msg="[install-snapshots] rage-encryption install failed; backups cannot encrypt until 'rage' is present"
        if is_dev; then
            echo "$msg (dev profile: continuing)" >&2
        else
            echo "ERROR: $msg" >&2
            exit 1
        fi
    fi
fi

install -d -m 0755 "$DEST_LIB_QDISTRO" "$DEST_BIN" "$DEST_SYSD"

install -m 0644 "$SRC/qdistro_snapshots.py"        "$DEST_LIB_QDISTRO/"
install -m 0644 "$SRC/qdistro_backup_manifest.py"  "$DEST_LIB_QDISTRO/"
install -m 0644 "$SRC/qdistro_backup_cli.py"       "$DEST_LIB_QDISTRO/"
install -m 0644 "$SRC/qdistro_backup_service.py"   "$DEST_LIB_QDISTRO/"
install -m 0644 "$SRC/qdistro_backup_recovery.py"  "$DEST_LIB_QDISTRO/"
# qdistro-snap-swap — the per-user "Roll back this user (full)" crash-consistent
# state-restore CLI that the admin Snapshots panel drives (doc/recovery.md). Its
# source lives in snapshots/ (this $SRC) but it was previously installed ONLY by
# install-templates-for-vm.sh, a VM-only script NOT in the bootstrap installer
# chain — so on a production install the panel's rollback action would hit a
# missing binary. It ships here, in the `snapshots` chain step, so it lands
# wherever the snapshot feature does. Stdlib-only (no sibling imports), installed
# into the same /usr/libexec/qdistro/ + /usr/local/bin/ paths the templates
# script uses, so VM bakes that run both installers just write it twice
# (idempotent, identical content).
install -m 0644 "$SRC/qdistro_snap_swap.py"        "$DEST_LIB_QDISTRO/"

# Signed-manifest backup/verify/restore CLI (engine) + the daily-service driver.
# (The legacy unsigned ``qdistro-snap-export`` shim was removed — command
# injection, opus-security-review HIGH #4; the signed engine below supersedes it.)
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

cat >"$DEST_BIN/qdistro-snap-swap" <<'CLI'
#!/bin/bash
exec /usr/bin/python3 /usr/libexec/qdistro/qdistro_snap_swap.py "$@"
CLI
chmod 0755 "$DEST_BIN/qdistro-snap-swap"

install -m 0644 "$SRC/qdistro-backup.service" "$DEST_SYSD/qdistro-backup.service"
install -m 0644 "$SRC/qdistro-backup.timer"   "$DEST_SYSD/qdistro-backup.timer"
# Weekly verify-only restore rehearsal (06 §3.3) — read-only, fails loudly.
install -m 0644 "$SRC/qdistro-backup-verify.service" "$DEST_SYSD/qdistro-backup-verify.service"
install -m 0644 "$SRC/qdistro-backup-verify.timer"   "$DEST_SYSD/qdistro-backup-verify.timer"

# Operator template — copying it to /etc/qdistro/backup.conf is what ENABLES the
# daily backup (the unit ConditionPathExists-skips until then). Never overwrite a
# real backup.conf.
if [ -f "$SRC/backup.conf.example" ]; then
    install -d -m 0755 /etc/qdistro
    install -m 0644 "$SRC/backup.conf.example" /etc/qdistro/backup.conf.example
fi

systemctl daemon-reload >/dev/null 2>&1 || true

echo "[install-snapshots] OK"
