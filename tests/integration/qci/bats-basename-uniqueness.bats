#!/usr/bin/env bats
#
# Host-only test for the bats-gate scheduled-basename uniqueness assert
# (ci/lib/gates/bats.sh, H10). Result rows + per-file VM names key on the
# basename, so a duplicate in the SCHEDULED set would silently collide. The pure
# assert_unique_bats_basenames helper takes path args and fails on any duplicate
# basename, naming both paths. Uses a tempdir fixture; no VM, no libvirt.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    TMP="$(mktemp -d)"
    mkdir -p "$TMP/vm" "$TMP/other"
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/gates/bats.sh"
}

teardown() { [ -n "${TMP:-}" ] && rm -rf "$TMP"; }

@test "unique basenames pass" {
    : > "$TMP/vm/a.bats"; : > "$TMP/vm/b.bats"; : > "$TMP/vm/c.bats"
    run assert_unique_bats_basenames "$TMP/vm/a.bats" "$TMP/vm/b.bats" "$TMP/vm/c.bats"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "empty schedule passes" {
    run assert_unique_bats_basenames
    [ "$status" -eq 0 ]
}

@test "duplicate basename across dirs fails, naming both paths" {
    : > "$TMP/vm/dup.bats"; : > "$TMP/other/dup.bats"
    run assert_unique_bats_basenames "$TMP/vm/dup.bats" "$TMP/other/dup.bats"
    [ "$status" -ne 0 ]
    [[ "$output" == *"duplicate scheduled bats basename dup.bats"* ]]
    [[ "$output" == *"$TMP/vm/dup.bats"* ]]
    [[ "$output" == *"$TMP/other/dup.bats"* ]]
}

@test "the real cross-dir dup is only a problem if BOTH are scheduled" {
    # Mirrors the real repo dup: only the vm/ copy is scheduled today -> passes.
    : > "$TMP/vm/backup-rehearse-e2e.bats"
    run assert_unique_bats_basenames "$TMP/vm/backup-rehearse-e2e.bats" "$TMP/vm/a.bats"
    [ "$status" -eq 0 ]
    # If discovery is ever widened to include the sibling copy too -> fails.
    : > "$TMP/other/backup-rehearse-e2e.bats"
    run assert_unique_bats_basenames \
        "$TMP/vm/backup-rehearse-e2e.bats" "$TMP/other/backup-rehearse-e2e.bats"
    [ "$status" -ne 0 ]
    [[ "$output" == *"backup-rehearse-e2e.bats"* ]]
}
