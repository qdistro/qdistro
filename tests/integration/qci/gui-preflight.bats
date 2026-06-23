#!/usr/bin/env bats
#
# Host-only tests for the GUI gate's preflight capability summary
# (ci/lib/gates/gui.sh::gui_preflight_capabilities). Pure (reads only its args),
# so the lane-down observations are host-testable without the GUI VM stack. The
# function is REPORTING ONLY — it must never invent an observation for a fully
# capable profile, and never change a dispatch decision.
#
# Signature:
#   gui_preflight_capabilities skip_qdwin qdshell_active nested_kvm legacy_ctrl \
#       vm_ssh_port tier5_base tier4_base tier5_optin tier4_optin

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/gates/gui.sh"
}

@test "preflight: fully capable profile yields NO observations" {
    # skip_qdwin=0 qdshell=1 kvm=1 legacy=1 ssh=set bases=1 optins=0
    run gui_preflight_capabilities 0 1 1 1 2222 1 1 0 0
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "preflight: absent qdshell session is reported once as a profile gap" {
    run gui_preflight_capabilities 0 0 1 1 2222 1 1 0 0
    [ "$status" -eq 0 ]
    [[ "$output" == *"qdshell session ABSENT"* ]]
    [[ "$output" == *"profile gap"* ]]
}

@test "preflight: qdwin lane disabled is reported" {
    run gui_preflight_capabilities 1 1 1 1 2222 1 1 0 0
    [[ "$output" == *"qdwin lane DISABLED"* ]]
}

@test "preflight: skip_qdwin suppresses the per-lane qdshell/kvm notes" {
    # With the whole qdwin lane disabled, do not ALSO emit the qdshell/kvm lines.
    run gui_preflight_capabilities 1 0 0 1 2222 1 1 0 0
    [[ "$output" == *"qdwin lane DISABLED"* ]]
    ! [[ "$output" == *"qdshell session ABSENT"* ]]
    ! [[ "$output" == *"nested KVM"* ]]
}

@test "preflight: absent nested KVM is reported" {
    run gui_preflight_capabilities 0 1 0 1 2222 1 1 0 0
    [[ "$output" == *"nested KVM"* ]]
    [[ "$output" == *"tier-4/5"* ]]
}

@test "preflight: unset VM_SSH_PORT flags the SELinux scenario" {
    run gui_preflight_capabilities 0 1 1 1 "" 1 1 0 0
    [[ "$output" == *"VM_SSH_PORT unset"* ]]
    [[ "$output" == *"55"* ]]
}

@test "preflight: requested-but-absent tier5 bake is a run-and-ERROR note, not a skip" {
    # tier5_optin=1 tier5_base=0 -> requested but missing => runs and ERRORs.
    run gui_preflight_capabilities 0 1 1 1 2222 0 1 1 0
    [[ "$output" == *"tier-5 base image REQUESTED"* ]]
    [[ "$output" == *"ERROR on the broken bake"* ]]
}

@test "preflight: absent tier5 bake WITHOUT opt-in is NOT reported (clean skip lane)" {
    # tier5_optin=0 tier5_base=0 -> opt-in lane, absent is a clean skip, no note.
    run gui_preflight_capabilities 0 1 1 1 2222 0 1 0 0
    ! [[ "$output" == *"tier-5 base image REQUESTED"* ]]
}

@test "preflight: absent legacy ctrl-socket is an expected informational note" {
    run gui_preflight_capabilities 0 1 1 0 2222 1 1 0 0
    [[ "$output" == *"legacy qdshell ctrl-socket absent"* ]]
    [[ "$output" == *"expected"* ]]
}
