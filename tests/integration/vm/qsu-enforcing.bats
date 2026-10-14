#!/usr/bin/env bats
# qci:enforcing — runs on a clone of the per-run golden booted
# SELinux=enforcing, reached over SSH (ci/lib/gates/bats.sh).
#
# qsu end to end under Enforcing: the replacement for the retired GUI scenario
# permissions-gui/55 (which never needed a GUI) and for the former
# tiered-isolation.bats case phase7-qsu-enforcing, which SKIPped in every
# ordinary run (no SSH transport outside run-bats-enforcing.sh, and its driver
# skipped on a config-pinned permissive VM). Here the gate routes it to an
# enforcing VM automatically, and a SKIP is a failure, never a pass.
#
# Driver: s54-qsu-enforcing.sh (see its header for every assertion).

load helpers

teardown_file() {
    # The Permissive-start leg flips the VM; leave it Enforcing whatever happened.
    vm_run "/usr/sbin/setenforce 1" || true
    reap_vm_drivers
}

# run_s54 — stage and run the driver, keeping its stderr in the log.
run_s54() {
    stage_vm_driver "s54-qsu-enforcing.sh"
    # stderr is kept: a failing run's diagnostics (AVC lines, audit2allow,
    # ausearch errors) belong in the bats log.
    vm_run "curl -fsS -o /tmp/s54.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/s54-qsu-enforcing.sh && chmod +x /tmp/s54.sh && bash /tmp/s54.sh 2>&1"
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "qsu enforcing driver skipped on an enforcing VM: $output"
    fi
}

assert_s54_flow() {
    assert_success
    assert_output_contains "PASS: SELinux mode now Enforcing"
    assert_output_contains "PASS: once: qsu request found by admin and approved by root over D-Bus under Enforcing"
    assert_output_contains "PASS: once: qsu /usr/bin/id ran under Enforcing (rc=0, uid=0(root))"
    assert_output_contains "PASS: forever_argv: qsu request found by admin and approved by root over D-Bus under Enforcing"
    assert_output_contains "PASS: forever_argv: qsu /usr/bin/id ran under Enforcing (rc=0, uid=0(root))"
    assert_output_contains "PASS: forever_argv approval row written"
    assert_output_contains "PASS: 0 new denials — qsu policy covers the enforcing qsu flow"
    assert_output_contains "PASS: added qsu approval rows removed"
}

@test "phase7-qsu-enforcing: qsu once + forever_argv under Enforcing — root, cached, zero qdistro-domain AVCs" {
    vm_run "/usr/sbin/getenforce"
    assert_success
    [ "$(printf '%s' "$output" | tr -d '\r' | tail -n1)" = Enforcing ] \
        || fail_loud "VM is not enforcing before the run (getenforce: $output)"
    run_s54
    assert_s54_flow
    assert_output_contains "PASS: SELinux mode now Enforcing (was Enforcing)"
    assert_output_contains "PASS: SELinux mode restored to Enforcing"
}

@test "phase7-qsu-enforcing: from a Permissive start the driver flips to Enforcing and restores Permissive" {
    # The retired permissions-gui/55 asserted restoration to Permissive. The
    # enforcing VM boots Enforcing, so set it Permissive at runtime first
    # (the SSH transport may setenforce), run the same driver, and check the
    # mode it leaves behind from OUTSIDE the driver too.
    vm_run "/usr/sbin/setenforce 0 && /usr/sbin/getenforce"
    assert_success
    [ "$(printf '%s' "$output" | tr -d '\r' | tail -n1)" = Permissive ] \
        || fail_loud "could not set the VM Permissive for this leg: $output"
    run_s54
    local driver_out=$output driver_status=$status
    vm_run "/usr/sbin/getenforce"
    local after
    after=$(printf '%s' "$output" | tr -d '\r' | tail -n1)
    vm_run "/usr/sbin/setenforce 1 && /usr/sbin/getenforce"
    [ "$(printf '%s' "$output" | tr -d '\r' | tail -n1)" = Enforcing ] \
        || fail_loud "could not put the VM back to Enforcing: $output"
    output=$driver_out status=$driver_status
    assert_s54_flow
    assert_output_contains "PASS: SELinux mode now Enforcing (was Permissive)"
    assert_output_contains "PASS: SELinux mode restored to Permissive"
    [ "$after" = Permissive ] || fail_loud "driver left the VM $after, not Permissive"
}
