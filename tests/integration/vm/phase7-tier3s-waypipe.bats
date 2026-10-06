#!/usr/bin/env bats
# §Phase-7 tier 3s (gVisor runsc), todo/paravirt Phase B milestone B-iii:
# the waypipe bridge launch path end to end (driver s123-tier3s-waypipe.sh).
# DONE bar (Phase B): the secctx-wrapped bridge client starts under the
# root/runuser topology; the toplevel metadata is engine=qdistro.tier3s,
# app_id=qdistro.tier3s.<silo>, instance=<launch token>; qdshell logs
# "[tier3s] toplevel observed"; RegisterLaunch succeeded and audited; the
# launch record is consumed; teardown reaps the bridge pair.
# Setup provisions the fresh worker AND stages the weston-terminal GUI
# image + admin session (tier3s.bash). Evidence: guest transcripts land in
# qci's per-file log (fd 3) and in the scratch dir.

load helpers
load tier3s

setup_file() {
    t3s_setup_file s123-tier3s-waypipe.sh weston-terminal
}

teardown_file() {
    t3s_teardown_file
}

@test "phase7-tier3s-waypipe: bridge topology, secctx identity, RegisterLaunch, teardown" {
    t3s_run_driver s123-tier3s-waypipe.sh
    t3s_log s123
    assert_success
    t3s_no_failures s123
    assert_output_contains "PASS: launch up (token "
    assert_output_contains "PASS: record: bridge client+wrapper pid/starttime recorded"
    assert_output_contains "PASS: bridge: bridge client pid "
    assert_output_contains "PASS: bridge: bridge channel live"
    assert_output_contains "PASS: bridge client connects through secctx listener wayland-secctx-"
    assert_output_contains "PASS: bridge wrapper is root and the waypipe client's ancestor"
    assert_output_contains "PASS: broker wrote qdistro.lineage.register:s123a"
    assert_output_contains "PASS: launch record consumed after RegisterLaunch"
    assert_output_contains "PASS: qdshell: [tier3s] toplevel observed"
    assert_output_contains "PASS: compositor: toplevel_security_context carries the launch token as instance"
    assert_output_contains "PASS: compositor: peer identity names the live bridge client"
    assert_output_contains "PASS: bridge mount is READ-ONLY in the sandbox view"
    assert_output_contains "PASS: sandbox write to the bridge dir is refused"
    # model A: the bridge socket is chowned silo:silo 0600 after the admin
    # client's bind (the sandbox connects as the silo uid); the launch dir
    # is admin-owned 0711 (traversable, not listable)
    assert_output_contains "PASS: host link.sock is silo-owned 0600"
    assert_output_contains "PASS: launch dir is admin-owned 0711"
    assert_output_contains "PASS: sandbox identity is the silo uid (keep-id, model A)"
    assert_output_contains "PASS: container NetworkMode is none"
    assert_output_contains "PASS: link.sock channel is an ESTABLISHED host unix socket through the gofer netns"
    assert_output_contains "PASS: launch dir empty once the bridge attached"
    assert_output_contains "PASS: single-attach: the consumed link.sock listener fd still listens"
    assert_output_contains "PASS: create argv pins --runtime-flag=host-uds=open"
    assert_output_contains "PASS: create argv pins --network=none"
    assert_output_contains "PASS: no host bind outside the launch dir and podman-internal userdata"
    assert_output_contains "PASS: teardown: bridge client+wrapper gone (pid+starttime)"
    assert_output_contains "PASS: teardown: secctx listener "
    assert_output_contains "PASS: teardown: launch record "
}
