#!/usr/bin/env bats
# Verify-only restore REHEARSAL over a REAL ssh TRANSPORT — the read-only remote
# access (SshTarget.listdir + SshTarget.get) of qdistro_backup_service.py
# `rehearse` (06-backup-dr §3.3). The sibling backup-rehearse-e2e.bats proves the
# rehearsal over a REAL btrfs incremental chain but with a LocalDirTarget
# "remote" (a plain dir — no network), so the rehearsal's SSH remote-access paths
# are never exercised there. This suite points the rehearsal's `remote` at a REAL
# sshd inside the VM (localhost, throwaway keypair authorised for root) so the
# manifest discovery (listdir = `ssh <host> find ...`) and the manifest/blob
# pulls (get = `rsync -e <ssh> <host>:<path> <local>`) really traverse ssh. This
# closes the rehearsal's SSH-transport residual — the read pull-path counterpart
# of the driver's push lane proven by backup-ssh-e2e.bats. btrfs
# snapshot/send/send -p/receive are real too (loopback fs).
#
# Heavy lifting: tests/integration/vm/probes/backup-rehearse-ssh-probe.sh (staged
# to /root by fresh-vm-bootstrap.sh). Order is load-bearing: setup_file stands up
# the loopback + source subvol + keys + sshd + backup.conf + 2 real ssh-pushed
# backup runs (a real incremental chain); later tests assume it; teardown_file
# stops sshd + unmounts.

load helpers

PROBE="/root/backup-rehearse-ssh-probe.sh"

setup_file() {
    vm_run "[ -f $PROBE ]"
    assert_success || fail_loud "probe not staged at $PROBE (fresh-vm-bootstrap.sh probes step)"
    vm_run "command -v mkfs.btrfs >/dev/null"
    assert_success || fail_loud "mkfs.btrfs not available in the VM"
    vm_run "command -v rsync >/dev/null"
    assert_success || fail_loud "rsync not installed in the VM — the rehearsal pulls blobs with it over ssh"
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

@test "rehearse-ssh: verify-only pulls a REAL 2-manifest chain DOWN over ssh, passes, and leaves the remote unchanged" {
    vm_run "bash $PROBE verify-only"
    assert_success
    assert_output_contains "PASS: verify-only"
}

@test "rehearse-ssh: --rehearse-receive pulls the chain over ssh and does a REAL btrfs receive into a throwaway scratch subvol" {
    vm_run "bash $PROBE receive"
    assert_success
    assert_output_contains "PASS: receive"
}

@test "rehearse-ssh: a corrupt ANCESTOR blob on the ssh remote still FAILS (full-chain coverage over the real transport)" {
    vm_run "bash $PROBE corrupt-ancestor"
    assert_success
    assert_output_contains "PASS: corrupt-ancestor"
}

@test "rehearse-ssh: an ABSENT ssh remote yields an empty listdir and fails loudly (no crash/hang)" {
    vm_run "bash $PROBE missing-remote"
    assert_success
    assert_output_contains "PASS: missing-remote"
}
