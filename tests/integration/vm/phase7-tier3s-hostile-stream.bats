#!/usr/bin/env bats
# §Phase-7 tier 3s (gVisor runsc), todo/paravirt Phase B milestone B-iii:
# hostile waypipe stream (driver s129-tier3s-hostile-stream.sh). DONE bar
# item: "hostile bridge input kills only the bridge connection" — garbage,
# truncated frames and a connect flood on the launch's link.sock AND its
# secctx listener never take down the compositor, qdshell, or the sibling
# launch; whichever way the attacked bridge ends up (dropped connection or
# dead client) the blast radius is that one connection.

load helpers
load tier3s

setup_file() {
    t3s_setup_file s129-tier3s-hostile-stream.sh weston-terminal
}

teardown_file() {
    t3s_teardown_file
}

@test "phase7-tier3s-hostile-stream: garbage on the bridge kills only that connection" {
    t3s_run_driver s129-tier3s-hostile-stream.sh
    t3s_log s129
    assert_success
    t3s_no_failures s129
    assert_output_contains "PASS: both GUI launches up"
    assert_output_contains "PASS: listener attacks DELIVERED while A's listener was live"
    assert_output_contains "PASS: waypipe frames written onto the SANDBOX end of the link"
    assert_output_contains "PASS: hose wrote onto bridge sockets (attributed ends only)"
    assert_output_contains "PASS: compositor still the same pid, unit active"
    assert_output_contains "PASS: qdshell still the SAME pid (no restart) and active"
    assert_output_contains "PASS: no compositor/qdshell crash in the journal since the attack"
    assert_output_contains "PASS: B's record still running"
    assert_output_contains "PASS: B's bridge client still live (starttime verified)"
    assert_output_contains "PASS: B's toplevel still in the qdshell model"
    assert_output_contains "PASS: B's container still running"
    # whichever in-contract outcome A took, one of these PASSes prints
    assert_output_contains "PASS: A's bridge "
}
