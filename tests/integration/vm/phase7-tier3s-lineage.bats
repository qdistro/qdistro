#!/usr/bin/env bats
# §Phase-7 tier 3s (gVisor runsc), todo/paravirt Phase B milestone B-iii:
# launch-record lineage for the GUI bridge (driver s128-tier3s-lineage.sh).
# DONE bar items: "lineage enforcement enabled" — spawn-tier3s.sh
# registers the REAL bridge-client (pid,starttime) before podman run
# (qdistro.lineage.register:<silo> audit row); RegisterLaunch re-verifies
# the live process (dead pid / wrong starttime refused); under
# lineage_enforce=true the broker's cross-silo clipboard gate hard-denies
# a missing, unrecorded, stale-starttime or forged-claim source, and an
# attested bridge-client source satisfies an explicit allow rule while the
# same attested source in a direction with no rule still denies.

load helpers
load tier3s

setup_file() {
    t3s_setup_file s128-tier3s-lineage.sh weston-terminal
}

teardown_file() {
    t3s_teardown_file
}

@test "phase7-tier3s-lineage: RegisterLaunch binding + enforce-mode source attestation" {
    t3s_run_driver s128-tier3s-lineage.sh
    t3s_log s128
    assert_success
    t3s_no_failures s128
    assert_output_contains "PASS: launch up ("
    assert_output_contains "PASS: launch reached running (RegisterLaunch succeeded before podman run)"
    assert_output_contains "PASS: audit: register row names the bridge pid+starttime"
    assert_output_contains "PASS: RegisterLaunch of a dead pid is refused"
    assert_output_contains "PASS: RegisterLaunch with a wrong starttime is refused"
    assert_output_contains "PASS: broker restarted under lineage_enforce"
    assert_output_contains "PASS: enforce: no relayed source pid -> deny"
    assert_output_contains "PASS: enforce: unrecorded live pid (1) -> deny"
    assert_output_contains "PASS: enforce: real pid + drifted starttime -> deny"
    assert_output_contains "PASS: enforce: real pid + forged claim of another silo -> the attested silo wins"
    assert_output_contains "PASS: enforce: attested source, no rule -> deny (enforce never bypasses rules)"
    assert_output_contains "PASS: attested source + allow rule -> allow"
    assert_output_contains "PASS: attested source, no rule for s128b->s128a -> deny"
    assert_output_contains "PASS: lineage_enforce restored"
}
