#!/usr/bin/env bats
# §Phase-7 tier 3s (gVisor runsc), todo/paravirt Phase B milestone B-iii:
# weston-terminal AND foot render and accept input through the waypipe
# bridge (driver s124-tier3s-app.sh). DONE bar items: "[apps] render and
# accept input" — focus injection lands on the tagged toplevel, the
# compositor logs a committed frame (mapped handle=N), and ydotool-typed
# input runs a command whose effect is observed INSIDE the sandbox
# (podman exec sees the marker in the container's /tmp).
# Stages both GUI image archives; fails loudly when either is missing.

load helpers
load tier3s

setup_file() {
    t3s_setup_file s124-tier3s-app.sh weston-terminal,foot
}

teardown_file() {
    t3s_teardown_file
}

@test "phase7-tier3s-app: weston-terminal and foot render + accept input through the bridge" {
    t3s_run_driver s124-tier3s-app.sh
    t3s_log s124
    assert_success
    t3s_no_failures s124
    assert_output_contains "PASS: s124w launch up (token "
    assert_output_contains "PASS: s124f launch up (token "
    assert_output_contains "PASS: weston toplevel carries the [3s:s124w] title prefix"
    assert_output_contains "PASS: foot toplevel carries the [3s:s124f] title prefix"
    for t in weston foot; do
        assert_output_contains "PASS: $t: findSiloHandle resolves the tier3s toplevel"
        assert_output_contains "PASS: $t: injectFocus accepted for the tier3s handle"
        assert_output_contains "PASS: $t: compositor reports focus on handle "
        assert_output_contains "PASS: $t: compositor mapped a committed frame"
        assert_output_contains "PASS: $t: typed command created /tmp/s124-s124"
        assert_output_contains "PASS: teardown/s124"
    done
    assert_output_contains "PASS: s124w: spec carries the weston-terminal.json profile"
    assert_output_contains "PASS: s124f: spec carries the foot.json profile"
    for s in s124w s124f; do
        assert_output_contains "PASS: $s: fchmodat ALLOW effective (plain chmod)"
        assert_output_contains "PASS: $s: fchmodat2 path (chmod -h) EPERM, mode unchanged"
        assert_output_contains "PASS: $s: link/linkat DENY effective"
        assert_output_contains "PASS: $s: llistxattr ALLOW effective (ls -l clean)"
        assert_output_contains "PASS: $s: NoNewPrivs + seccomp filter mode inside"
    done
}
