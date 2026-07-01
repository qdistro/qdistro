#!/usr/bin/env bats
#
# Host-only test for fragment-merge row validation + malformed quarantine
# (ci/lib/run.sh::merge_worker_fragments, H4). A worker killed mid-write can
# leave a short/truncated row in its per-worker fragment; merged blindly it
# silently misaligns report parsing. The merge must validate each row's column
# count against the canonical header, quarantine malformed rows to
# <name>.d/malformed.log, and surface a runner-integrity row.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    TMP="$(mktemp -d)"; RDIR="$TMP/run"; mkdir -p "$RDIR/results.d"
    EXIT_RUNNER=90
    QDISTRO_REPO="$REPO_ROOT"
    log() { :; }
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/core.sh"
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/run.sh"
    # Canonical results.tsv header (9 columns), matching init_run.
    printf 'gate\tsubject\tstatus\texit_code\texit_class\tkind\tlog\tnotes\tcategory\n' \
        > "$RDIR/results.tsv"
}

teardown() { [ -n "${TMP:-}" ] && rm -rf "$TMP"; }

@test "merge quarantines a short row and surfaces a runner-integrity row" {
    # One well-formed 9-column row + one truncated (5-column) row.
    {
        printf 'bats\tok.bats\tpass\t0\tpass\tbats\tbats/ok.log\t\tintegration\n'
        printf 'bats\ttrunc.bats\tfail\t35\tbats\n'   # short: worker died mid-write
    } > "$RDIR/results.d/worker1.tsv"

    merge_worker_fragments

    # The good row is merged into the canonical file.
    grep -q 'ok.bats' "$RDIR/results.tsv"
    # The short row is NOT in the canonical file (quarantined).
    ! grep -q 'trunc.bats	fail	35$' "$RDIR/results.tsv"
    # It landed in the quarantine log.
    grep -q 'trunc.bats' "$RDIR/results.d/malformed.log"
    # And a runner-integrity row was recorded.
    grep -q 'runner-integrity' "$RDIR/results.tsv"
    grep -q 'malformed worker-fragment row' "$RDIR/results.tsv"
    # The fragment is renamed .merged (idempotent, not re-merged).
    [ -e "$RDIR/results.d/worker1.tsv.merged" ]
}

@test "merge with only well-formed rows records NO integrity row" {
    printf 'bats\ta.bats\tpass\t0\tpass\tbats\t\t\tintegration\n' \
        > "$RDIR/results.d/w.tsv"
    merge_worker_fragments
    grep -q 'a.bats' "$RDIR/results.tsv"
    ! grep -q 'runner-integrity' "$RDIR/results.tsv"
    [ ! -e "$RDIR/results.d/malformed.log" ]
}

@test "merge tolerates a fragment missing its trailing newline" {
    printf 'bats\tb.bats\tpass\t0\tpass\tbats\t\t\tintegration' \
        > "$RDIR/results.d/w2.tsv"   # NO trailing newline
    merge_worker_fragments
    grep -q 'b.bats' "$RDIR/results.tsv"
    ! grep -q 'runner-integrity' "$RDIR/results.tsv"
}
