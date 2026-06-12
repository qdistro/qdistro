#!/usr/bin/env bats
# Backup DR e2e — the REAL `btrfs send -p | btrfs receive` half (06-backup-dr
# §4/§6, finding F1). The host lane tests/integration/backup-e2e.bats proves
# the orchestration + integrity gates with tar-backed btrfs stubs (same argv)
# plus REAL rage and signed manifests; this suite swaps in real btrfs
# send/receive on a real loopback filesystem — the half the headless dev host
# could not run (needs CAP_SYS_ADMIN + btrfs).
#
# The heavy lifting lives in tests/integration/vm/probes/backup-btrfs-probe.sh
# (staged to /root by fresh-vm-bootstrap.sh, host-runnable too). It builds its
# own btrfs loopback so the suite does not depend on the VM's root layout, and
# runs as root (loop mount + subvolume + send/receive are privileged).
#
# Order is load-bearing: setup_file builds the loopback + source subvols + RO
# snapshots + keys; the first test does the backup and later tests assume it;
# teardown_file unmounts and removes it.

load helpers

PROBE="/root/backup-btrfs-probe.sh"

# NB: vm_run() calls bats `run` internally (sets $status/$output itself), so it
# must be invoked BARE — wrapping it in another `run` captures nothing. Matches
# templates-state-snapshot.bats / pwd-print-recall.bats.

setup_file() {
    vm_run "[ -f $PROBE ]"
    assert_success || fail_loud "probe not staged at $PROBE (fresh-vm-bootstrap.sh probes step)"
    vm_run "command -v mkfs.btrfs >/dev/null"
    assert_success || fail_loud "mkfs.btrfs not available in the VM"
    # Backup encryption needs an age-compatible tool. Production VMs ship
    # rage-encryption (install-deps.sh / install-snapshots-for-vm.sh), so a
    # missing encryptor is a real packaging regression, not a reason to skip —
    # fail loudly (helpers.bash policy: silent skips have masked missing-dep
    # regressions before). The probe accepts rage OR age (same -e/-R/-d/-i CLI).
    vm_run "command -v rage >/dev/null || command -v age >/dev/null"
    assert_success || fail_loud "neither rage nor age installed in the VM — backups cannot encrypt (expected rage-encryption from install-deps.sh)"
    vm_run "bash $PROBE setup"
    assert_success
    assert_output_contains "PASS: setup"
}

teardown_file() {
    vm_run "bash $PROBE teardown" || true
}

@test "backup-btrfs: full backup writes age blobs + signed manifest via REAL btrfs send" {
    vm_run "bash $PROBE full-backup"
    assert_success
    assert_output_contains "PASS: full-backup"
}

@test "backup-btrfs: verify passes on the clean signed chain" {
    vm_run "bash $PROBE verify-clean"
    assert_success
    assert_output_contains "PASS: verify-clean"
}

@test "backup-btrfs: restore via REAL btrfs receive yields a subvolume that diffs equal to source" {
    vm_run "bash $PROBE restore-full"
    assert_success
    assert_output_contains "PASS: restore-full"
}

@test "backup-btrfs: a corrupted blob FAILS verify (sha256) and ABORTS restore (never received)" {
    vm_run "bash $PROBE fail-corrupt"
    assert_success
    assert_output_contains "PASS: fail-corrupt"
}

@test "backup-btrfs: verify/restore REFUSE without --allowed-signers (fail-closed)" {
    vm_run "bash $PROBE fail-nosigner"
    assert_success
    assert_output_contains "PASS: fail-nosigner"
}

@test "backup-btrfs: incremental 'btrfs send -p' produces a delta blob with real parent lineage" {
    vm_run "bash $PROBE incremental-send"
    assert_success
    assert_output_contains "PASS: incremental-send"
}

@test "backup-btrfs: incremental chain restore yields the seq1 state under the stable name (F-A fixed)" {
    # Regression guard for 06-backup-dr-draft.md §6 finding F-A: the CLI now
    # receives each incremental ancestor into a per-seq staging subvol and the
    # final seq straight into --dest, so a 2-seq chain restores cleanly and the
    # result diffs equal to the seq1 source (the host tar stub masked the old
    # same-name receive collision).
    vm_run "bash $PROBE incremental-restore-chain"
    assert_success
    assert_output_contains "PASS: incremental-restore-chain"
}
