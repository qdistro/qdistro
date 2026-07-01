#!/usr/bin/env bats
#
# Host-only test for the split flake-lint metric (ci/lib/gates/lint.sh::
# lint_flake_smells + ci/bin/scenario-flake-lint.py --set). Two SEPARATE strict
# metrics must be reported as SEPARATE rows so the report can never conflate
# "qdistro-owned is strict-clean" with "umbrella is strict-clean":
#   qdistro-owned = qdistro/tests/integration/{permissions-gui,qdwin-noctalia}
#   umbrella      = the full path set `qci gui` schedules (qdistro + siblings)

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    SCRIPT="$REPO_ROOT/ci/bin/scenario-flake-lint.py"
    TMP="$(mktemp -d)"
    ROWS="$TMP/rows.tsv"; : > "$ROWS"
    EXIT_OK=0; EXIT_HOST=30
    QDISTRO_REPO="$REPO_ROOT"
    QCI_BIN_DIR="$REPO_ROOT/ci/bin"
    log() { :; }
    # Record subject<TAB>status so the two-row contract is assertable.
    record_result() { printf '%s\t%s\n' "$2" "$3" >> "$ROWS"; }
    record_skip() { printf '%s\t%s\n' "$2" skip >> "$ROWS"; }
    rel_path() { printf '%s' "$1"; }
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/gates/lint.sh"
}

teardown() { [ -n "${TMP:-}" ] && rm -rf "$TMP"; }

# --- the linter's --set path selection ---

@test "flake-lint: --set qdistro and --set umbrella are both accepted" {
    run python3 "$SCRIPT" --set qdistro --format summary
    [ "$status" -eq 0 ]   # warn-only default
    run python3 "$SCRIPT" --set umbrella --format summary
    [ "$status" -eq 0 ]
}

@test "flake-lint: umbrella file count >= qdistro-owned (distinct, super-set)" {
    local q u
    q=$(python3 "$SCRIPT" --set qdistro --format summary 2>&1 1>/dev/null \
        | awk '/scenario-flake-lint:/{print $5; exit}')
    u=$(python3 "$SCRIPT" --set umbrella --format summary 2>&1 1>/dev/null \
        | awk '/scenario-flake-lint:/{print $5; exit}')
    [ "$q" -gt 0 ]
    [ "$u" -ge "$q" ]
}

@test "flake-lint: an unknown --set value is rejected" {
    run python3 "$SCRIPT" --set bogus
    [ "$status" -ne 0 ]
}

# --- the gate records TWO distinct rows ---

@test "gate lint records two distinct flake-smells rows (qdistro + umbrella)" {
    lint_flake_smells "$TMP/lint.log"
    grep -q '^flake-smells-qdistro	' "$ROWS"
    grep -q '^flake-smells-umbrella	' "$ROWS"
    # Exactly two flake-smells rows — never a single conflated row.
    [ "$(grep -c '^flake-smells-' "$ROWS")" -eq 2 ]
}

@test "gate lint: QCI_FLAKE_STRICT=1 escalates each dirty set to a fail row" {
    # Both sets currently carry non-allowlisted findings in-repo, so strict fails
    # BOTH — proving the escalation semantics are preserved per-set.
    QCI_FLAKE_STRICT=1 lint_flake_smells "$TMP/lint.log"
    grep -q '^flake-smells-qdistro	fail$' "$ROWS"
    grep -q '^flake-smells-umbrella	fail$' "$ROWS"
}

@test "gate lint: warn-only (default) records skip rows, not fail" {
    lint_flake_smells "$TMP/lint.log"
    ! grep -q '	fail$' "$ROWS"
    grep -q '^flake-smells-qdistro	skip$' "$ROWS"
    grep -q '^flake-smells-umbrella	skip$' "$ROWS"
}
