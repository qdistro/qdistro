#!/usr/bin/env bats
# Task 05 state-snapshot / rollback invariants on a REAL btrfs filesystem
# (fableplan2 task 05; doc/05; doc/filesystem.md). The host unit suites
# (test_snap_swap.py, test_state_snapshot.py) prove the LOGIC on tmpfs; this
# suite proves the btrfs-specific code the headless host could not run:
# mechanism selection on btrfs, the read-only `btrfs subvolume snapshot -r`,
# the RENAME_EXCHANGE swap of subvolumes, and the crash-recovery journal.
#
# The heavy lifting lives in tests/integration/vm/probes/
# templates-state-snapshot-probe.sh (staged to /root by fresh-vm-bootstrap.sh,
# host-runnable too). It builds its own btrfs loopback so the suite does not
# depend on the VM's root layout. Runs as root (loop mount + subvolume ops are
# privileged; production snapshots state as root/broker).
#
# Order is load-bearing: setup_file builds + mounts the loopback; the
# scenarios assume it; teardown_file unmounts and removes it.

load helpers

PROBE="/root/templates-state-snapshot-probe.sh"

# NB: vm_run() calls bats `run` internally (it sets $status/$output itself), so
# it must be invoked BARE — wrapping it in another `run` captures nothing. This
# matches the working suites (pwd-print-recall.bats etc.).

setup_file() {
    vm_run "[ -f $PROBE ]"
    assert_success || fail_loud "probe not staged at $PROBE (fresh-vm-bootstrap.sh probes step)"
    vm_run "command -v mkfs.btrfs >/dev/null"
    assert_success || fail_loud "mkfs.btrfs not available in the VM"
    vm_run "bash $PROBE setup"
    assert_success
    assert_output_contains "PASS: setup"
}

teardown_file() {
    vm_run "bash $PROBE teardown" || true
}

@test "state-snapshot: a btrfs state_path is created as a subvolume (mechanism=subvolume)" {
    vm_run "bash $PROBE mechanism"
    assert_success
    assert_output_contains "PASS: mechanism"
}

@test "state-snapshot: the pre-activation snapshot is a read-only btrfs subvolume" {
    vm_run "bash $PROBE pre-activation-snapshot"
    assert_success
    assert_output_contains "PASS: pre-activation-snapshot"
}

@test "state-snapshot: rollback restores via RENAME_EXCHANGE, the displaced state is kept aside" {
    vm_run "bash $PROBE exchange-roundtrip"
    assert_success
    assert_output_contains "PASS: exchange-roundtrip"
}

@test "state-snapshot: a crash mid-swap recovers on btrfs, state_path is never missing" {
    vm_run "bash $PROBE crash-recovery"
    assert_success
    assert_output_contains "PASS: crash-recovery"
}
