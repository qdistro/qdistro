#!/usr/bin/env bats
# §Phase-7 tier 3s (gVisor runsc), todo/paravirt Phase C: network=none is
# enforced, not just requested (driver s131-tier3s-netnone.sh; README O3).
# Proven inside the sandbox: lo only, no non-loopback route, no outbound
# connect — plus host-side spec/runsc corroboration. Headless staging only.

load helpers
load tier3s

setup_file() {
    t3s_setup_file s131-tier3s-netnone.sh
}

teardown_file() {
    t3s_teardown_file
}

@test "phase7-tier3s-netnone: lo only, no routes, no outbound connect" {
    t3s_run_driver s131-tier3s-netnone.sh
    t3s_log s131
    assert_success
    t3s_no_failures s131
    assert_output_contains "PASS: netnone: the only link is lo"
    assert_output_contains "PASS: netnone: no default route"
    assert_output_contains "PASS: netnone: every route is on lo"
    assert_output_contains "PASS: netnone: route to the non-loopback test address is unreachable"
    assert_output_contains "PASS: netnone: the connect probe ran and failed"
    assert_output_contains "PASS: netnone: the failure is ENETUNREACH"
    assert_output_contains "PASS: netnone: the failure is fast (not a filtered-route timeout)"
    assert_output_contains "PASS: netnone: podman NetworkMode"
    assert_output_contains "PASS: netnone: sentry runs with --network=none"
}
