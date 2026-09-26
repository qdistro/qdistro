#!/usr/bin/env bats
# Live guest regression for the root GUI driver's cgroup claim. A /proc tree
# scan cannot prove fork/reparent teardown safety; the kernel cgroup can.
load helpers

@test "GUI driver cgroup drains foreground and orphaned work before retry" {
    local root lib_b64 test_b64
    root=$(git -C "$BATS_TEST_DIRNAME" rev-parse --show-toplevel)
    lib_b64=$(base64 -w0 "$root/ci/lib/guest/gui-waiters.sh")
    vm_run "echo $lib_b64 | base64 -d > /tmp/qci-gui-waiters-cgroup.sh"
    assert_success

    test_b64=$(base64 -w0 "$BATS_TEST_DIRNAME/s115-gui-driver-cgroup.sh")
    vm_run "echo $test_b64 | base64 -d | bash"
    assert_success
    assert_output_contains "PASS direct owner SIGKILL drains foreground child"
    assert_output_contains "PASS forked orphans cannot act after contender"
    assert_output_contains "PASS intentional completion preserves detached app without holding lock"
    assert_output_contains "PASS registered worker holds claim after owner exit"
}
