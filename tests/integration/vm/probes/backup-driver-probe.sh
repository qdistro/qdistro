#!/bin/bash
# backup-driver-probe — the REAL-btrfs half of the daily backup-SERVICE driver
# (qdistro_backup_service.py, P2a). The host lane tests/integration/backup-e2e.bats
# proves the driver orchestration with tar-stubbed btrfs + a cp-stub snapshot;
# this probe runs the driver with REAL `btrfs subvolume snapshot -r`,
# `btrfs subvolume create` (the metadata collector subvol), and real `btrfs send`
# / `btrfs send -p` on a loopback filesystem — the half the headless dev host
# cannot run (CAP_SYS_ADMIN + btrfs). It directly validates the review fix that
# the collector stage must be a real SUBVOLUME (a plain dir would make the RO
# snapshot fail on btrfs).
#
# Runs as root INSIDE the test VM (staged to /root by fresh-vm-bootstrap.sh).
# Builds its own btrfs loopback so it does not depend on the VM root layout.
# remote = a local directory on the loopback (LocalDirTarget — no ssh needed);
# real ssh transport stays a separate residual. Every PASS line is asserted by
# the bats wrapper (backup-driver-e2e.bats).
set -u

WORK=/tmp/qd-backup-driver
IMG="$WORK/btrfs.img"
MNT="$WORK/mnt"
SRC_ROOT=/root/qdistro-src/qdistro/snapshots
DRV="${QDISTRO_BACKUP_SVC:-$SRC_ROOT/qdistro_backup_service.py}"
BK="${QDISTRO_BACKUP_CLI:-$SRC_ROOT/qdistro_backup_cli.py}"
CONF="$WORK/backup.conf"

fail() { printf 'FAIL: %s — %s\n' "$1" "${2:-}" >&2; exit 1; }
pass() { printf 'PASS: %s\n' "$1"; }

RAGE=""; KEYGEN=""
resolve_rage() {
    if command -v rage >/dev/null 2>&1; then RAGE=rage; KEYGEN=rage-keygen
    elif command -v age >/dev/null 2>&1; then RAGE=age; KEYGEN=age-keygen
    else return 1; fi
    export QDISTRO_RAGE="$RAGE"
}
resolve_rage || true

DRV_RUN() { python3 "$DRV" run --config "$CONF" "$@"; }   # default btrfs cmds
VERIFY() { python3 "$BK" verify --out-dir "$MNT/remote" \
    --allowed-signers "$WORK/allowed_signers" --identity owner@qdistro "$@"; }
RESTORE() { local sv="$1" dest="$2"; shift 2
    python3 "$BK" restore --out-dir "$MNT/remote" --subvol "$sv" --dest "$dest" \
        --identity-file "$WORK/id.txt" --allowed-signers "$WORK/allowed_signers" \
        --identity owner@qdistro "$@"; }

cmd_setup() {
    command -v mkfs.btrfs >/dev/null 2>&1 || fail setup "mkfs.btrfs not installed"
    command -v rsync >/dev/null 2>&1 || fail setup "rsync not installed (collector needs it)"
    [ -n "$RAGE" ] || fail setup "neither rage nor age installed"
    [ -f "$DRV" ] || fail setup "driver not staged at $DRV"
    cmd_teardown >/dev/null 2>&1 || true
    mkdir -p "$WORK" "$MNT"
    truncate -s 2G "$IMG" || fail setup truncate
    mkfs.btrfs -q -f "$IMG" || fail setup mkfs.btrfs
    mount -o loop "$IMG" "$MNT" || fail setup mount
    findmnt -no FSTYPE "$MNT" | grep -q btrfs || fail setup "not btrfs"

    # A live source subvolume the driver will snapshot itself.
    btrfs subvolume create "$MNT/data" >/dev/null || fail setup "subvol data"
    echo "the quick brown fox" > "$MNT/data/file1"
    head -c 4096 /dev/urandom > "$MNT/data/file2.bin"
    sync

    # A config-file set for the metadata collector (lives off the loopback;
    # rsync copies its CONTENTS into the collector subvol on btrfs).
    mkdir -p "$WORK/etcq"
    echo "silos.yaml contents" > "$WORK/etcq/silos.yaml"

    "$KEYGEN" -o "$WORK/id.txt" 2>"$WORK/keygen.err" || fail setup keygen
    grep -oE 'age1[0-9a-z]+' "$WORK/id.txt" | head -1 > "$WORK/recipients.txt"
    [ -s "$WORK/recipients.txt" ] || fail setup "no recipient parsed"
    ssh-keygen -t ed25519 -N "" -C backup@qdistro -f "$WORK/sign" \
        >/dev/null 2>&1 || fail setup ssh-keygen
    echo "owner@qdistro $(cat "$WORK/sign.pub")" > "$WORK/allowed_signers"

    # state_dir on the SAME btrfs fs as the source (snapshot/send need it);
    # remote is a plain dir on the loopback (LocalDirTarget).
    cat > "$CONF" <<EOF
host_id = "drvhost"
recipients = "$WORK/recipients.txt"
sign_key = "$WORK/sign"
remote = "$MNT/remote"
state_dir = "$MNT/state"
[[subvol]]
name = "data"
source = "$MNT/data"
[[subvol]]
name = "metadata"
collector = true
paths = ["$WORK/etcq"]
EOF
    pass setup
}

cmd_run_full() {
    DRV_RUN --now 1700000000 || fail run-full "driver seq0 (real btrfs) failed"
    [ -f "$MNT/remote/data-0.btrfs.age" ] || fail run-full "no data blob"
    [ -f "$MNT/remote/metadata-0.btrfs.age" ] || fail run-full "no metadata blob"
    [ -f "$MNT/remote/manifest-0.json" ] || fail run-full "no manifest"
    [ -f "$MNT/remote/manifest-0.json.sig" ] || fail run-full "no signature"
    # the collector stage is a REAL subvolume (the review fix) — the seq0
    # snapshot of it would have failed otherwise
    btrfs subvolume show "$MNT/state/collect/metadata" >/dev/null 2>&1 \
        || fail run-full "collector stage is not a btrfs subvolume"
    btrfs subvolume show "$MNT/state/snapshots/0/data" >/dev/null 2>&1 \
        || fail run-full "driver did not take a real RO snapshot"
    grep -q '"seq": 0' "$MNT/state/state.json" || fail run-full "state not advanced"
    grep -q "the quick brown fox" "$MNT/remote/data-0.btrfs.age" \
        && fail run-full "blob contains plaintext"
    pass run-full
}

cmd_verify() {
    VERIFY --checkpoint-seq 0 | grep -q "VERIFY OK" || fail verify "not OK"
    pass verify
}

cmd_restore() {
    local dest="$MNT/restore-data"
    btrfs subvolume delete "$dest/data" >/dev/null 2>&1 || true
    rm -rf "$dest"; mkdir -p "$dest"
    RESTORE data "$dest" --checkpoint-seq 0 || fail restore "restore failed"
    btrfs subvolume show "$dest/data" >/dev/null 2>&1 \
        || fail restore "received target is not a btrfs subvolume"
    diff -r "$MNT/data" "$dest/data" || fail restore "restore != source"
    pass restore
}

cmd_restore_collector() {
    # the collector subvol restores too, and carries the collected config bytes
    local dest="$MNT/restore-meta"
    btrfs subvolume delete "$dest/metadata" >/dev/null 2>&1 || true
    rm -rf "$dest"; mkdir -p "$dest"
    RESTORE metadata "$dest" --checkpoint-seq 0 || fail restore-collector "failed"
    btrfs subvolume show "$dest/metadata" >/dev/null 2>&1 \
        || fail restore-collector "not a btrfs subvolume"
    # the silos.yaml landed somewhere under the per-source subdir
    find "$dest/metadata" -name silos.yaml | grep -q silos.yaml \
        || fail restore-collector "collected config file missing from restore"
    pass restore-collector
}

cmd_incremental() {
    # mutate the source; a second driver run must take a fresh RO snapshot and
    # do a REAL `btrfs send -p` against the kept seq0 snapshot, then PRUNE seq0.
    echo "added in seq1" > "$MNT/data/newfile"; sync
    DRV_RUN --now 1700000100 || fail incremental "driver seq1 (btrfs send -p) failed"
    [ -f "$MNT/remote/data-1.btrfs.age" ] || fail incremental "no seq1 blob"
    grep -q '"parent_blob":"data-0.btrfs.age"' "$MNT/remote/manifest-1.json" \
        || fail incremental "seq1 manifest missing real parent lineage"
    grep -q '"seq": 1' "$MNT/state/state.json" || fail incremental "state not advanced to 1"
    # seq0 snapshots pruned (real `btrfs subvolume delete`); seq1 kept as parent
    [ ! -e "$MNT/state/snapshots/0" ] || fail incremental "seq0 snapshot dir not pruned"
    btrfs subvolume show "$MNT/state/snapshots/1/data" >/dev/null 2>&1 \
        || fail incremental "seq1 parent snapshot missing"
    VERIFY --checkpoint-seq 1 | grep -q "2 manifest" \
        || fail incremental "chain verify did not span 2 manifests"
    # restore the full chain -> the seq1 state (carries newfile) under --dest/data
    local dest="$MNT/incr-restore"
    btrfs subvolume delete "$dest/data" >/dev/null 2>&1 || true
    rm -rf "$dest"; mkdir -p "$dest"
    RESTORE data "$dest" --checkpoint-seq 1 || fail incremental "chain restore failed"
    [ -f "$dest/data/newfile" ] || fail incremental "restored state is not seq1 (no newfile)"
    diff -r "$MNT/data" "$dest/data" || fail incremental "chain restore != seq1 source"
    pass incremental
}

cmd_teardown() {
    # received/created subvols are read-only or nested; delete them before umount.
    for sv in $(btrfs subvolume list "$MNT" 2>/dev/null | awk '{print $NF}' | sort -r); do
        btrfs subvolume delete "$MNT/$sv" >/dev/null 2>&1 || true
    done
    umount "$MNT" 2>/dev/null || true
    rm -rf "$WORK"
    pass teardown 2>/dev/null || true
}

case "${1:-}" in
    setup) cmd_setup ;;
    run-full) cmd_run_full ;;
    verify) cmd_verify ;;
    restore) cmd_restore ;;
    restore-collector) cmd_restore_collector ;;
    incremental) cmd_incremental ;;
    teardown) cmd_teardown ;;
    *) echo "usage: $0 {setup|run-full|verify|restore|restore-collector|incremental|teardown}" >&2; exit 2 ;;
esac
