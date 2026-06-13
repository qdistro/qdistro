#!/usr/bin/env bats
# Backup-SERVICE SSH TRANSPORT e2e — the REAL ssh/rsync push path of
# qdistro_backup_service.py's SshTarget (P2a residual). The sibling
# backup-driver-e2e.bats proves the driver with REAL btrfs but a LocalDirTarget
# "remote" (a plain dir, no network); this suite points the driver's SshTarget
# at a REAL sshd inside the VM (localhost, throwaway keypair authorised for
# root) so the push really goes over rsync-over-ssh and the readback
# (sha256sum) really runs on the remote via ssh. btrfs snapshot/send/receive
# are real too (loopback fs) — the fully-real end-to-end transport path.
#
# Heavy lifting: tests/integration/vm/probes/backup-ssh-probe.sh (staged to
# /root by fresh-vm-bootstrap.sh). Order is load-bearing: setup_file stands up
# the loopback + source subvol + keys + sshd + backup.conf; run-full does the
# seq0 ssh-push and later tests assume it; teardown_file stops sshd + unmounts.

load helpers

PROBE="/root/backup-ssh-probe.sh"

setup_file() {
    vm_run "[ -f $PROBE ]"
    assert_success || fail_loud "probe not staged at $PROBE (fresh-vm-bootstrap.sh probes step)"
    vm_run "command -v mkfs.btrfs >/dev/null"
    assert_success || fail_loud "mkfs.btrfs not available in the VM"
    vm_run "command -v rsync >/dev/null"
    assert_success || fail_loud "rsync not installed in the VM — the SshTarget pushes with it (expected from install-deps.sh)"
    vm_run "command -v ssh >/dev/null && (command -v sshd >/dev/null || command -v /usr/sbin/sshd >/dev/null)"
    assert_success || fail_loud "ssh client or sshd not installed in the VM — the SSH transport lane needs both"
    vm_run "command -v rage >/dev/null || command -v age >/dev/null"
    assert_success || fail_loud "neither rage nor age installed in the VM — backups cannot encrypt"
    vm_run "bash $PROBE setup"
    assert_success
    assert_output_contains "PASS: setup"
}

teardown_file() {
    vm_run "bash $PROBE teardown" || true
}

@test "backup-ssh: a full run pushes blobs+manifest+sig over rsync-over-ssh and the ssh readback advances state" {
    vm_run "bash $PROBE run-full"
    assert_success
    assert_output_contains "PASS: run-full"
}

@test "backup-ssh: a corrupt-on-push run is REJECTED by the over-ssh readback gate (state not advanced)" {
    vm_run "bash $PROBE readback-guard"
    assert_success
    assert_output_contains "PASS: readback-guard"
}

@test "backup-ssh: verify passes on the chain fetched from the ssh remote" {
    vm_run "bash $PROBE verify"
    assert_success
    assert_output_contains "PASS: verify"
}

@test "backup-ssh: restore from the ssh remote yields a subvolume equal to source" {
    vm_run "bash $PROBE restore"
    assert_success
    assert_output_contains "PASS: restore"
}

@test "backup-ssh: a second run pushes a real send -p blob over ssh and the chain restores to seq1" {
    vm_run "bash $PROBE incremental"
    assert_success
    assert_output_contains "PASS: incremental"
}
