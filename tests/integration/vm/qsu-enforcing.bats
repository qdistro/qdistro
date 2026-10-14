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
    reap_vm_drivers
}

@test "phase7-qsu-enforcing: qsu once + forever_argv under Enforcing — root, cached, zero qdistro-domain AVCs" {
    stage_vm_driver "s54-qsu-enforcing.sh"
    # stderr is kept: a failing run's diagnostics (AVC lines, audit2allow,
    # ausearch errors) belong in the bats log.
    vm_run "curl -fsS -o /tmp/s54.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/s54-qsu-enforcing.sh && chmod +x /tmp/s54.sh && bash /tmp/s54.sh 2>&1"
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "qsu enforcing driver skipped on an enforcing VM: $output"
    fi
    assert_success
    assert_output_contains "PASS: SELinux mode now Enforcing"
    assert_output_contains "PASS: once: qsu request found by admin and approved by root over D-Bus under Enforcing"
    assert_output_contains "PASS: once: qsu /usr/bin/id ran under Enforcing (rc=0, uid=0(root))"
    assert_output_contains "PASS: forever_argv: qsu request found by admin and approved by root over D-Bus under Enforcing"
    assert_output_contains "PASS: forever_argv: qsu /usr/bin/id ran under Enforcing (rc=0, uid=0(root))"
    assert_output_contains "PASS: forever_argv approval row written"
    assert_output_contains "PASS: 0 new denials — qsu policy covers the enforcing qsu flow"
    assert_output_contains "PASS: SELinux mode restored to"
    assert_output_contains "PASS: added qsu approval rows removed"
}
