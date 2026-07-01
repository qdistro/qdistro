#!/usr/bin/env bats
#
# Host-only test for blocked-on-infra cascade suppression in gate_full
# (ci/lib/dispatch.sh, H5). VM-dependent gates (vm-smoke, bats, gui) share one
# infra resource (libvirt provisioning + the per-run golden). When one fails with
# EXIT_VM_PROVISION, the remaining VM gates must be recorded as `blocked-on-infra`
# rows rather than run into the same wall — so a single infra root cause books ONE
# actionable failure, not N.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    TMP="$(mktemp -d)"
    EXIT_OK=0
    EXIT_VM_PROVISION=40
    CALLS="$TMP/calls.log"; BLOCKED="$TMP/blocked.log"
    : > "$CALLS"; : > "$BLOCKED"
    # Stub everything gate_full leans on. The gate stubs record their invocation
    # and return a per-test configurable rc; record_blocked logs its subject.
    qci_assert_run_dir() { return 0; }
    log() { :; }
    gate_preflight() { return 0; }
    gate_host() { return 0; }
    gate_release_manifest() { return 0; }
    gate_bootstrap_release_profile() { return 0; }
    record_blocked() { printf '%s\n' "$1" >> "$BLOCKED"; }
    gate_vm_smoke() { echo vm-smoke >> "$CALLS"; return "${VM_SMOKE_RC:-0}"; }
    gate_bats() { echo bats >> "$CALLS"; return "${BATS_RC:-0}"; }
    gate_gui() { echo gui >> "$CALLS"; return "${GUI_RC:-0}"; }
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/dispatch.sh"
}

teardown() { [ -n "${TMP:-}" ] && rm -rf "$TMP"; }

@test "all VM gates run when none fail infra; no blocked rows" {
    run gate_full
    [ "$status" -eq 0 ]
    [ "$(cat "$CALLS")" = "$(printf 'vm-smoke\nbats\ngui')" ]
    [ ! -s "$BLOCKED" ]
}

@test "vm-smoke vm-provision failure blocks bats + gui (not run)" {
    VM_SMOKE_RC=40 run gate_full
    [ "$status" -eq 40 ]
    # Only vm-smoke ran; bats + gui were NOT invoked.
    [ "$(cat "$CALLS")" = "vm-smoke" ]
    # Both downstream VM gates were recorded blocked-on-infra.
    [ "$(cat "$BLOCKED")" = "$(printf 'bats\ngui')" ]
}

@test "bats vm-provision failure blocks only gui" {
    BATS_RC=40 run gate_full
    [ "$status" -eq 40 ]
    # vm-smoke + bats ran; gui did not.
    [ "$(cat "$CALLS")" = "$(printf 'vm-smoke\nbats')" ]
    [ "$(cat "$BLOCKED")" = "gui" ]
}

@test "a non-infra gate failure does NOT trigger the cascade" {
    # A plain bats failure (EXIT_BATS=35, not vm-provision) still runs gui.
    BATS_RC=35 run gate_full
    [ "$status" -eq 35 ]
    [ "$(cat "$CALLS")" = "$(printf 'vm-smoke\nbats\ngui')" ]
    [ ! -s "$BLOCKED" ]
}
