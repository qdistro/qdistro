#!/usr/bin/env bats
# Disposable tier-2 silo e2e — the REAL podman + live-compositor half of the
# disposables lifecycle (07-disposables-plan P1, M3 VM residual). The host lane
# tests/unit/test_disposables.py proves the pure helpers + reaper logic against
# a FAKE ops; this suite swaps in real podman on a running qdwin session — the
# half the headless dev host could not run (rootless podman + CAP_SYS_ADMIN + a
# live compositor).
#
# The heavy lifting lives in tests/integration/vm/probes/disp-probe.sh (staged
# to /root by fresh-vm-bootstrap.sh, host-runnable too where root + podman + a
# qdwin session exist). It drives the SHIPPED /usr/bin/qdistro-tier2-spawn
# --disposable (not a source copy) so a packaging gap surfaces, and the reaper
# half imports the INSTALLED daemon module to exercise the real podman sweep.
#
# Order is load-bearing: setup_file builds the tier-2 image + authors the broker
# allow rule + checks the compositor is up; the per-test subcommands are each
# self-contained (spawn -> assert -> teardown) so a stranded container can never
# leak across tests; teardown_file removes the rule + fixtures.

load helpers

PROBE="/root/disp-probe.sh"

# NB: vm_run() calls bats `run` internally (sets $status/$output itself), so it
# must be invoked BARE — wrapping it in another `run` captures nothing. Matches
# backup-btrfs-e2e.bats / templates-state-snapshot.bats.

setup_file() {
    vm_run "[ -f $PROBE ]"
    assert_success || fail_loud "probe not staged at $PROBE (fresh-vm-bootstrap.sh probes step)"
    vm_run "command -v podman >/dev/null"
    assert_success || fail_loud "podman not available in the VM — tier-2 disposables cannot spawn"
    # The disposable spawn binary is a SHIPPED artifact (install-qdwin-session-
    # for-vm.sh). A missing binary is a packaging regression, not a skip —
    # fail loudly (helpers.bash policy: silent skips have masked missing-dep
    # regressions before).
    vm_run "[ -x /usr/bin/qdistro-tier2-spawn ]"
    assert_success || fail_loud "/usr/bin/qdistro-tier2-spawn not installed — disposable spawn path absent (PACKAGING GAP)"
    vm_run "bash $PROBE setup"
    assert_success
    assert_output_contains "PASS: setup"
}

teardown_file() {
    # Assert teardown succeeded so a failed cleanup — which would leave the
    # test-authored broker allow rule standing in the VM — is LOUD, not
    # swallowed (helpers.bash policy: never silently skip).
    vm_run "bash $PROBE teardown"
    assert_success || fail_loud "disp-probe teardown failed (test-authored broker rule may persist)"
    assert_output_contains "PASS: teardown"
}

@test "disposables: --disposable spawn -> disp-* name, qdistro.disp app_id, label+tmpfs+--rm, window, close-gone" {
    vm_run "bash $PROBE spawn-window-close"
    assert_success
    assert_output_contains "PASS: disp name shape"
    assert_output_contains "PASS: secctx app_id"
    assert_output_contains "PASS: qdistro_disposable=1 label present"
    assert_output_contains "PASS: podman --rm (AutoRemove) set"
    assert_output_contains "PASS: tmpfs /home/admin (no persistent state)"
    assert_output_contains "PASS: disposable window advertised to the outer compositor"
    assert_output_contains "PASS: inner GUI process (weston-terminal) alive in the disposable"
    assert_output_contains "PASS: close -> container gone (--rm discarded it)"
    assert_output_contains "PASS: spawn-window-close"
}

@test "disposables: reaper sweeps a stranded disp-* by label, never an ordinary/disp-named-unlabelled/forged-labelled container" {
    vm_run "bash $PROBE reaper-sweep"
    assert_success
    assert_output_contains "PASS: reaper lists by label + sweeps by name-shape; forged & misnamed containers refused"
    assert_output_contains "PASS: orphan reaped; ordinary, disp-named-unlabelled, and forged-labelled containers all survived"
    assert_output_contains "PASS: reaper-sweep"
}

@test "disposables: dispose.spawn with no allow rule fails closed at the broker (no container minted)" {
    vm_run "bash $PROBE deny-fail-closed"
    assert_success
    assert_output_contains "PASS: broker returns 'unknown' for the unruled disposable action"
    assert_output_contains "PASS: broker has no rule -> spawn refused at the gate (decision=unknown)"
    assert_output_contains "PASS: no container minted on the deny path (fail-closed)"
    assert_output_contains "PASS: deny-fail-closed"
}

@test "disposables: shipped spawn stamps qdistro_lease_ttl/created labels when QDISTRO_DISPOSABLE_TTL is opted in" {
    vm_run "bash $PROBE lease-spawn-labels"
    assert_success
    assert_output_contains "PASS: shipped spawn stamped qdistro_lease_ttl"
    assert_output_contains "PASS: shipped spawn stamped qdistro_lease_created"
    assert_output_contains "PASS: lease-spawn-labels"
}

@test "disposables: TTL-lease sweep reaps only the expired well-formed disposable (fresh/no-lease/no-token/forged-name survive)" {
    vm_run "bash $PROBE lease-sweep"
    assert_success
    assert_output_contains "PASS: lease sweep enumerated by label, reaped only the expired well-formed disposable"
    assert_output_contains "PASS: fresh / no-lease / no-token / forged-name fixtures all survived"
    assert_output_contains "PASS: lease-sweep"
}
