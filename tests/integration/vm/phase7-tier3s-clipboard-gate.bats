#!/usr/bin/env bats
# §Phase-7 tier 3s (gVisor runsc), todo/paravirt Phase B milestone B-iii:
# the cross-silo clipboard gate for tier3s (driver
# s127-tier3s-clipboard-gate.sh). DONE bar items: "clipboard is
# default-deny, then explicitly allowed via SaveRule, with lineage
# enforcement enabled" — a REAL secctx-tagged selection drives qdshell's
# CLIPBOARD_GATE (deny -> broker:deny), a png-only offer is stripped
# before the broker (tier3s-no-allowed-mimes), SaveRule flips the live
# verdict to allow, a focus crossing clears the selection
# (CLIPBOARD_FOCUS_GATE), and the receive gate is per-MIME (text/plain
# allowed, image/png denied) — every probe audited.

load helpers
load tier3s

setup_file() {
    t3s_setup_file s127-tier3s-clipboard-gate.sh weston-terminal
}

teardown_file() {
    t3s_teardown_file
}

@test "phase7-tier3s-clipboard-gate: default-deny, SaveRule allow, strict MIME, focus clear, receive gate" {
    t3s_run_driver s127-tier3s-clipboard-gate.sh
    t3s_log s127
    assert_success
    t3s_no_failures s127
    assert_output_contains "PASS: broker restarted under lineage_enforce"
    assert_output_contains "PASS: silo-security registry stays root-owned 0644"
    assert_output_contains "PASS: clip source A registered in the launch-record store"
    assert_output_contains "PASS: qdshell gate line names the real tagged source silo"
    assert_output_contains "PASS: default-deny verdict at set-time"
    assert_output_contains "PASS: enforce: an unrecorded pid can only deny"
    assert_output_contains "PASS: qdshell denied a png-only tier3s offer before the broker"
    assert_output_contains "PASS: qdshell logged the tier3s mime-strip"
    assert_output_contains "PASS: SaveRule wrote the file"
    assert_output_contains "PASS: broker probe allows transfer s127a->s127b under the rule"
    assert_output_contains "PASS: enforce: a forged source-silo claim cannot steer"
    assert_output_contains "PASS: broker journaled the claim-vs-attested override"
    assert_output_contains "PASS: enforce: a forged destination silo still denies"
    assert_output_contains "PASS: cross-silo clip source A registered in the launch-record store"
    assert_output_contains "PASS: compositor relayed the source's own pid"
    assert_output_contains "PASS: live rule-driven allow: cross-silo s127a -> s127b"
    assert_output_contains "PASS: bound clip source C registered in the launch-record store"
    assert_output_contains "PASS: compositor attested the bound source's tag on its toplevel"
    assert_output_contains "PASS: seat focus landed on the bound source"
    assert_output_contains "PASS: live attested allow: bound tagged source -> same-silo"
    assert_output_contains "PASS: same-silo allow is broker-verified"
    assert_output_contains "PASS: seat focus re-landed on s127b's toplevel"
    assert_output_contains "PASS: B's tagged offer recorded"
    assert_output_contains "PASS: focus crossing out of the source silo cleared the selection"
    assert_output_contains "PASS: receive probe defaults to deny"
    assert_output_contains "PASS: receive probe allows text/plain under the mime rule"
    assert_output_contains "PASS: receive probe still denies image/png (mime selector)"
    assert_output_contains "PASS: audit: denied transfer row(s) recorded"
    assert_output_contains "PASS: audit: allowed transfer row(s) recorded"
    assert_output_contains "PASS: lineage_enforce restored"
    assert_output_contains "PASS: silo-security registry restored byte-exact"
}
