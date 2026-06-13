#!/usr/bin/env bats
# Verify-only restore REHEARSAL — the REAL-btrfs half (qdistro_backup_service.py
# `rehearse`, 06-backup-dr §3.3). The host lane
# tests/integration/backup-rehearse-e2e.bats proves the always-on core (sig +
# chain + full-chain blob verification + freshness + read-only) with tar-stubbed
# btrfs; this suite swaps in real `btrfs subvolume snapshot -r`, real
# `btrfs send`/`send -p` (to build a real incremental chain) and the OPTIONAL
# real `btrfs receive` of --rehearse-receive into a throwaway scratch subvol that
# must be destroyed afterwards.
#
# Heavy lifting: tests/integration/vm/probes/backup-rehearse-probe.sh (staged to
# /root by fresh-vm-bootstrap.sh). Order is load-bearing: setup_file builds the
# loopback + 2 backup runs (a real incremental chain); later tests assume it.

load helpers

PROBE="/root/backup-rehearse-probe.sh"

setup_file() {
    vm_run "[ -f $PROBE ]"
    assert_success || fail_loud "probe not staged at $PROBE (fresh-vm-bootstrap.sh probes step)"
    vm_run "command -v mkfs.btrfs >/dev/null"
    assert_success || fail_loud "mkfs.btrfs not available in the VM"
    vm_run "command -v rsync >/dev/null"
    assert_success || fail_loud "rsync not installed in the VM — the backup collector needs it"
    vm_run "command -v rage >/dev/null || command -v age >/dev/null"
    assert_success || fail_loud "neither rage nor age installed in the VM — backups cannot encrypt"
    vm_run "bash $PROBE setup"
    assert_success
    assert_output_contains "PASS: setup"
}

teardown_file() {
    vm_run "bash $PROBE teardown" || true
}

@test "rehearse: verify-only over a REAL 2-manifest chain passes and never advances seq" {
    vm_run "bash $PROBE verify-only"
    assert_success
    assert_output_contains "PASS: verify-only"
}

@test "rehearse: --rehearse-receive does a REAL btrfs receive into a throwaway scratch subvol, then destroys it" {
    vm_run "bash $PROBE receive"
    assert_success
    assert_output_contains "PASS: receive"
}

@test "rehearse: a corrupt ANCESTOR blob still FAILS (full-chain coverage, not just newest)" {
    vm_run "bash $PROBE corrupt-ancestor"
    assert_success
    assert_output_contains "PASS: corrupt-ancestor"
}
