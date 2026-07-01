#!/usr/bin/env bats
#
# Host-only tests for the per-scenario scratch-dir contract
# (ci/lib/run.sh::scenario_scratch_dir). Every agent scenario and bats worker
# gets a UNIQUE scratch dir under the run tree so a fixed shared path cannot
# collide once GUI/bats lanes run in parallel (todo/qci1 P0 isolation contract).
# The path derivation is pure, so it is host-testable without a run.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    RDIR="$BATS_TEST_TMPDIR/run"
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/run.sh"
}

@test "scratch dir is under the run tree, keyed by gate + slug" {
    [ "$(scenario_scratch_dir gui my-scenario)" = "$RDIR/gui/my-scenario.scratch" ]
    [ "$(scenario_scratch_dir bats foo.bats)" = "$RDIR/bats/foo.bats.scratch" ]
}

@test "scratch dir is unique per scenario slug (no cross-scenario collision)" {
    a=$(scenario_scratch_dir gui scenario-a)
    b=$(scenario_scratch_dir gui scenario-b)
    [ "$a" != "$b" ]
}

@test "scratch dir separates lanes with the same slug" {
    # The same base name under two gates must not share a scratch dir.
    [ "$(scenario_scratch_dir gui x)" != "$(scenario_scratch_dir bats x)" ]
}

@test "scratch dir tracks RDIR (stays inside the current run)" {
    RDIR=/tmp/other-run
    [ "$(scenario_scratch_dir gui s)" = "/tmp/other-run/gui/s.scratch" ]
}
