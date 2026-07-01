#!/usr/bin/env bats
#
# Host-only tests for the per-worker result-fragment routing + merge
# (ci/lib/run.sh::record_dest / merge_worker_fragments). Under a parallel worker
# pool each worker appends to its own `<name>.d/<worker>.tsv`; the parent folds
# the fragments back into the canonical TSV at finish_run. The DEFAULT (serial,
# no worker id) path must be byte-identical to the historic canonical append.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    RDIR="$BATS_TEST_TMPDIR/run"
    mkdir -p "$RDIR"
    printf 'H\n' > "$RDIR/results.tsv"   # canonical header (1 column)
    # merge_worker_fragments now records a runner-integrity row (and logs) when it
    # quarantines a malformed fragment row, so stub log + the record primitives.
    log() { :; }
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/core.sh"
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/run.sh"
}

@test "worker_fragment_id: same basename in different dirs yields distinct ids" {
    a=$(worker_fragment_id bats "tests/a/x.bats")
    b=$(worker_fragment_id bats "tests/b/x.bats")
    [ "$a" != "$b" ]            # no shared fragment => no concurrent append
    [[ "$a" == bats-* ]]
}

@test "worker_fragment_id: a very long subject is length-bounded" {
    long=$(printf 'tests/integration/permissions-gui/%0.sx' {1..400})
    id=$(worker_fragment_id gui "$long")
    [ "${#id}" -lt 160 ]       # slug truncated + hash, not an overlong filename
}

@test "merge: a fragment with no trailing newline is newline-guarded" {
    mkdir -p "$RDIR/flake.d"
    printf 'H\n' > "$RDIR/flake.tsv"   # 1-column header (matches the toy rows)
    printf 'partial-row-no-newline' > "$RDIR/flake.d/w1.tsv"   # crashed mid-write
    printf 'next-row\n' > "$RDIR/flake.d/w2.tsv"
    merge_worker_fragments
    # The two rows must be on separate lines, not concatenated.
    grep -qx 'partial-row-no-newline' "$RDIR/flake.tsv"
    grep -qx 'next-row' "$RDIR/flake.tsv"
}

@test "record_dest: serial run (no worker id) writes the canonical file" {
    [ "$(record_dest results)" = "$RDIR/results.tsv" ]
    # Flag on but no worker id => still canonical (safety).
    QCI_RESULT_FRAGMENTS=1 [ "$(QCI_RESULT_FRAGMENTS=1 record_dest results)" = "$RDIR/results.tsv" ]
}

@test "record_dest: a parallel worker writes a per-worker fragment under <name>.d" {
    run env QCI_RESULT_FRAGMENTS=1 QCI_WORKER_ID=gui-alpha \
        bash -c "RDIR='$RDIR'; source '$REPO_ROOT/ci/lib/run.sh'; record_dest results"
    [ "$output" = "$RDIR/results.d/gui-alpha.tsv" ]
    [ -d "$RDIR/results.d" ]   # created on demand
}

@test "record_dest: fragments OFF (flag=0) writes canonical even with a worker id" {
    run env QCI_RESULT_FRAGMENTS=0 QCI_WORKER_ID=gui-alpha \
        bash -c "RDIR='$RDIR'; source '$REPO_ROOT/ci/lib/run.sh'; record_dest results"
    [ "$output" = "$RDIR/results.tsv" ]
}

@test "merge_worker_fragments: folds fragments into canonical, preserving them" {
    mkdir -p "$RDIR/results.d"
    printf 'row-b\n' > "$RDIR/results.d/gui-b.tsv"
    printf 'row-a\n' > "$RDIR/results.d/gui-a.tsv"
    merge_worker_fragments
    # Canonical now has the header plus both rows, in sorted worker order.
    [ "$(cat "$RDIR/results.tsv")" = "$(printf 'H\nrow-a\nrow-b')" ]
    # Fragments are kept for post-mortem, renamed so they are not re-merged.
    [ -f "$RDIR/results.d/gui-a.tsv.merged" ]
    [ ! -f "$RDIR/results.d/gui-a.tsv" ]
}

@test "merge_worker_fragments: is idempotent (a second merge does not double-append)" {
    mkdir -p "$RDIR/timings.d"
    printf 'H\n' > "$RDIR/timings.tsv"   # 1-column header (matches the toy row)
    printf 'row1\n' > "$RDIR/timings.d/w1.tsv"
    merge_worker_fragments
    merge_worker_fragments   # second call: nothing new to merge
    [ "$(grep -c 'row1' "$RDIR/timings.tsv")" -eq 1 ]
}

@test "merge_worker_fragments: no-op when there are no fragment dirs (serial run)" {
    local before; before=$(cat "$RDIR/results.tsv")
    merge_worker_fragments
    [ "$(cat "$RDIR/results.tsv")" = "$before" ]
}

@test "end-to-end: a worker's record_dest write lands in canonical after merge" {
    # Simulate a worker appending one row via the same record_dest a writer uses.
    # The canonical header is 1 column (setup), so the worker row must be 1 column
    # too (merge validates the fragment row's column count against the header).
    env QCI_RESULT_FRAGMENTS=1 QCI_WORKER_ID=gui-x bash -c "
        RDIR='$RDIR'; source '$REPO_ROOT/ci/lib/run.sh'
        printf 'scenario-x\n' >> \"\$(record_dest results)\""
    # Before merge the canonical file is unchanged (only the header).
    [ "$(cat "$RDIR/results.tsv")" = "H" ]
    merge_worker_fragments
    [[ "$(cat "$RDIR/results.tsv")" == *"scenario-x"* ]]
}
