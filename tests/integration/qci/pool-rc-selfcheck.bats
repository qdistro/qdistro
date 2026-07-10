#!/usr/bin/env bats
#
# Host-only test for the pool-rc vs recorded-rows self-check
# (ci/lib/run.sh::pool_rc_selfcheck, H9). The parallel bats/gui gates accumulate
# their gate rc from `wait -n` worker statuses. Every worker records a result row,
# so a POOL-gate `fail` row with a rc=0 finish means the plumbing lost a worker's
# nonzero exit: record a runner-integrity FAIL row and escalate rc to EXIT_RUNNER.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    TMP="$(mktemp -d)"; RDIR="$TMP/run"; mkdir -p "$RDIR"
    EXIT_RUNNER=90
    QDISTRO_REPO="$REPO_ROOT"
    log() { :; }
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/core.sh"
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/run.sh"
    # core.sh redefines log; re-stub it AFTER sourcing so its stderr output does
    # not merge into `run`'s captured $output.
    log() { :; }
    printf 'gate\tsubject\tstatus\texit_code\texit_class\tkind\tlog\tnotes\tcategory\n' \
        > "$RDIR/results.tsv"
}

teardown() { [ -n "${TMP:-}" ] && rm -rf "$TMP"; }

_add() { printf '%s\t%s\t%s\t0\tpass\t%s\t\t\tintegration\n' "$1" "$2" "$3" "$4" >> "$RDIR/results.tsv"; }

@test "pool rc=0 with a bats fail row escalates to EXIT_RUNNER + integrity row" {
    _add bats silo-egress.bats fail bats
    run pool_rc_selfcheck 0
    [ "$status" -eq 0 ]
    [ "$output" = "90" ]
    grep -q 'runner-integrity' "$RDIR/results.tsv"
    grep -q 'lost a worker' "$RDIR/results.tsv"
}

@test "pool rc=0 with a gui fail row also escalates" {
    _add gui permissions-gui/21.md fail agent
    run pool_rc_selfcheck 0
    [ "$output" = "90" ]
}

@test "pool rc=0 with only pass/skip rows stays 0, no integrity row" {
    _add bats a.bats pass bats
    _add gui x.md skip agent
    run pool_rc_selfcheck 0
    [ "$output" = "0" ]
    ! grep -q 'runner-integrity' "$RDIR/results.tsv"
}

@test "a nonzero pool rc is returned unchanged (nothing to check)" {
    _add bats a.bats fail bats
    run pool_rc_selfcheck 35
    [ "$output" = "35" ]
    ! grep -q 'runner-integrity' "$RDIR/results.tsv"
}

@test "a fail row from a SERIAL gate (lint) does not trigger the pool check" {
    # Serial gates propagate their own return codes directly. This helper audits
    # only background-pool status and must not reinterpret a synthetic serial row
    # as a dropped worker exit.
    _add lint flake-smells-umbrella fail lint
    run pool_rc_selfcheck 0
    [ "$output" = "0" ]
    ! grep -q 'runner-integrity' "$RDIR/results.tsv"
}

@test "an existing runner-integrity fail row does not self-trigger" {
    _add runner-integrity fragment-rows fail runner
    run pool_rc_selfcheck 0
    [ "$output" = "0" ]
    # Still exactly one runner-integrity row (no new one added).
    [ "$(grep -c 'runner-integrity' "$RDIR/results.tsv")" -eq 1 ]
}
