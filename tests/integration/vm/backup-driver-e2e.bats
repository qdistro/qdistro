#!/usr/bin/env bats
# Backup-SERVICE driver e2e — the REAL-btrfs half of qdistro_backup_service.py
# (P2a). The host lane tests/integration/backup-e2e.bats proves the driver
# orchestration with tar-stubbed btrfs + a cp-stub snapshot + a local-dir
# remote; this suite swaps in real `btrfs subvolume snapshot -r`,
# `btrfs subvolume create` (the metadata collector subvol), and real
# `btrfs send` / `send -p` on a loopback filesystem — the half the headless dev
# host cannot run (CAP_SYS_ADMIN + btrfs). It directly validates the review fix
# that the collector stage must be a real subvolume.
#
# Heavy lifting: tests/integration/vm/probes/backup-driver-probe.sh (staged to
# /root by fresh-vm-bootstrap.sh). Order is load-bearing: setup_file builds the
# loopback + source subvol + collector config + keys + backup.conf; run-full
# does the seq0 backup and later tests assume it; teardown_file unmounts.

load helpers

PROBE="/root/backup-driver-probe.sh"

setup_file() {
    vm_run "[ -f $PROBE ]"
    assert_success || fail_loud "probe not staged at $PROBE (fresh-vm-bootstrap.sh probes step)"
    vm_run "command -v mkfs.btrfs >/dev/null"
    assert_success || fail_loud "mkfs.btrfs not available in the VM"
    # The collector stages its config set with rsync; a missing rsync is a real
    # packaging regression (install-deps.sh / qdistro-bootstrap.sh ship it), not
    # a reason to skip — fail loudly (helpers.bash policy).
    vm_run "command -v rsync >/dev/null"
    assert_success || fail_loud "rsync not installed in the VM — the backup collector needs it (expected from install-deps.sh)"
    vm_run "command -v rage >/dev/null || command -v age >/dev/null"
    assert_success || fail_loud "neither rage nor age installed in the VM — backups cannot encrypt"
    vm_run "bash $PROBE setup"
    assert_success
    assert_output_contains "PASS: setup"
}

teardown_file() {
    vm_run "bash $PROBE teardown" || true
}

@test "backup-driver: a full run takes a REAL RO snapshot + collector subvol and signs a manifest" {
    vm_run "bash $PROBE run-full"
    assert_success
    assert_output_contains "PASS: run-full"
}

@test "backup-driver: verify passes on the clean signed chain" {
    vm_run "bash $PROBE verify"
    assert_success
    assert_output_contains "PASS: verify"
}

@test "backup-driver: restore via REAL btrfs receive yields a subvolume equal to source" {
    vm_run "bash $PROBE restore"
    assert_success
    assert_output_contains "PASS: restore"
}

@test "backup-driver: the metadata collector subvol restores with its collected config bytes" {
    vm_run "bash $PROBE restore-collector"
    assert_success
    assert_output_contains "PASS: restore-collector"
}

@test "backup-driver: a second run does a REAL 'btrfs send -p', prunes seq0, and the chain restores to seq1" {
    vm_run "bash $PROBE incremental"
    assert_success
    assert_output_contains "PASS: incremental"
}
