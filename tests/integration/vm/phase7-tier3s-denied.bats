#!/usr/bin/env bats
# §Phase-7 tier 3s (gVisor runsc), todo/paravirt Phase A milestone A-iii:
# refusals (driver s121-tier3s-denied.sh). DONE bar (todo/paravirt 06) items
# 5 (broker denial => no podman run, no activation record; with a positive
# control) and 6 (hardened profiles release/daily and a probe failure refuse;
# no fallback).
# Setup provisions the fresh worker (tier3s.bash).

load helpers
load tier3s

setup_file() {
    t3s_setup_file s121-tier3s-denied.sh
}

teardown_file() {
    t3s_teardown_file
}

@test "phase7-tier3s-denied: broker denial, non-dev profile and probe failure refuse; no fallback" {
    t3s_run_driver s121-tier3s-denied.sh
    t3s_log s121
    assert_success
    t3s_no_failures s121
    for t in no-rule/untemplated no-rule/templated deny/untemplated deny/templated; do
        assert_output_contains "PASS: $t: the spawn refused with the expected message"
        assert_output_contains "PASS: $t: no podman run"
        assert_output_contains "PASS: $t: no fallback"
    done
    assert_output_contains "PASS: no-rule/templated: no activation record for the templated silo"
    assert_output_contains "PASS: deny/templated: no activation record for the templated silo"
    assert_output_contains "PASS: control: activation marker committed"
    assert_output_contains "PASS: control: the same event oracle sees the container start"
    for prof in release daily; do
        assert_output_contains "PASS: $prof: CreateTier3sSilo refused with the message"
        assert_output_contains "PASS: $prof: StartSilo refused with the message"
        assert_output_contains "PASS: $prof/spawn (direct unit start): the spawn refused with the expected message"
        assert_output_contains "PASS: $prof/spawn (direct unit start): no podman run"
    done
    # astra A r2 #4: an admin process in the launch unit's cgroup cannot ack
    assert_output_contains "PASS: the installed launch unit takes notifications from its main PID only (main)"
    assert_output_contains "PASS: forged READY/main: launch unit state after the admin process's READY=1 (activating)"
    assert_output_contains "PASS: forged READY/control (NotifyAccess=all): launch unit state after the admin process's READY=1 (active)"
    assert_output_contains "PASS: probe-failure: the spawn refused with the expected message"
    assert_output_contains "PASS: probe-failure: no fallback"
}
