#!/bin/bash
# backup-btrfs-probe — the REAL `btrfs send -p | btrfs receive` half of the
# backup DR e2e (06-backup-dr-draft.md §4/§6, finding F1). The host lane
# tests/integration/backup-e2e.bats proves the orchestration + integrity gates
# with tar-backed btrfs stubs and REAL rage + signed manifests; this probe
# swaps in the real btrfs send/receive on a real loopback filesystem — the one
# half the headless dev host could not run (CAP_SYS_ADMIN + btrfs).
#
# Runs as root INSIDE the test VM (staged to /root by fresh-vm-bootstrap.sh;
# host-runnable too where root + btrfs + age/rage exist). It builds its own
# btrfs loopback so it does not depend on the VM's root layout. Every PASS line
# is asserted by the bats wrapper (backup-btrfs-e2e.bats).
#
# Encryption: the backup CLI shells out to `rage` ($QDISTRO_RAGE). rage and age
# share the -e/-R/-d/-i CLI surface and the age1.../AGE-SECRET-KEY format, so
# the probe uses whichever is installed (production VMs ship one; this probe
# skips loudly if neither is present — see the bats setup guard).
set -u

WORK=/tmp/qd-backup-btrfs
IMG="$WORK/btrfs.img"
MNT="$WORK/mnt"
REMOTE="$WORK/remote"
BK="${QDISTRO_BACKUP_CLI:-/root/qdistro-src/qdistro/snapshots/qdistro_backup_cli.py}"

fail() { printf 'FAIL: %s — %s\n' "$1" "${2:-}" >&2; exit 1; }
pass() { printf 'PASS: %s\n' "$1"; }

# Resolve an age-compatible encryptor and a keygen for it. Each subcommand runs
# in its OWN process (the bats wrapper does one `vm_run` per @test), so this
# must run on every invocation — an export from `setup` does not survive into
# `full-backup`. Non-fatal so teardown works even with neither tool; setup does
# the hard check. The backup CLI reads $QDISTRO_RAGE (default "rage").
RAGE=""; KEYGEN=""
resolve_rage() {
    if command -v rage >/dev/null 2>&1; then
        RAGE=rage; KEYGEN=rage-keygen
    elif command -v age >/dev/null 2>&1; then
        RAGE=age; KEYGEN=age-keygen
    else
        return 1
    fi
    export QDISTRO_RAGE="$RAGE"
}
resolve_rage || true

# do_backup <seq> <prev-manifest|""> <subvol-spec...>
do_backup() {
    local seq="$1" prev="$2"; shift 2
    local args=(backup --recipients "$WORK/recipients.txt" --out-dir "$REMOTE"
        --seq "$seq" --host-id testhost --created-at "$((1700000000 + seq))"
        --sign-key "$WORK/sign")
    [ -n "$prev" ] && args+=(--prev-manifest "$prev")
    local s; for s in "$@"; do args+=(--subvol "$s"); done
    python3 "$BK" "${args[@]}"
}

VERIFY() { python3 "$BK" verify --out-dir "$REMOTE" \
    --allowed-signers "$WORK/allowed_signers" --identity owner@qdistro "$@"; }

RESTORE() { # <subvol> <dest> [extra args...]
    local sv="$1" dest="$2"; shift 2
    python3 "$BK" restore --out-dir "$REMOTE" --subvol "$sv" --dest "$dest" \
        --identity-file "$WORK/id.txt" --allowed-signers "$WORK/allowed_signers" \
        --identity owner@qdistro "$@"; }

cmd_setup() {
    command -v mkfs.btrfs >/dev/null 2>&1 || fail setup "mkfs.btrfs not installed"
    [ -n "$RAGE" ] || fail setup "neither rage nor age installed (backup encryption needs one)"
    cmd_teardown >/dev/null 2>&1 || true
    mkdir -p "$WORK" "$MNT"
    truncate -s 2G "$IMG" || fail setup "truncate"
    mkfs.btrfs -q -f "$IMG" || fail setup "mkfs.btrfs"
    mount -o loop "$IMG" "$MNT" || fail setup "mount loopback"
    findmnt -no FSTYPE "$MNT" | grep -q btrfs || fail setup "mount is not btrfs"

    # source subvols: one data + one metadata-staging subvol (§2)
    btrfs subvolume create "$MNT/data" >/dev/null || fail setup "subvol data"
    echo "the quick brown fox" > "$MNT/data/file1"
    head -c 4096 /dev/urandom > "$MNT/data/file2.bin"
    mkdir -p "$MNT/data/sub"; echo nested > "$MNT/data/sub/file3"
    btrfs subvolume create "$MNT/meta" >/dev/null || fail setup "subvol meta"
    echo "silos.yaml contents" > "$MNT/meta/silos.yaml"
    sync

    # read-only snapshots: btrfs send requires a read-only subvolume
    mkdir -p "$MNT/snap0"
    btrfs subvolume snapshot -r "$MNT/data" "$MNT/snap0/data" >/dev/null \
        || fail setup "ro snapshot data"
    btrfs subvolume snapshot -r "$MNT/meta" "$MNT/snap0/meta" >/dev/null \
        || fail setup "ro snapshot meta"
    sync

    # age/rage keypair: identity (decrypt) + recipient (encrypt)
    "$KEYGEN" -o "$WORK/id.txt" 2>"$WORK/keygen.err" || fail setup "keygen"
    grep -oE 'age1[0-9a-z]+' "$WORK/id.txt" | head -1 > "$WORK/recipients.txt"
    [ -s "$WORK/recipients.txt" ] || fail setup "no recipient parsed from id.txt"

    # ssh signing key + allowed_signers pinned to an identity
    ssh-keygen -t ed25519 -N "" -C backup@qdistro -f "$WORK/sign" \
        >/dev/null 2>&1 || fail setup "ssh-keygen"
    echo "owner@qdistro $(cat "$WORK/sign.pub")" > "$WORK/allowed_signers"
    pass setup
}

cmd_full_backup() {
    do_backup 0 "" "data:$MNT/snap0/data" "meta:$MNT/snap0/meta" \
        || fail full-backup "backup seq0 (real btrfs send) failed"
    [ -f "$REMOTE/data-0.btrfs.age" ] || fail full-backup "no data blob"
    [ -f "$REMOTE/meta-0.btrfs.age" ] || fail full-backup "no meta blob"
    [ -f "$REMOTE/manifest-0.json" ] || fail full-backup "no manifest"
    [ -f "$REMOTE/manifest-0.json.sig" ] || fail full-backup "no signature"
    # blob is real age ciphertext, not plaintext
    if grep -q "the quick brown fox" "$REMOTE/data-0.btrfs.age"; then
        fail full-backup "blob contains plaintext"
    fi
    head -c 30 "$REMOTE/data-0.btrfs.age" | grep -q "age-encryption" \
        || fail full-backup "blob is not age-encrypted"
    pass full-backup
}

cmd_verify_clean() {
    VERIFY --checkpoint-seq 0 | grep -q "VERIFY OK" || fail verify-clean "verify did not report OK"
    pass verify-clean
}

cmd_restore_full() {
    local dest="$MNT/restore-data"
    # idempotent rerun: a received subvol is read-only; rm -rf cannot remove it.
    btrfs subvolume delete "$dest/data" >/dev/null 2>&1 || true
    rm -rf "$dest"
    btrfs subvolume create "$dest" >/dev/null 2>&1 || mkdir -p "$dest"
    RESTORE data "$dest" --checkpoint-seq 0 || fail restore-full "restore failed"
    # the received target is a REAL btrfs subvolume (not a tar dir)
    btrfs subvolume show "$dest/data" >/dev/null 2>&1 \
        || fail restore-full "received target is not a btrfs subvolume"
    diff -r "$MNT/snap0/data" "$dest/data" || fail restore-full "restore != source"
    pass restore-full
}

cmd_fail_corrupt() {
    # corrupt a blob in a private copy -> verify FAILS + restore ABORTS
    local r="$WORK/corrupt"; rm -rf "$r"; cp -r "$REMOTE" "$r"
    # seek=100 lands in the age ASCII header (deterministic: header bytes are
    # < 0x80, so writing 0xff always flips one — no 1/256 already-0xff no-op).
    printf '\xff' | dd of="$r/data-0.btrfs.age" bs=1 seek=100 count=1 \
        conv=notrunc status=none
    if python3 "$BK" verify --out-dir "$r" --allowed-signers "$WORK/allowed_signers" \
        --identity owner@qdistro --checkpoint-seq 0 2>"$WORK/corrupt.verify.err"; then
        fail fail-corrupt "verify PASSED on a corrupted blob"
    fi
    grep -q "sha256 mismatch" "$WORK/corrupt.verify.err" \
        || fail fail-corrupt "verify failed without a sha256-mismatch reason"
    local dest="$MNT/corrupt-restore"; rm -rf "$dest"; mkdir -p "$dest"
    if python3 "$BK" restore --out-dir "$r" --subvol data --dest "$dest" \
        --identity-file "$WORK/id.txt" --allowed-signers "$WORK/allowed_signers" \
        --identity owner@qdistro --checkpoint-seq 0 2>"$WORK/corrupt.restore.err"; then
        fail fail-corrupt "restore SUCCEEDED on a corrupted blob"
    fi
    grep -q "blob verification failed" "$WORK/corrupt.restore.err" \
        || fail fail-corrupt "restore aborted for the wrong reason: $(tr '\n' ' ' < "$WORK/corrupt.restore.err")"
    [ ! -e "$dest/data" ] || fail fail-corrupt "restore left a subvol despite corruption"
    pass fail-corrupt
}

cmd_fail_nosigner() {
    # verify/restore REFUSE without --allowed-signers (fail-closed)
    if python3 "$BK" verify --out-dir "$REMOTE" 2>"$WORK/nosign.err"; then
        fail fail-nosigner "verify PASSED without signature verification"
    fi
    grep -q "without signature verification" "$WORK/nosign.err" \
        || fail fail-nosigner "verify failed for the wrong reason"
    local dest="$MNT/nosign-restore"; rm -rf "$dest"; mkdir -p "$dest"
    if python3 "$BK" restore --out-dir "$REMOTE" --subvol data --dest "$dest" \
        --identity-file "$WORK/id.txt" 2>"$WORK/nosign.restore.err"; then
        fail fail-nosigner "restore SUCCEEDED without signature verification"
    fi
    grep -q "without signature verification" "$WORK/nosign.restore.err" \
        || fail fail-nosigner "restore refused for the wrong reason: $(tr '\n' ' ' < "$WORK/nosign.restore.err")"
    [ ! -e "$dest/data" ] || fail fail-nosigner "restore left a subvol with no signer"
    pass fail-nosigner
}

cmd_incremental_send() {
    # mutate source, RO-snapshot it, and run a REAL incremental `btrfs send -p`
    echo "added in seq1" > "$MNT/data/newfile"; sync
    mkdir -p "$MNT/snap1"
    btrfs subvolume snapshot -r "$MNT/data" "$MNT/snap1/data" >/dev/null \
        || fail incremental-send "ro snapshot seq1"
    do_backup 1 "$REMOTE/manifest-0.json" \
        "data:$MNT/snap1/data:$MNT/snap0/data" \
        || fail incremental-send "incremental backup seq1 (btrfs send -p) failed"
    [ -f "$REMOTE/data-1.btrfs.age" ] || fail incremental-send "no seq1 blob"
    # the seq-1 manifest records the parent blob -> real lineage, not implied
    grep -q '"parent_blob":"data-0.btrfs.age"' "$REMOTE/manifest-1.json" \
        || fail incremental-send "seq1 manifest missing real parent lineage"
    VERIFY --checkpoint-seq 1 | grep -q "2 manifest" \
        || fail incremental-send "chain verify did not span 2 manifests"
    pass incremental-send
}

cmd_incremental_restore_chain() {
    # REGRESSION GUARD for 06-backup-dr-draft.md §6 finding F-A: restore a full
    # 2-seq chain (full seq0 + incremental seq1) on REAL btrfs. The host tar stub
    # masked this — it overwrites the same dir, so it never hit the original
    # same-name `btrfs receive` collision ("File exists"). The CLI now receives
    # each incremental ANCESTOR into a per-seq staging subvol (btrfs locates the
    # parent by UUID fs-wide) and the FINAL seq straight into --dest, exposing the
    # restored state under the stable name. The restored subvol must therefore
    # equal the seq1 source (which carries `newfile` from incremental-send), NOT
    # the seq0 base.
    local dest="$MNT/incr-restore"
    # idempotent rerun: a received subvol is read-only; rm -rf cannot remove it.
    btrfs subvolume delete "$dest/data" >/dev/null 2>&1 || true
    rm -rf "$dest"; mkdir -p "$dest"
    RESTORE data "$dest" --checkpoint-seq 1 2>"$WORK/incr.err" \
        || fail incremental-restore-chain \
            "incremental chain restore failed: $(tr '\n' ' ' < "$WORK/incr.err")"
    # the received target is a REAL btrfs subvolume (not a plain dir)
    btrfs subvolume show "$dest/data" >/dev/null 2>&1 \
        || fail incremental-restore-chain "restored target is not a btrfs subvolume"
    # final state == seq1 source (the incremental was actually applied)
    diff -r "$MNT/snap1/data" "$dest/data" \
        || fail incremental-restore-chain "restored chain != seq1 source"
    # the seq1-only file proves it is the incremental state, not the seq0 base
    [ -f "$dest/data/newfile" ] \
        || fail incremental-restore-chain "restored chain is missing the seq1 increment (newfile)"
    # the intermediate ancestor staging must be cleaned up — only the final
    # state is kept under --dest
    [ ! -e "$dest/.qd-restore-chain" ] \
        || fail incremental-restore-chain "intermediate chain staging was not cleaned up"
    pass incremental-restore-chain
}

cmd_teardown() {
    # Unmount BEFORE rm -rf so we never recurse into the live btrfs (which would
    # unlink the backing image out from under the loop device and leak the
    # mount). `mount -o loop` autoclears the loop device on unmount, so no
    # explicit losetup -D (which would detach unrelated loop devs system-wide).
    if mountpoint -q "$MNT" 2>/dev/null; then
        umount "$MNT" 2>/dev/null || umount -l "$MNT" 2>/dev/null || true
    fi
    if mountpoint -q "$MNT" 2>/dev/null; then
        fail teardown "could not unmount $MNT (refusing to rm -rf into a live fs)"
    fi
    rm -rf "$WORK"
    pass teardown
}

case "${1:-}" in
    setup) cmd_setup ;;
    full-backup) cmd_full_backup ;;
    verify-clean) cmd_verify_clean ;;
    restore-full) cmd_restore_full ;;
    fail-corrupt) cmd_fail_corrupt ;;
    fail-nosigner) cmd_fail_nosigner ;;
    incremental-send) cmd_incremental_send ;;
    incremental-restore-chain) cmd_incremental_restore_chain ;;
    teardown) cmd_teardown ;;
    *) echo "usage: $0 {setup|full-backup|verify-clean|restore-full|fail-corrupt|fail-nosigner|incremental-send|incremental-restore-chain|teardown}" >&2; exit 2 ;;
esac
