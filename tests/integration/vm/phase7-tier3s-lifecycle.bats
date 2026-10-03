#!/usr/bin/env bats
# §Phase-7 tier 3s (gVisor runsc), todo/paravirt Phase B milestone B-iii:
# GUI launch lifecycle and the pre-podman refusals (driver
# s125-tier3s-lifecycle.sh). DONE bar items: "missing launch identity or a
# failed RegisterLaunch prevents the launch before podman run" and
# "GUI lifecycle and concurrent-launch teardown". Covers: no compositor
# socket, a bridge client that never publishes the launch record, a denied
# RegisterLaunch, a normal start/stop, a SIGKILL mid-run, and two
# concurrent launches coming down independently.

load helpers
load tier3s

setup_file() {
    t3s_setup_file s125-tier3s-lifecycle.sh weston-terminal
}

teardown_file() {
    t3s_teardown_file
}

@test "phase7-tier3s-lifecycle: refusals before podman run, start/stop, SIGKILL, concurrency" {
    t3s_run_driver s125-tier3s-lifecycle.sh
    t3s_log s125
    assert_success
    t3s_no_failures s125
    # the three refused launches: refused message + nothing ran
    for tag in no-compositor no-launch-record registerlaunch-denied; do
        assert_output_contains "PASS: $tag: StartSilo fails for the refused launch"
        assert_output_contains "PASS: $tag: the spawn refused with the expected message"
        assert_output_contains "PASS: $tag: no podman run (no container event but the probe's scratch create/remove)"
        assert_output_contains "PASS: $tag: no control record, no per-launch dir"
    done
    assert_output_contains "PASS: lifecycle: qdshell observed the toplevel"
    assert_output_contains "PASS: lifecycle: StopSilo"
    assert_output_contains "PASS: concurrent: two launches up with distinct tokens"
    assert_output_contains "PASS: concurrent: A's launch unit failed visibly (signal)"
    assert_output_contains "PASS: concurrent: B's launch still runs"
    assert_output_contains "PASS: concurrent: B's bridge client still live"
}
