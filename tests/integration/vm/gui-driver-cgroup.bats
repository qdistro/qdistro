#!/usr/bin/env bats
# Live guest regression for the root GUI driver's cgroup claim. A /proc tree
# scan cannot prove fork/reparent teardown safety; the kernel cgroup can.
load helpers

@test "GUI driver cgroup drains foreground and orphaned work before retry" {
    local root test_b64
    root=$(git -C "$BATS_TEST_DIRNAME" rev-parse --show-toplevel)
    # In chunks: the library is past the one-argument limit of vm-exec's
    # command (an inline copy failed with E2BIG).
    # shellcheck source=../../../scripts/vm/lib/vm-push-file.sh
    . "$root/scripts/vm/lib/vm-push-file.sh"
    run qdistro_vm_push_file "$VM_EXEC" "$VM_NAME" \
        "$root/ci/lib/guest/gui-waiters.sh" /tmp/qci-gui-waiters-cgroup.sh "bash -n"
    assert_success

    test_b64=$(base64 -w0 "$BATS_TEST_DIRNAME/s115-gui-driver-cgroup.sh")
    vm_run "echo $test_b64 | base64 -d | bash"
    assert_success
    assert_output_contains "PASS direct owner SIGKILL drains foreground child"
    assert_output_contains "PASS forked orphans cannot act after contender"
    assert_output_contains "PASS intentional completion preserves detached app without holding lock"
    assert_output_contains "PASS registered worker holds claim after owner exit"
    assert_output_contains "PASS owner death during job handoff kills stopped worker"
    assert_output_contains "PASS ordinary completion removes empty scope and marker"
    assert_output_contains "PASS refused claim cleans its scope and marker"
    assert_output_contains "PASS failed setup removes empty scope without following marker"
}
