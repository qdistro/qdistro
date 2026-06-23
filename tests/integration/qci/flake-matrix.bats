#!/usr/bin/env bats
#
# Host-only tests for ci/bin/flake-matrix.py — the cross-run recurrence-matrix
# generator (Phase 2 observability). Builds a fixture runs dir with known
# results.tsv files and asserts the rendered cells (. pass, F fail, s skip,
# B blocked, - absent). No qci/VM involvement.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    SCRIPT="$REPO_ROOT/ci/bin/flake-matrix.py"
    RUNS="$BATS_TEST_TMPDIR/runs"
    mkdir -p "$RUNS"
    # Two runs, newest first by name: full-...-200 then full-...-100.
    mk_run "full-20260101T000000Z-100" \
        "gui	scenarioA	pass" \
        "gui	scenarioB	fail" \
        "gui	scenarioC	skip"
    mk_run "full-20260102T000000Z-200" \
        "gui	scenarioA	fail" \
        "gui	scenarioB	pass" \
        "gui	scenarioD	blocked"
}

# mk_run <dirname> <tab-separated "gate<TAB>subject<TAB>status" rows...>
mk_run() {
    local dir="$RUNS/$1"; shift
    mkdir -p "$dir"
    {
        printf 'gate\tsubject\tstatus\texit_code\texit_class\tkind\tlog\tnotes\tcategory\n'
        local row
        for row in "$@"; do printf '%s\t\t\t\t\t\t\n' "$row"; done
    } > "$dir/results.tsv"
}

matrix() {
    python3 "$SCRIPT" --runs-dir "$RUNS" --gate gui "$@"
}

@test "flake-matrix: newest run is the leftmost column" {
    run matrix --all
    [ "$status" -eq 0 ]
    # Header column order: 200 (newest) before 100 (older).
    header="$(printf '%s\n' "$output" | head -1)"
    [[ "$header" == *"200"*"100"* ]]
}

@test "flake-matrix: cells map pass/fail/skip/blocked/absent correctly" {
    run matrix --all
    [ "$status" -eq 0 ]
    # scenarioA: newest=fail(F) older=pass(.)
    line_a="$(printf '%s\n' "$output" | grep 'scenarioA')"
    [[ "$line_a" == *"F"*"."* ]]
    # scenarioC only in older run -> newest absent(-), older skip(s)
    line_c="$(printf '%s\n' "$output" | grep 'scenarioC')"
    [[ "$line_c" == *"-"*"s"* ]]
    # scenarioD only in newest run -> blocked(B) then absent(-)
    line_d="$(printf '%s\n' "$output" | grep 'scenarioD')"
    [[ "$line_d" == *"B"*"-"* ]]
}

@test "flake-matrix: default view shows only ever-non-pass scenarios" {
    run matrix
    [ "$status" -eq 0 ]
    # A (fail), B (fail in older), C (never fail/block but... C is skip-only) ,
    # D (blocked) -> A, B, D shown; C is skip-only so it is filtered out.
    [[ "$output" == *"scenarioA"* ]]
    [[ "$output" == *"scenarioB"* ]]
    [[ "$output" == *"scenarioD"* ]]
    ! [[ "$output" == *"scenarioC"* ]]
}

@test "flake-matrix: --columns prints a per-run tally" {
    run matrix --all --columns
    [ "$status" -eq 0 ]
    [[ "$output" == *"pass"*"fail"*"skip"*"blocked"* ]]
}

@test "flake-matrix: missing runs dir exits nonzero with a message" {
    run python3 "$SCRIPT" --runs-dir "$BATS_TEST_TMPDIR/nope" --gate gui
    [ "$status" -ne 0 ]
    [[ "$output" == *"not found"* ]]
}
