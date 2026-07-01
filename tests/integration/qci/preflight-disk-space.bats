#!/usr/bin/env bats
#
# Host-only tests for the preflight disk-space floor (ci/lib/gates/preflight.sh,
# H7). The decision is factored into pure `disk_space_verdict` (free_gib, floor)
# and a `fs_free_gib` measurement helper that walks up to the nearest existing
# ancestor. No libvirt, no run dir — sourcing the module only defines functions.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    TMP="$(mktemp -d)"
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/gates/preflight.sh"
}

teardown() { [ -n "${TMP:-}" ] && rm -rf "$TMP"; }

@test "disk_space_verdict: below floor => low + rc 1" {
    run disk_space_verdict 5 20
    [ "$status" -eq 1 ]
    [ "$output" = "low" ]
}

@test "disk_space_verdict: at floor => warn + rc 0 (within 2x)" {
    run disk_space_verdict 20 20
    [ "$status" -eq 0 ]
    [ "$output" = "warn" ]
}

@test "disk_space_verdict: between floor and 2x floor => warn" {
    run disk_space_verdict 30 20
    [ "$status" -eq 0 ]
    [ "$output" = "warn" ]
}

@test "disk_space_verdict: at 2x floor => ok" {
    run disk_space_verdict 40 20
    [ "$status" -eq 0 ]
    [ "$output" = "ok" ]
}

@test "disk_space_verdict: well above 2x floor => ok" {
    run disk_space_verdict 500 20
    [ "$status" -eq 0 ]
    [ "$output" = "ok" ]
}

@test "disk_space_verdict: just below floor => low" {
    run disk_space_verdict 19 20
    [ "$status" -eq 1 ]
    [ "$output" = "low" ]
}

@test "fs_free_gib: existing path yields a non-negative integer" {
    run fs_free_gib "$TMP"
    [ "$status" -eq 0 ]
    [[ "$output" =~ ^[0-9]+$ ]]
}

@test "fs_free_gib: nonexistent nested path walks up to an existing ancestor" {
    run fs_free_gib "$TMP/does/not/exist/yet"
    [ "$status" -eq 0 ]
    [[ "$output" =~ ^[0-9]+$ ]]
}
