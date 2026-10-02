#!/usr/bin/env bats
# §Phase-7 tier 3s (gVisor runsc), todo/paravirt Phase A milestone A-iii:
# supervisor-side teardown (driver s122-tier3s-sigkill-cleanup.sh). DONE bar
# (todo/paravirt 06) item 2: launcher SIGKILL / service failure, session-
# manager restart reconciliation (state lost: unknown live unit, unrecorded
# launch, labelled container without a unit); owner O11: a session-manager
# STOP leaves no launch-owned process, scope, token dir or control dir.
# Setup provisions the fresh worker (tier3s.bash).

load helpers
load tier3s

setup_file() {
    t3s_setup_file s122-tier3s-sigkill-cleanup.sh
}

teardown_file() {
    t3s_teardown_file
}

@test "phase7-tier3s-sigkill-cleanup: SIGKILL, manager stop (O11), crash, restart, reconciliation" {
    t3s_run_driver s122-tier3s-sigkill-cleanup.sh
    t3s_log s122
    assert_success
    t3s_no_failures s122
    for t in launcher-sigkill manager-stop/s122a manager-stop/s122b manager-crash manager-restart \
             reconcile/recorded reconcile/unrecorded; do
        assert_output_contains "PASS: $t: all "
        assert_output_contains "PASS: $t: scope "
        assert_output_contains "PASS: $t: per-launch dir "
        assert_output_contains "PASS: $t: control record "
    done
    assert_output_contains "PASS: manager-stop: qdistro-tier3s-silo@s122a.service stopped through the verified cleanup"
    assert_output_contains "PASS: reconcile/recorded: the restarted manager stopped the unknown launch unit"
    assert_output_contains "PASS: ghost: reconciliation reaped the labelled container"
}
