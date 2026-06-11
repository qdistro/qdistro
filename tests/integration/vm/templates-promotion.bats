#!/usr/bin/env bats
# Template/promotion invariants (todo/fableplan task 09). These tests ARE
# the promise from doc/templates.md — each asserts a load-bearing claim
# against real rootless podman inside the VM, with no DB mocking.
#
# The heavy lifting lives in tests/integration/vm/probes/
# templates-promotion-probe.sh (staged to /root by fresh-vm-bootstrap.sh
# and runnable on the host too), so vm-exec's qga JSON quoting never has to
# carry the build/promote sequences. Each @test runs one scenario as admin
# (uid 1000, rootless podman). State is built once in setup_file and shared
# via a private QDISTRO_TEST_ROOT so the real /var/lib/qdistro is untouched.
#
# Order is load-bearing: setup builds generation A; flip-at-restart builds
# generation B; rollback and GC depend on both.

load helpers

# NB: vm_run()/vm_run_admin() call bats `run` internally (they set $status and
# $output themselves), so they must be invoked BARE — wrapping them in another
# `run` captures nothing and the assertions become vacuous. This matches the
# working suites (pwd-print-recall.bats, templates-state-snapshot.bats).
PROBE="/root/templates-promotion-probe.sh"
TROOT="/tmp/fp09-promotion"

setup_file() {
    # vm_run/vm_run_admin call bats `run` internally and always return 0, so the
    # precondition must `assert_success` (a `|| fail_loud` after them is dead —
    # it never fires). Matches the templates-browser suite.
    vm_run "[ -f $PROBE ]"
    assert_success || fail_loud "probe not staged at $PROBE (fresh-vm-bootstrap.sh probes step)"
    vm_run_admin "command -v podman >/dev/null"
    assert_success || fail_loud "podman not available for admin in the VM"
    # Build + validate + promote the baseline generation A (slow, once).
    vm_run_admin "QDISTRO_TEST_ROOT=$TROOT bash $PROBE setup"
    assert_success
    assert_output_contains "PASS: setup"
}

@test "templates: digest pinning — binding pins a digest, a tag ref refuses launch" {
    vm_run_admin "QDISTRO_TEST_ROOT=$TROOT bash $PROBE digest-pinning"
    assert_success
    assert_output_contains "PASS: digest-pinning"
}

@test "templates: failed validation never flips the binding" {
    vm_run_admin "QDISTRO_TEST_ROOT=$TROOT bash $PROBE failed-validation"
    assert_success
    assert_output_contains "PASS: failed-validation"
}

@test "templates: promotion flips only at restart" {
    vm_run_admin "QDISTRO_TEST_ROOT=$TROOT bash $PROBE flip-at-restart"
    assert_success
    assert_output_contains "PASS: flip-at-restart"
}

@test "templates: rollback flips back, both generations pinned" {
    vm_run_admin "QDISTRO_TEST_ROOT=$TROOT bash $PROBE rollback"
    assert_success
    assert_output_contains "PASS: rollback"
}

@test "templates: GC pin safety — pinned survive, corrupt pin aborts" {
    vm_run_admin "QDISTRO_TEST_ROOT=$TROOT bash $PROBE gc-pin-safety"
    assert_success
    assert_output_contains "PASS: gc-pin-safety"
}

@test "templates: crash consistency — a killed promote never leaves a partial binding" {
    vm_run_admin "QDISTRO_TEST_ROOT=$TROOT bash $PROBE crash-consistency"
    assert_success
    assert_output_contains "PASS: crash-consistency"
}

@test "templates: candidate isolation — no silo state reaches a candidate runtime" {
    vm_run_admin "QDISTRO_TEST_ROOT=$TROOT bash $PROBE candidate-isolation"
    assert_success
    assert_output_contains "PASS: candidate-isolation"
}
