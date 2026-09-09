#!/usr/bin/env bats
#
# Host-only tests for the coverage floor comparison
# (ci/lib/gates/host.sh::coverage_floor_check). No VM, no pytest run: the
# function is fed a synthetic coverage JSON and a synthetic floors table.
#
# Contract under test:
#   - The measured percentage is compared EXACTLY. It used to be round()ed
#     before the comparison, so 79.6% cleared an 80% floor and every floor in
#     coverage-floors.tsv effectively sat half a point below its stated value.
#   - A positive floor with missing/unparseable data still FAILS CLOSED.
#   - The recorded row reports the exact measurement, not a rounded one.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    RDIR="$BATS_TEST_TMPDIR/run"
    mkdir -p "$RDIR/host"
    QDISTRO_REPO="$REPO_ROOT"
    # coverage_floor_check reads $QCI_DIR/coverage-floors.tsv; point QCI_DIR at a
    # synthetic table so the real floors are neither read nor depended on.
    QCI_DIR="$BATS_TEST_TMPDIR/qci"
    mkdir -p "$QCI_DIR"
    FLOORS="$QCI_DIR/coverage-floors.tsv"
    log() { :; }
    # Capture rows instead of writing a real results.tsv.
    ROWS="$BATS_TEST_TMPDIR/rows"
    : > "$ROWS"
    record_result() { printf '%s|%s|%s|%s\n' "$1" "$2" "$3" "${8:-}" >> "$ROWS"; }
    record_skip() { printf '%s|%s|skip|%s\n' "$1" "$2" "${4:-}" >> "$ROWS"; }
    rel_path() { printf '%s' "$1"; }
    exit_class_name() { printf 'host'; }
    EXIT_HOST=30
    EXIT_OK=0
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/core.sh"      # safe_name
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/gates/host.sh"
    # core.sh's own rel_path/exit_class_name are fine; re-stub the recorders that
    # would otherwise need a real run dir.
    record_result() { printf '%s|%s|%s|%s\n' "$1" "$2" "$3" "${8:-}" >> "$ROWS"; }
    record_skip() { printf '%s|%s|skip|%s\n' "$1" "$2" "${4:-}" >> "$ROWS"; }
}

# Write a coverage.py-shaped JSON with an exact percent_covered.
mkjson() {
    printf '{"totals": {"percent_covered": %s, "percent_covered_display": "%s"}}\n' "$1" "$1" \
        > "$BATS_TEST_TMPDIR/cov.json"
}

mkfloor() { printf 'project\tfloor\tnote\n%s\t%s\tsynthetic\n' "$1" "$2" > "$FLOORS"; }

@test "79.6 does NOT satisfy an 80% floor (the rounding hole)" {
    mkfloor demo 80
    mkjson 79.6
    run coverage_floor_check demo "$BATS_TEST_TMPDIR/cov.json"
    [ "$status" -ne 0 ]
    grep -q 'demo-coverage-floor|fail' "$ROWS"
}

@test "79.999 does NOT satisfy an 80% floor (rounding to hundredths is still rounding)" {
    # The first fix rounded to hundredths instead of to units. 79.999*100 rounds
    # to 8000, which cleared an 80 floor just as 79.6 had. The comparison is now
    # done on the UNROUNDED value in Python; only the display is rounded, so the
    # row may legitimately read "80.00%" while still failing.
    mkfloor demo 80
    mkjson 79.999
    run coverage_floor_check demo "$BATS_TEST_TMPDIR/cov.json"
    [ "$status" -ne 0 ]
    grep -q 'demo-coverage-floor|fail' "$ROWS"
}

@test "79.99 does NOT satisfy an 80% floor" {
    mkfloor demo 80
    mkjson 79.99
    run coverage_floor_check demo "$BATS_TEST_TMPDIR/cov.json"
    [ "$status" -ne 0 ]
}

@test "exactly 80.0 satisfies an 80% floor" {
    mkfloor demo 80
    mkjson 80.0
    run coverage_floor_check demo "$BATS_TEST_TMPDIR/cov.json"
    [ "$status" -eq 0 ]
    grep -q 'demo-coverage-floor|pass' "$ROWS"
}

@test "80.01 satisfies an 80% floor" {
    mkfloor demo 80
    mkjson 80.01
    run coverage_floor_check demo "$BATS_TEST_TMPDIR/cov.json"
    [ "$status" -eq 0 ]
}

@test "the row reports the exact measurement, not a rounded one" {
    mkfloor demo 0
    mkjson 86.64
    run coverage_floor_check demo "$BATS_TEST_TMPDIR/cov.json"
    [ "$status" -eq 0 ]
    grep -q '86.64' "$ROWS"
}

@test "a positive floor with a MISSING artifact fails closed" {
    mkfloor demo 80
    run coverage_floor_check demo "$BATS_TEST_TMPDIR/absent.json"
    [ "$status" -ne 0 ]
    grep -q 'demo-coverage-floor|fail' "$ROWS"
}

@test "a positive floor with an UNPARSEABLE artifact fails closed" {
    mkfloor demo 80
    printf 'not json at all' > "$BATS_TEST_TMPDIR/cov.json"
    run coverage_floor_check demo "$BATS_TEST_TMPDIR/cov.json"
    [ "$status" -ne 0 ]
}

@test "an istanbul/vitest coverage-final.json is measured from its 's' maps" {
    mkfloor demo 80
    # 3 of 4 statements covered = 75% -> below an 80% floor.
    printf '{"a.js": {"s": {"0": 1, "1": 1, "2": 1, "3": 0}}}\n' > "$BATS_TEST_TMPDIR/cov.json"
    run coverage_floor_check demo "$BATS_TEST_TMPDIR/cov.json"
    [ "$status" -ne 0 ]
    # Assert the MEASURED value, not merely a nonzero rc: a parser that rejected
    # every istanbul file would also produce a nonzero rc here.
    grep -q '75.00' "$ROWS"
}

@test "an istanbul file ABOVE the floor passes and reports its measurement" {
    mkfloor demo 80
    # 9 of 10 statements covered = 90%.
    printf '{"a.js": {"s": {"0":1,"1":1,"2":1,"3":1,"4":1,"5":1,"6":1,"7":1,"8":1,"9":0}}}\n' \
        > "$BATS_TEST_TMPDIR/cov.json"
    run coverage_floor_check demo "$BATS_TEST_TMPDIR/cov.json"
    [ "$status" -eq 0 ]
    grep -q 'demo-coverage-floor|pass' "$ROWS"
    grep -q '90.00' "$ROWS"
}

# --- floor SYNTAX: the comparison used to be bash arithmetic ----------------
# "010" was octal 8, "08" was a fatal arithmetic error that recorded NO row at
# all, and a 19-digit floor overflowed to a negative threshold everything
# cleared. The floor is now normalized base-10 and bounded to 0..100, and the
# comparison happens in Python.

@test "a leading-zero floor is decimal, not octal" {
    mkfloor demo 010
    mkjson 9
    run coverage_floor_check demo "$BATS_TEST_TMPDIR/cov.json"
    [ "$status" -ne 0 ]          # 9 < 10, not 9 > 8
    grep -q 'demo-coverage-floor|fail' "$ROWS"
}

@test "an invalid-octal floor records a row instead of aborting on arithmetic" {
    mkfloor demo 08
    mkjson 7
    run coverage_floor_check demo "$BATS_TEST_TMPDIR/cov.json"
    [ "$status" -ne 0 ]
    # The old code died in $((floor * 100)) and recorded nothing at all.
    [ -s "$ROWS" ]
    grep -q 'demo-coverage-floor|fail' "$ROWS"
}

@test "an out-of-range floor is malformed, never an overflowed threshold" {
    mkfloor demo 184467440737095517
    mkjson 80
    run coverage_floor_check demo "$BATS_TEST_TMPDIR/cov.json"
    [ "$status" -ne 0 ]
    grep -q 'malformed floor' "$ROWS"
}

@test "a floor above 100 is malformed" {
    mkfloor demo 101
    mkjson 80
    run coverage_floor_check demo "$BATS_TEST_TMPDIR/cov.json"
    [ "$status" -ne 0 ]
    grep -q 'malformed floor' "$ROWS"
}
