#!/bin/bash
# backup-rehearse-ssh-probe — the REAL ssh/rsync READ-ONLY half of the
# verify-only restore REHEARSAL (qdistro_backup_service.py `rehearse`,
# 06-backup-dr §3.3). The sibling backup-rehearse-probe.sh proves the rehearsal
# over a REAL btrfs incremental chain but with a LocalDirTarget "remote" (a
# plain directory — no network), so the rehearsal's read-only remote access
# (SshTarget.listdir + SshTarget.get) is NEVER exercised there. This probe
# instead points the rehearsal's `remote` at a REAL sshd reachable inside the VM
# (localhost, a throwaway keypair authorised for root) so the rehearsal's manifest
# discovery (listdir = `ssh <host> find ...`) and its manifest/blob pulls
# (get = `rsync -e <ssh> <host>:<path> <local>`) really traverse ssh. This is
# the residual the backup DRIVER's SSH lane (backup-ssh-probe.sh) already closed
# for the PUSH path; here we close it for the rehearsal's PULL path.
#
# The whole point of the rehearsal is that it NEVER writes the remote — so this
# lane also asserts the SSH remote is byte-for-byte unchanged across the
# rehearsal (a read-only contract over a real transport, not just over a local
# dir). btrfs snapshot/send/send -p/receive are real too (loopback fs).
#
# Runs as root INSIDE the test VM (staged to /root by fresh-vm-bootstrap.sh).
# Builds its own btrfs loopback so it does not depend on the VM root layout and
# stands up its own sshd on a throwaway port + key so it does not perturb the
# VM's system sshd. Every PASS line is asserted by backup-rehearse-ssh-e2e.bats.
set -u

WORK=/tmp/qd-backup-rehearse-ssh
IMG="$WORK/btrfs.img"
MNT="$WORK/mnt"
SSHDIR="$WORK/ssh"          # throwaway host key + client key + sshd config live here
SSH_PORT=2223               # loopback-only listener (distinct from the driver lane's 2222)
SRC_ROOT=/root/qdistro-src/qdistro/snapshots
DRV="${QDISTRO_BACKUP_SVC:-$SRC_ROOT/qdistro_backup_service.py}"
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

# The client ssh command the rehearsal hands its SshTarget: a fixed throwaway
# identity + a non-default port + no host-key prompting (the host key is
# regenerated per run, so a known_hosts pin would be wrong). This is exactly the
# production shape (`ssh -i <key> -p <port> -o ...`) — the rehearsal passes it
# verbatim to BOTH `ssh <host> find` (listdir) and rsync's `-e` (get).
SSH_CMD() {
    echo "ssh -i $SSHDIR/client_id -p $SSH_PORT" \
         "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null" \
         "-o BatchMode=yes -o ConnectTimeout=10 -o LogLevel=ERROR"
}

# The driver PUSH still goes over the same real ssh transport so the chain we
# rehearse is itself one that landed over ssh (end-to-end realism). The push is
# not the subject under test here (the driver lane covers it) but using a
# LocalDirTarget remote for setup would mean the rehearsal could never build an
# SshTarget — the `remote` spec is what selects the target class.
DRV_RUN() { python3 "$DRV" run --config "$CONF" --ssh-cmd "$(SSH_CMD)" "$@"; }
REHEARSE() { python3 "$DRV" rehearse --config "$CONF" --ssh-cmd "$(SSH_CMD)" "$@"; }

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
    ssh-keygen -t ed25519 -N "" -C backup-rehearse-ssh-probe -f "$SSHDIR/client_id" \
        >/dev/null 2>&1 || fail setup "client keygen"
    install -d -m 700 /root/.ssh
    # Tag the line so teardown can remove exactly ours (idempotent re-runs).
    grep -q 'backup-rehearse-ssh-probe' /root/.ssh/authorized_keys 2>/dev/null \
        && sed -i '/backup-rehearse-ssh-probe/d' /root/.ssh/authorized_keys
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
    "$SSHD" -f "$SSHDIR/sshd_config" >/dev/null 2>"$WORK/sshd.err" &
    # sshd daemonises by default (writes the pidfile); give it a moment.
    local i
    for ((i=0; i<30; i++)); do
        [ -s "$SSHD_PIDFILE" ] && break
        sleep 0.2
    done
    [ -s "$SSHD_PIDFILE" ] || fail setup "sshd did not start (see $WORK/sshd.err: $(cat "$WORK/sshd.err" 2>/dev/null))"
    # Prove the loop closes BEFORE the rehearsal relies on it: a real ssh
    # round-trip to root@127.0.0.1 over the throwaway key/port must succeed.
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
    sed -i '/backup-rehearse-ssh-probe/d' /root/.ssh/authorized_keys 2>/dev/null || true
}

# Snapshot the SSH remote tree's content fingerprint (sorted name+sha256 of
# every regular file) so a test can assert the rehearsal left it byte-for-byte
# unchanged — the read-only contract, proven over the real transport.
remote_fingerprint() {
    ( cd "$MNT/remote_ssh" 2>/dev/null && \
      find . -maxdepth 1 -type f -printf '%f\n' | LC_ALL=C sort | \
      while read -r f; do printf '%s ' "$f"; sha256sum "$f" | cut -d' ' -f1; done )
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
    # btrfs so the rehearsal's listdir/get genuinely traverse ssh to the same
    # box. allowed_signers + sign_identity are MANDATORY for the rehearsal.
    cat > "$CONF" <<EOF
host_id = "rehearsesshhost"
recipients = "$WORK/recipients.txt"
sign_key = "$WORK/sign"
allowed_signers = "$WORK/allowed_signers"
sign_identity = "owner@qdistro"
remote = "root@127.0.0.1:$MNT/remote_ssh"
state_dir = "$MNT/state"
[[subvol]]
name = "data"
source = "$MNT/data"
EOF
    # Two driver runs over the REAL ssh transport so the rehearsal exercises a
    # real incremental CHAIN (a full + an incremental) pulled back over ssh —
    # exactly where verifying only the newest blob would false-green.
    DRV_RUN --now 1700000000 >/dev/null || fail setup "seq0 backup (ssh push) failed"
    [ -f "$MNT/remote_ssh/manifest-0.json" ] || fail setup "seq0 manifest not on ssh remote"
    echo "added in seq1" > "$MNT/data/newfile"; sync
    DRV_RUN --now 1700000100 >/dev/null || fail setup "seq1 backup (ssh push) failed"
    grep -q '"seq": 1' "$MNT/state/state.json" || fail setup "state not at seq1"
    [ -f "$MNT/remote_ssh/manifest-1.json" ] || fail setup "seq1 manifest not on ssh remote"
    pass setup
}

cmd_verify_only() {
    # The always-on core over a REAL chain pulled DOWN over ssh (no receive).
    # This is the test that actually drives SshTarget.listdir (manifest
    # discovery) + SshTarget.get (manifest + blob pulls) over the real transport.
    local before after
    before="$(remote_fingerprint)"

    REHEARSE | grep -q '"rehearsal": "ok"' || fail verify-only "rehearsal not ok over ssh"
    REHEARSE | grep -q '"manifests": 2' \
        || fail verify-only "rehearsal did not span 2 manifests pulled over ssh"
    # READ-ONLY: the rehearsal must not advance the seq anchor.
    grep -q '"seq": 1' "$MNT/state/state.json" || fail verify-only "seq anchor moved"
    # READ-ONLY over the REAL transport: the ssh remote tree is byte-for-byte
    # unchanged (SshTarget.listdir/get never write the remote).
    after="$(remote_fingerprint)"
    [ "$before" = "$after" ] \
        || fail verify-only "ssh remote changed during a read-only rehearsal (before/after fingerprint differ)"
    pass verify-only
}

cmd_receive() {
    # OPTIONAL dry-run receive of the full chain (pulled over ssh) into a
    # throwaway scratch subvol under state_dir, then DESTROYED. Real `btrfs
    # receive`; blobs come down via SshTarget.get over ssh.
    local before after
    before="$(remote_fingerprint)"

    REHEARSE --rehearse-receive --rehearse-subvol data \
        --identity-file "$WORK/id.txt" | grep -q '"received": true' \
        || fail receive "rehearse-receive (ssh pull) did not report received"
    # The scratch subvol tree must be GONE afterwards (throwaway, destroyed).
    [ ! -e "$MNT/state/rehearsal-scratch" ] \
        || fail receive "rehearsal scratch subvol not cleaned up"
    # Live state untouched: seq anchor still 1.
    grep -q '"seq": 1' "$MNT/state/state.json" || fail receive "seq anchor moved"
    # The ssh remote is still byte-for-byte unchanged after a receive rehearsal.
    after="$(remote_fingerprint)"
    [ "$before" = "$after" ] \
        || fail receive "ssh remote changed during a read-only receive rehearsal"
    pass receive
}

cmd_corrupt_ancestor() {
    # Corrupt the seq0 ANCESTOR full blob ON THE SSH REMOTE; the newest
    # manifest/blob is intact. The rehearsal pulls the ancestor down over ssh and
    # must STILL fail (full-chain coverage over the real transport), not
    # false-green. We restore the byte afterwards so later re-runs are clean.
    local blob="$MNT/remote_ssh/data-0.btrfs.age"
    [ -f "$blob" ] || fail corrupt-ancestor "seq0 ancestor blob missing on ssh remote"
    cp "$blob" "$WORK/data-0.orig" || fail corrupt-ancestor "could not snapshot ancestor blob"
    printf 'CORRUPT' >> "$blob"
    # Capture stderr so we can assert it fails for the RIGHT reason (a blob
    # problem on the pulled ancestor), not a spurious transport/sig error — the
    # sibling backup-rehearse probe's corrupt-ancestor only checks nonzero exit;
    # here we pin the cause so a regression cannot false-green off any failure.
    local out
    if out=$(REHEARSE 2>&1); then
        cp "$WORK/data-0.orig" "$blob"
        fail corrupt-ancestor "rehearsal passed despite a corrupt ANCESTOR blob pulled over ssh"
    fi
    cp "$WORK/data-0.orig" "$blob" || fail corrupt-ancestor "could not restore ancestor blob"
    printf '%s\n' "$out" | grep -qiE "blob problem|REHEARSAL BLOB PROBLEM" \
        || fail corrupt-ancestor "rehearsal failed but NOT via the blob-verification gate; output: $out"
    pass corrupt-ancestor
}

cmd_missing_remote() {
    # SshTarget.listdir over an ABSENT remote dir must yield [] (the rehearsal
    # then reports "no manifests at the remote", not a crash). Exercises the
    # listdir failure branch over the real ssh transport (find on a missing path
    # exits nonzero → []). Isolated: its own remote path that does not exist.
    local W2="$WORK/missing"
    rm -rf "$W2"; mkdir -p "$W2"
    cat > "$W2/backup.conf" <<EOF
host_id = "rehearsesshhost"
recipients = "$WORK/recipients.txt"
sign_key = "$WORK/sign"
allowed_signers = "$WORK/allowed_signers"
sign_identity = "owner@qdistro"
remote = "root@127.0.0.1:$MNT/does_not_exist_$$"
state_dir = "$MNT/state"
[[subvol]]
name = "data"
source = "$MNT/data"
EOF
    # MUST fail with the empty-remote message — proving listdir over ssh returned
    # [] for an absent dir rather than blowing up or hanging.
    if python3 "$DRV" rehearse --config "$W2/backup.conf" --ssh-cmd "$(SSH_CMD)" \
            >"$W2/out" 2>&1; then
        fail missing-remote "rehearsal passed against an absent ssh remote"
    fi
    grep -q "no manifests at the remote" "$W2/out" \
        || fail missing-remote "rehearsal failed but NOT via the empty-listdir path; output: $(cat "$W2/out")"
    pass missing-remote
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
    verify-only) cmd_verify_only ;;
    receive) cmd_receive ;;
    corrupt-ancestor) cmd_corrupt_ancestor ;;
    missing-remote) cmd_missing_remote ;;
    teardown) cmd_teardown ;;
    *) echo "usage: $0 {setup|verify-only|receive|corrupt-ancestor|missing-remote|teardown}" >&2; exit 2 ;;
esac
