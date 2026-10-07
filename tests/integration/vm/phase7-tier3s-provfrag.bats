#!/usr/bin/env bats
# §Phase-7 tier 3s (gVisor runsc), todo/paravirt follow-up: a launch unit
# killed mid-useradd (startup reconcile / stop during activation) leaves a
# bare qt3s-<silo> account that used to wedge the silo forever. The spawn now
# repairs exactly that fragment signature; partial state and live uids stay
# refused (driver s132-tier3s-provfrag.sh). Headless staging only.

load helpers
load tier3s

setup_file() {
    t3s_setup_file s132-tier3s-provfrag.sh
}

teardown_file() {
    t3s_teardown_file
}

@test "phase7-tier3s-provfrag: mid-useradd fragment repaired; partial/live states refused" {
    t3s_run_driver s132-tier3s-provfrag.sh
    t3s_log s132
    assert_success
    t3s_no_failures s132
    assert_output_contains "PASS: fragment forged for s132frag (no home, no subid rows)"
    assert_output_contains "PASS: spawn repaired the fragment"
    assert_output_contains "PASS: spawn re-provisioned the account"
    assert_output_contains "PASS: launch reached a LATER refusal (the empty silo store), not the fragment"
    assert_output_contains "PASS: the old wedge refusal is gone"
    assert_output_contains "PASS: account is healthy after repair (home owned, subid rows)"
    assert_output_contains "PASS: repaired silo s132frag launched"
    assert_output_contains "PASS: s132part: refused on the missing subid rows"
    assert_output_contains "PASS: s132part: repair did NOT fire (home exists — not a fragment signature)"
    assert_output_contains "PASS: s132part: account and home are still there"
    assert_output_contains "PASS: s132live: refused on the live uid"
    assert_output_contains "PASS: s132live: account not deleted while its uid is live"
    assert_output_contains "PASS: s132live: once the process is gone the fragment is repaired"
    assert_output_contains "PASS: s132one: repair did NOT fire (a subid row exists)"
    assert_output_contains "PASS: s132one: account and its planted row are still there"
    assert_output_contains "PASS: s132sym: repair did NOT fire (the path is not absent)"
    assert_output_contains "PASS: s132sym: account and planted symlink are still there"
    assert_output_contains "PASS: s132err: refusal cites the unreadable subid db"
    assert_output_contains "PASS: s132err: repair did NOT fire on a lookup error"
}
