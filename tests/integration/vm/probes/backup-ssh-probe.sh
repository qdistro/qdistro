#!/bin/bash
# backup-ssh-probe — the REAL ssh/rsync TRANSPORT half of the daily backup
# SERVICE driver (qdistro_backup_service.py, P2a SshTarget). The sibling
# backup-driver-probe.sh proves the driver with REAL btrfs send/receive but a
# LocalDirTarget "remote" (a plain directory — no network); this probe instead
# points the driver's SshTarget at a REAL sshd reachable inside the VM
# (localhost, a throwaway keypair authorised for root) so the push really goes
# over rsync-over-ssh + the readback (sha256sum) really runs on the remote via
# ssh. btrfs snapshot/send/receive are also real (loopback fs), so this is the
# fully-real end-to-end path: snapshot -> engine -> rsync/ssh push -> ssh
# readback -> atomic state advance -> chain restore.
#
# Runs as root INSIDE the test VM (staged to /root by fresh-vm-bootstrap.sh).
# Builds its own btrfs loopback so it does not depend on the VM root layout and
# stands up its own sshd on a throwaway port + key so it does not perturb the
# VM's system sshd. Every PASS line is asserted by backup-ssh-e2e.bats.
set -u

WORK=/tmp/qd-backup-ssh
IMG="$WORK/btrfs.img"
MNT="$WORK/mnt"
SSHDIR="$WORK/ssh"          # throwaway host key + client key + sshd config live here
SSH_PORT=2222               # loopback-only listener for this probe
SRC_ROOT=/root/qdistro-src/qdistro/snapshots
DRV="${QDISTRO_BACKUP_SVC:-$SRC_ROOT/qdistro_backup_service.py}"
BK="${QDISTRO_BACKUP_CLI:-$SRC_ROOT/qdistro_backup_cli.py}"
CONF="$WORK/backup.conf"
SSHD_PIDFILE="$WORK/sshd.pid"

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

# The client ssh command the driver hands its SshTarget: a fixed throwaway
# identity + a non-default port + no host-key prompting (the host key is
# regenerated per run, so a known_hosts pin would be wrong). This is exactly
# the production shape (`ssh -i <key> -p <port> -o ...`) — the driver passes it
# verbatim to BOTH `ssh <host> <op>` (mkdir/mv/sha256sum) and rsync's `-e`.
SSH_CMD() {
    echo "ssh -i $SSHDIR/client_id -p $SSH_PORT" \
         "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null" \
         "-o BatchMode=yes -o ConnectTimeout=10 -o LogLevel=ERROR"
}

DRV_RUN() { python3 "$DRV" run --config "$CONF" --ssh-cmd "$(SSH_CMD)" "$@"; }
VERIFY() { python3 "$BK" verify --out-dir "$MNT/remote_ssh" \
    --allowed-signers "$WORK/allowed_signers" --identity owner@qdistro "$@"; }
RESTORE() { local sv="$1" dest="$2"; shift 2
    python3 "$BK" restore --out-dir "$MNT/remote_ssh" --subvol "$sv" --dest "$dest" \
        --identity-file "$WORK/id.txt" --allowed-signers "$WORK/allowed_signers" \
        --identity owner@qdistro "$@"; }

start_sshd() {
    command -v sshd >/dev/null 2>&1 || command -v /usr/sbin/sshd >/dev/null 2>&1 \
        || fail setup "sshd binary not found"
    local SSHD; SSHD="$(command -v sshd || echo /usr/sbin/sshd)"
    mkdir -p "$SSHDIR"
    chmod 700 "$SSHDIR"
    # Throwaway host key + an unprivileged-friendly sshd config bound to
    # loopback only. PidFile lets us tear it down by exact pid.
    ssh-keygen -t ed25519 -N "" -f "$SSHDIR/host_ed25519" >/dev/null 2>&1 \
        || fail setup "host keygen"
    # Client identity, authorised for root@localhost via root's authorized_keys.
    ssh-keygen -t ed25519 -N "" -C backup-ssh-probe -f "$SSHDIR/client_id" \
        >/dev/null 2>&1 || fail setup "client keygen"
    install -d -m 700 /root/.ssh
    # Tag the line so teardown can remove exactly ours (idempotent re-runs).
    grep -q 'backup-ssh-probe' /root/.ssh/authorized_keys 2>/dev/null \
        && sed -i '/backup-ssh-probe/d' /root/.ssh/authorized_keys
    cat "$SSHDIR/client_id.pub" >> /root/.ssh/authorized_keys
    chmod 600 /root/.ssh/authorized_keys
    cat > "$SSHDIR/sshd_config" <<EOF
Port $SSH_PORT
ListenAddress 127.0.0.1
HostKey $SSHDIR/host_ed25519
PidFile $SSHD_PIDFILE
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
UsePAM no
AuthorizedKeysFile /root/.ssh/authorized_keys
StrictModes no
LogLevel ERROR
EOF
    # -e logs to stderr; background it. sshd needs an absolute config path.
    "$SSHD" -f "$SSHDIR/sshd_config" >/dev/null 2>"$WORK/sshd.err" &
    # sshd daemonises by default (writes the pidfile); give it a moment.
    local i
    for ((i=0; i<30; i++)); do
        [ -s "$SSHD_PIDFILE" ] && break
        sleep 0.2
    done
    [ -s "$SSHD_PIDFILE" ] || fail setup "sshd did not start (see $WORK/sshd.err: $(cat "$WORK/sshd.err" 2>/dev/null))"
    # Prove the loop closes BEFORE the driver relies on it: a real ssh round-trip
    # to root@127.0.0.1 over the throwaway key/port must succeed.
    local probe
    probe=$($(SSH_CMD) root@127.0.0.1 "echo SSH_OK" 2>"$WORK/sshprobe.err")
    [ "$probe" = "SSH_OK" ] || fail setup "ssh round-trip to root@127.0.0.1 failed: $(cat "$WORK/sshprobe.err" 2>/dev/null)"
}

stop_sshd() {
    if [ -s "$SSHD_PIDFILE" ]; then
        kill "$(cat "$SSHD_PIDFILE")" 2>/dev/null || true
    fi
    # Also sweep any stray sshd we spawned on our throwaway config (belt+braces).
    pkill -f "sshd -f $SSHDIR/sshd_config" 2>/dev/null || true
    sed -i '/backup-ssh-probe/d' /root/.ssh/authorized_keys 2>/dev/null || true
}

cmd_setup() {
    command -v mkfs.btrfs >/dev/null 2>&1 || fail setup "mkfs.btrfs not installed"
    command -v rsync >/dev/null 2>&1 || fail setup "rsync not installed"
    command -v ssh >/dev/null 2>&1 || fail setup "ssh client not installed"
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

    start_sshd

    # remote is an SSH spec (root@127.0.0.1:<path>) so make_target() builds an
    # SshTarget — the whole point of this lane. The path lives on the loopback
    # btrfs so the readback genuinely traverses ssh to the same box.
    cat > "$CONF" <<EOF
host_id = "sshhost"
recipients = "$WORK/recipients.txt"
sign_key = "$WORK/sign"
remote = "root@127.0.0.1:$MNT/remote_ssh"
state_dir = "$MNT/state"
[[subvol]]
name = "data"
source = "$MNT/data"
EOF
    pass setup
}

cmd_run_full() {
    DRV_RUN --now 1700000000 || fail run-full "driver seq0 (ssh transport) failed"
    # The blobs/manifest/sig must have landed on the remote via rsync-over-ssh.
    [ -f "$MNT/remote_ssh/data-0.btrfs.age" ] || fail run-full "no data blob on ssh remote"
    [ -f "$MNT/remote_ssh/manifest-0.json" ] || fail run-full "no manifest on ssh remote"
    [ -f "$MNT/remote_ssh/manifest-0.json.sig" ] || fail run-full "no signature on ssh remote"
    # No .upload.tmp commit-marker leftovers — the ssh `mv` published the manifest.
    ls "$MNT/remote_ssh"/*.upload.tmp >/dev/null 2>&1 \
        && fail run-full "stray .upload.tmp leftover (ssh commit mv did not run)"
    grep -q '"seq": 0' "$MNT/state/state.json" \
        || fail run-full "state not advanced (remote readback over ssh must have passed)"
    grep -q "the quick brown fox" "$MNT/remote_ssh/data-0.btrfs.age" \
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

cmd_incremental() {
    # A second run pushes a real `btrfs send -p` blob over ssh, the readback runs
    # over ssh, state advances to seq1, and the full chain restores to seq1.
    echo "added in seq1" > "$MNT/data/newfile"; sync
    DRV_RUN --now 1700000100 || fail incremental "driver seq1 (ssh transport) failed"
    [ -f "$MNT/remote_ssh/data-1.btrfs.age" ] || fail incremental "no seq1 blob on ssh remote"
    grep -q '"parent_blob":"data-0.btrfs.age"' "$MNT/remote_ssh/manifest-1.json" \
        || fail incremental "seq1 manifest missing real parent lineage"
    grep -q '"seq": 1' "$MNT/state/state.json" || fail incremental "state not advanced to 1"
    VERIFY --checkpoint-seq 1 | grep -q "2 manifest" \
        || fail incremental "chain verify did not span 2 manifests"
    local dest="$MNT/incr-restore"
    btrfs subvolume delete "$dest/data" >/dev/null 2>&1 || true
    rm -rf "$dest"; mkdir -p "$dest"
    RESTORE data "$dest" --checkpoint-seq 1 || fail incremental "chain restore failed"
    [ -f "$dest/data/newfile" ] || fail incremental "restored state is not seq1 (no newfile)"
    diff -r "$MNT/data" "$dest/data" || fail incremental "chain restore != seq1 source"
    pass incremental
}

cmd_readback_guard() {
    # TRUE negative control (codex review): drive a REAL driver run whose
    # rsync-over-ssh push corrupts the blob ON THE REMOTE, so the driver's
    # over-ssh sha256 READBACK must detect the mismatch and REFUSE to advance
    # state. This exercises cmd_run()'s actual state-advance gate (not just that
    # `sha256sum` is content-sensitive) — if cmd_run stopped enforcing the
    # readback, this would FAIL.
    #
    # Isolated from the main chain: its own loopback source/state/remote so a
    # rejected run cannot perturb the seq0/seq1 chain the other tests assert.
    local W2="$WORK/neg"
    rm -rf "$W2"; mkdir -p "$W2"
    btrfs subvolume delete "$MNT/negdata" >/dev/null 2>&1 || true
    btrfs subvolume create "$MNT/negdata" >/dev/null || fail readback-guard "negdata subvol"
    echo "negative-control payload" > "$MNT/negdata/file1"; sync

    # A corrupting rsync: do the real push, THEN flip a byte on the just-pushed
    # remote file (localhost, same fs) so the over-ssh readback hash won't match
    # the local staging hash. The driver hands this to its SshTarget verbatim.
    local CR="$W2/rsync-corrupt"
    cat > "$CR" <<'EOS'
#!/bin/bash
# rsync-corrupt -e <ssh> <src> <host>:<dst> : normal push, then corrupt the
# remote copy so the driver's readback over ssh sees a mismatch.
real_rsync=$(command -v rsync)
"$real_rsync" "$@" || exit $?
dst="${@: -1}"            # host:/path
path="${dst#*:}"          # strip 'host:' (localhost remote == local path)
# Append a byte to whatever was just pushed (blob or manifest .upload.tmp);
# any single mismatch trips the readback gate.
printf 'CORRUPT' >> "$path"
exit 0
EOS
    chmod 0755 "$CR"

    local NEG_CONF="$W2/backup.conf"
    cat > "$NEG_CONF" <<EOF
host_id = "neghost"
recipients = "$WORK/recipients.txt"
sign_key = "$WORK/sign"
remote = "root@127.0.0.1:$MNT/neg_remote"
state_dir = "$MNT/neg_state"
[[subvol]]
name = "negdata"
source = "$MNT/negdata"
EOF

    # Run the driver; it MUST fail (nonzero) because the over-ssh readback
    # mismatches the corrupted remote bytes.
    if python3 "$DRV" run --config "$NEG_CONF" --ssh-cmd "$(SSH_CMD)" \
            --rsync-cmd "$CR" --now 1700000200 >"$W2/run.out" 2>&1; then
        fail readback-guard "driver advanced state despite a corrupt remote (readback gate not enforced); output: $(cat "$W2/run.out")"
    fi
    grep -qi "readback mismatch" "$W2/run.out" \
        || fail readback-guard "driver failed but NOT via the remote-readback gate; output: $(cat "$W2/run.out")"
    # State must NOT have advanced — no state.json at seq 0 (fresh chain stays
    # at last_seq -1, i.e. no committed state file with "seq": 0).
    if [ -f "$MNT/neg_state/state.json" ] && grep -q '"seq": 0' "$MNT/neg_state/state.json"; then
        fail readback-guard "state advanced to seq0 despite the readback mismatch"
    fi
    btrfs subvolume delete "$MNT/negdata" >/dev/null 2>&1 || true
    pass readback-guard
}

cmd_teardown() {
    stop_sshd
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
    incremental) cmd_incremental ;;
    readback-guard) cmd_readback_guard ;;
    teardown) cmd_teardown ;;
    *) echo "usage: $0 {setup|run-full|verify|restore|incremental|readback-guard|teardown}" >&2; exit 2 ;;
esac
