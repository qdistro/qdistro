#!/usr/bin/env bats
# §Phase-7 tier 3s (gVisor runsc), todo/paravirt Phase A milestone A-iii:
# the headless launch path end to end (driver s120-tier3s-headless.sh).
# DONE bar (todo/paravirt 06 "Δ DONE bar") items 1, 2 (normal exit, plain
# podman stop / rm, session-manager stop, forced runtime failure + recovery),
# 3, 4 and 8. Setup provisions the fresh worker (tier3s.bash). Evidence: the
# guest transcripts land in qci's per-file log (fd 3) and in the scratch dir.

load helpers
load tier3s

setup_file() {
    t3s_setup_file s120-tier3s-headless.sh
}

teardown_file() {
    t3s_teardown_file
}

@test "phase7-tier3s-headless: launch path, placement, identity, state root, two launches, posture" {
    t3s_run_driver s120-tier3s-headless.sh
    t3s_log s120
    assert_success
    t3s_no_failures s120
    # DONE 1: placement (every class)
    for c in runuser podman-cli conmon runsc-gofer runsc-sandbox runsc-fd-parking systrap-stub; do
        assert_output_contains "PASS: placement[A]: $c x"
    done
    assert_output_contains "PASS: placement[A]: runsc-bundle processes outside the owning scope"
    # DONE 2: teardown paths
    for t in normal-exit session-manager-stop podman-stop podman-rm recovery; do
        assert_output_contains "PASS: $t: all "
        assert_output_contains "PASS: $t: control record /run/qdistro-tier3s-ctl/"
    done
    assert_output_contains "PASS: missing root: cleanup refuses to query or stop"
    assert_output_contains "PASS: replaced root (cleanup): control record preserved"
    assert_output_contains "PASS: missing root (cleanup): owning scope preserved"
    # DONE 3
    assert_output_contains "PASS: two launches: every process of B survived A's teardown"
    # DONE 4
    assert_output_contains "PASS: identity: State.Pid exe sha512 = pin sidecar_gvisor_sentry_sha512"
    assert_output_contains "PASS: identity: gofer exe sha512 = pin runsc_sha512"
    assert_output_contains "PASS: state root: plain podman ps --sync (runtime state query) rc"
    assert_output_contains "PASS: missing root: plain podman stop fails visibly"
    assert_output_contains "PASS: replaced root: plain podman stop fails visibly"
    # DONE 8
    assert_output_contains "PASS: posture/spec: seccomp allow set = the file's allow set"
    assert_output_contains "PASS: ΔA4 fchmodat2 path exercised: chmod -h (fchmodat2) is denied, mode unchanged"
    assert_output_contains "PASS: ΔA5: image snapshot label = snapshot.conf pin"
}
