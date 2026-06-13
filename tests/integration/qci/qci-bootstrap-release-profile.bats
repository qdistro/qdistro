#!/usr/bin/env bats
#
# Host-only tests for the `qci bootstrap-release-profile` gate (R2;
# 05-agent-test-plan.md §A). The gate runs the host-static bootstrap-contract
# bats HOST-ONLY and fails closed on a `not ok`, a bats crash, a TAP-plan
# mismatch (partial run), a SKIPPED contract test, an eroded suite (below its
# pinned floor), a missing file, or an empty/whitespace suite list. These tests
# exercise that parsing + the fail-open guards against fast SYNTHETIC fixtures
# via the QCI_BOOTSTRAP_RELEASE_BATS(_DIR) overrides — plus a cheap check that
# the REAL default suites still declare at least their pinned floors (so test
# deletion in the shipped suites cannot hide behind fixture-only coverage).

setup() {
    REPO_ROOT_SRC="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    QCI="$REPO_ROOT_SRC/ci/bin/qci"
    [ -x "$QCI" ] || { echo "qci not found at $QCI" >&2; return 1; }
    RUNS="$(mktemp -d)"; export QCI_RUNS_DIR="$RUNS"
    FX="$(mktemp -d)"; export QCI_BOOTSTRAP_RELEASE_BATS_DIR="$FX"
}

teardown() { rm -rf "$RUNS" "$FX"; }

# Write a bats fixture $FX/$1 whose body is $2.
mkbats() {
    cat > "$FX/$1" <<EOF
#!/usr/bin/env bats
$2
EOF
}

run_gate() {
    run "$QCI" bootstrap-release-profile
    local latest
    latest=$(find "$RUNS" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' \
                 | sort -nr | awk 'NR==1{print $2}')
    RESULTS="$latest/results.tsv"
}

row_is() {
    local subject="$1" want="$2" got
    got=$(awk -F'\t' -v s="$subject" '$2==s {print $3; exit}' "$RESULTS")
    [ "$got" = "$want" ]
}

row_note_has() {
    local subject="$1" want="$2" note
    note=$(awk -F'\t' -v s="$subject" '$2==s {print $8; exit}' "$RESULTS")
    case "$note" in *"$want"*) return 0 ;; *) return 1 ;; esac
}

@test "bootstrap-release-profile: all-passing contract suite => pass exit 0" {
    mkbats ok.bats '@test "a" { true; }'$'\n''@test "b" { true; }'
    export QCI_BOOTSTRAP_RELEASE_BATS="ok.bats"
    run_gate
    [ "$status" -eq 0 ] || { echo "$output" >&2; cat "$RESULTS" >&2; return 1; }
    row_is ok.bats pass
    row_note_has ok.bats "2 passing"
}

@test "bootstrap-release-profile: a failing contract test => fail 15" {
    mkbats bad.bats '@test "a" { true; }'$'\n''@test "b" { false; }'
    export QCI_BOOTSTRAP_RELEASE_BATS="bad.bats"
    run_gate
    [ "$status" -eq 15 ] || { echo "status=$status: $output" >&2; return 1; }
    row_is bad.bats fail
    row_note_has bad.bats "failing"
}

@test "bootstrap-release-profile: a SKIPPED contract test => fail 15 (not counted as a pass)" {
    mkbats sk.bats '@test "a" { true; }'$'\n''@test "b" { skip "disabled"; }'
    export QCI_BOOTSTRAP_RELEASE_BATS="sk.bats"
    run_gate
    [ "$status" -eq 15 ] || { echo "status=$status: $output" >&2; cat "$RESULTS" >&2; return 1; }
    row_is sk.bats fail
    row_note_has sk.bats "skipped"
}

@test "bootstrap-release-profile: a vacuous 0-test suite => fail 15 (no silent pass)" {
    mkbats empty.bats '# no @test blocks at all'
    export QCI_BOOTSTRAP_RELEASE_BATS="empty.bats"
    run_gate
    [ "$status" -eq 15 ] || { echo "status=$status: $output" >&2; return 1; }
    row_is empty.bats fail
}

@test "bootstrap-release-profile: a shipped suite gutted below its floor => fail 15 (erosion)" {
    # A fixture using a DEFAULT suite name (floor 32) with a single test must NOT
    # pass — this is the contract-erosion guard.
    mkbats bootstrap-hardening.bats '@test "stub" { true; }'
    export QCI_BOOTSTRAP_RELEASE_BATS="bootstrap-hardening.bats"
    run_gate
    [ "$status" -eq 15 ] || { echo "status=$status: $output" >&2; return 1; }
    row_is bootstrap-hardening.bats fail
    row_note_has bootstrap-hardening.bats "floor 32"
}

@test "bootstrap-release-profile: a missing contract suite => fail 15" {
    export QCI_BOOTSTRAP_RELEASE_BATS="nonexistent.bats"
    run_gate
    [ "$status" -eq 15 ] || { echo "status=$status: $output" >&2; return 1; }
    row_is nonexistent.bats fail
    row_note_has nonexistent.bats "missing"
}

@test "bootstrap-release-profile: empty/whitespace suite list => fail 15 (no zero-suite pass)" {
    export QCI_BOOTSTRAP_RELEASE_BATS="   "
    run_gate
    [ "$status" -eq 15 ] || { echo "status=$status: $output" >&2; cat "$RESULTS" >&2; return 1; }
    row_is suites fail
    row_note_has suites "no contract suites selected"
}

@test "bootstrap-release-profile: one bad suite among good ones still fails the gate" {
    mkbats good.bats '@test "a" { true; }'
    mkbats badx.bats '@test "b" { false; }'
    export QCI_BOOTSTRAP_RELEASE_BATS="good.bats badx.bats"
    run_gate
    [ "$status" -eq 15 ] || { echo "status=$status: $output" >&2; return 1; }
    row_is good.bats pass
    row_is badx.bats fail
}

@test "real default contract suites still declare at least their pinned floors" {
    # Cheap (no execution): bats --count parses the @test blocks. Guards against
    # silent test deletion in the shipped suites going unnoticed by the
    # fixture-only tests above. Floors mirror release_contract_floor in qci.
    local vmdir="$REPO_ROOT_SRC/tests/integration/vm"
    local n
    n=$(bats --count "$vmdir/bootstrap-hardening.bats");       [ "$n" -ge 32 ] || { echo "bootstrap-hardening=$n < 32" >&2; return 1; }
    n=$(bats --count "$vmdir/source-manifest-signature.bats"); [ "$n" -ge 18 ] || { echo "source-manifest-signature=$n < 18" >&2; return 1; }
    n=$(bats --count "$vmdir/gen-source-manifest.bats");       [ "$n" -ge 32 ] || { echo "gen-source-manifest=$n < 32" >&2; return 1; }
}
