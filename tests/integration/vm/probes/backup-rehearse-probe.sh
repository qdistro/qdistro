#!/bin/bash
# backup-rehearse-probe — the REAL-btrfs half of the verify-only restore
# REHEARSAL (qdistro_backup_service.py `rehearse`, 06-backup-dr §3.3). The host
# lane tests/integration/backup-rehearse-e2e.bats proves the always-on core
# (signature + hash chain + FULL-chain blob verification + freshness + read-only)
# with tar-stubbed btrfs; this probe additionally exercises the OPTIONAL
# --rehearse-receive dry-run path with REAL `btrfs receive` into a THROWAWAY
# scratch subvol that must be destroyed afterwards — the half the headless dev
# host cannot run (CAP_SYS_ADMIN + btrfs).
#
# Runs as root INSIDE the test VM (staged to /root by fresh-vm-bootstrap.sh).
# Builds its own btrfs loopback. Reuses the same backup.conf shape as the
# backup-driver probe but adds allowed_signers + sign_identity (the rehearsal
# REQUIRES them). Every PASS line is asserted by the bats wrapper.
set -u

WORK=/tmp/qd-backup-rehearse
IMG="$WORK/btrfs.img"
MNT="$WORK/mnt"
SRC_ROOT=/root/qdistro-src/qdistro/snapshots
DRV="${QDISTRO_BACKUP_SVC:-$SRC_ROOT/qdistro_backup_service.py}"
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

DRV_RUN() { python3 "$DRV" run --config "$CONF" "$@"; }
REHEARSE() { python3 "$DRV" rehearse --config "$CONF" "$@"; }

cmd_setup() {
    command -v mkfs.btrfs >/dev/null 2>&1 || fail setup "mkfs.btrfs not installed"
    command -v rsync >/dev/null 2>&1 || fail setup "rsync not installed"
    [ -n "$RAGE" ] || fail setup "neither rage nor age installed"
    [ -f "$DRV" ] || fail setup "driver not staged at $DRV"
    cmd_teardown >/dev/null 2>&1 || true
    mkdir -p "$WORK" "$MNT"
    truncate -s 2G "$IMG" || fail setup truncate
    mkfs.btrfs -q -f "$IMG" || fail setup mkfs.btrfs
    mount -o loop "$IMG" "$MNT" || fail setup mount
    findmnt -no FSTYPE "$MNT" | grep -q btrfs || fail setup "not btrfs"

    btrfs subvolume create "$MNT/data" >/dev/null || fail setup "subvol data"
    echo "the quick brown fox" > "$MNT/data/file1"
    head -c 4096 /dev/urandom > "$MNT/data/file2.bin"
    sync

    "$KEYGEN" -o "$WORK/id.txt" 2>"$WORK/keygen.err" || fail setup keygen
    grep -oE 'age1[0-9a-z]+' "$WORK/id.txt" | head -1 > "$WORK/recipients.txt"
    [ -s "$WORK/recipients.txt" ] || fail setup "no recipient parsed"
    ssh-keygen -t ed25519 -N "" -C backup@qdistro -f "$WORK/sign" \
        >/dev/null 2>&1 || fail setup ssh-keygen
    echo "owner@qdistro $(cat "$WORK/sign.pub")" > "$WORK/allowed_signers"

    cat > "$CONF" <<EOF
host_id = "rehearsehost"
recipients = "$WORK/recipients.txt"
sign_key = "$WORK/sign"
allowed_signers = "$WORK/allowed_signers"
sign_identity = "owner@qdistro"
remote = "$MNT/remote"
state_dir = "$MNT/state"
[[subvol]]
name = "data"
source = "$MNT/data"
EOF
    # Two runs so the rehearsal exercises a real incremental CHAIN (a full +
    # an incremental), which is exactly where verifying only the newest blob
    # would false-green.
    DRV_RUN --now 1700000000 >/dev/null || fail setup "seq0 backup failed"
    echo "added in seq1" > "$MNT/data/newfile"; sync
    DRV_RUN --now 1700000100 >/dev/null || fail setup "seq1 backup failed"
    grep -q '"seq": 1' "$MNT/state/state.json" || fail setup "state not at seq1"
    pass setup
}

cmd_verify_only() {
    # The always-on core over a REAL chain on real btrfs (no receive).
    REHEARSE | grep -q '"rehearsal": "ok"' || fail verify-only "rehearsal not ok"
    REHEARSE | grep -q '"manifests": 2' || fail verify-only "did not span 2 manifests"
    # READ-ONLY: the rehearsal must not advance the seq anchor.
    grep -q '"seq": 1' "$MNT/state/state.json" || fail verify-only "seq anchor moved"
    pass verify-only
}

cmd_receive() {
    # OPTIONAL dry-run receive of the full chain into a throwaway scratch subvol
    # under state_dir, then DESTROYED. Real `btrfs receive`.
    REHEARSE --rehearse-receive --rehearse-subvol data \
        --identity-file "$WORK/id.txt" | grep -q '"received": true' \
        || fail receive "rehearse-receive did not report received"
    # The scratch subvol tree must be GONE afterwards (throwaway, destroyed).
    [ ! -e "$MNT/state/rehearsal-scratch" ] \
        || fail receive "rehearsal scratch subvol not cleaned up"
    # Live state untouched: seq anchor still 1, no restore landed in a live dest.
    grep -q '"seq": 1' "$MNT/state/state.json" || fail receive "seq anchor moved"
    pass receive
}

cmd_corrupt_ancestor() {
    # Corrupt the seq0 ANCESTOR full blob; the newest manifest/blob is intact.
    # The rehearsal must STILL fail (full-chain coverage), not false-green.
    printf 'CORRUPT' >> "$MNT/remote/data-0.btrfs.age"
    if REHEARSE >/dev/null 2>&1; then
        fail corrupt-ancestor "rehearsal passed despite a corrupt ANCESTOR blob"
    fi
    pass corrupt-ancestor
}

cmd_teardown() {
    for sv in $(btrfs subvolume list "$MNT" 2>/dev/null | awk '{print $NF}' | sort -r); do
        btrfs subvolume delete "$MNT/$sv" >/dev/null 2>&1 || true
    done
    umount "$MNT" 2>/dev/null || true
    rm -rf "$WORK"
    pass teardown 2>/dev/null || true
}

case "${1:-}" in
    setup) cmd_setup ;;
    verify-only) cmd_verify_only ;;
    receive) cmd_receive ;;
    corrupt-ancestor) cmd_corrupt_ancestor ;;
    teardown) cmd_teardown ;;
    *) echo "usage: $0 {setup|verify-only|receive|corrupt-ancestor|teardown}" >&2; exit 2 ;;
esac
