#!/usr/bin/env bats
#
# Host-only tests for the QCI_RELEASE release-profile mode (05-agent-test-plan.md
# §A): under QCI_RELEASE=1 a `blocked` or `skip` row in a release-relevant gate
# is FATAL, so "green" means every required row actually ran. NO VM. The
# release-manifest gate is the convenient blocked-row producer: an unpopulated
# manifest records a `blocked` row and exits 0 in normal mode.

setup() {
    REPO_ROOT_SRC="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    QCI="$REPO_ROOT_SRC/ci/bin/qci"
    [ -x "$QCI" ] || { echo "qci not found at $QCI" >&2; return 1; }
    RUNS="$(mktemp -d)"; export QCI_RUNS_DIR="$RUNS"
    RR="$(mktemp -d)"
    MANIFEST="$RR/source-manifest.txt"; export QDISTRO_RELEASE_MANIFEST="$MANIFEST"
    printf '# unpopulated manifest -> release-manifest records a `blocked` row\n' > "$MANIFEST"
}

teardown() { rm -rf "$RUNS" "$RR"; }

# Run a gate; set $status/$output and expose $RESULTS.
run_gate() {
    run "$QCI" "$@"
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

row_present() {
    awk -F'\t' -v s="$1" 'BEGIN{f=2} $2==s{f=0} END{exit f}' "$RESULTS"
}

row_note_has() {
    local subject="$1" want="$2" note
    note=$(awk -F'\t' -v s="$subject" '$2==s {print $8; exit}' "$RESULTS")
    case "$note" in *"$want"*) return 0 ;; *) return 1 ;; esac
}

@test "release-profile: normal mode — a blocked release row exits 0 (no escalation)" {
    run_gate release-manifest
    [ "$status" -eq 0 ] || { echo "$output" >&2; return 1; }
    row_is unpopulated blocked
    run row_present release-profile
    [ "$status" -ne 0 ]   # no release-profile escalation row was recorded
}

@test "release-profile: QCI_RELEASE=1 — a blocked release row is fatal (exit 15)" {
    QCI_RELEASE=1 run_gate release-manifest
    [ "$status" -eq 15 ] || { echo "status=$status: $output" >&2; cat "$RESULTS" >&2; return 1; }
    row_is unpopulated blocked          # the original row is preserved...
    row_is "incomplete-rows" fail       # ...and the escalation row is recorded
    row_note_has "incomplete-rows" "blocked:release-manifest/unpopulated"
}

@test "release-profile: QCI_RELEASE=1 escalation names the fatal gate scope" {
    QCI_RELEASE=1 run_gate release-manifest
    [ "$status" -eq 15 ]
    # The escalation note must reference the release-relevant gate, proving the
    # fatal-gate scope matched (not a blanket 'any blocked row' escalation).
    row_note_has "incomplete-rows" "QCI_RELEASE=1"
}

@test "release-profile: skip rows in fatal gates are selected as incomplete" {
    local rows="$RR/results.tsv"
    printf 'gate\tsubject\tstatus\texit_code\texit_class\tkind\tlog\tnotes\tcategory\n' > "$rows"
    printf 'gui\tapp-compat\tskip\t0\tpass\tagent\t\tdeps absent\tgui\n' >> "$rows"
    printf 'preflight\toptional-tool\tskip\t0\tpass\ttool\t\toptional\tunit\n' >> "$rows"

    run bash -c '
        QCI_RELEASE_FATAL_GATES="vm-smoke bats gui release-manifest bootstrap-release-profile"
        source "$1/ci/lib/run.sh"
        release_profile_incomplete_rows "$2"
    ' _ "$REPO_ROOT_SRC" "$rows"
    [ "$status" -eq 0 ]
    [ "$output" = "skip:gui/app-compat" ]
}

# NB: the "a genuinely-green release run is NOT falsely escalated by
# QCI_RELEASE=1" property is proven in release-manifest-gate.bats, against a
# fully-signed + complete manifest fixture (no incomplete rows). It is not retested
# here to avoid a recursive `qci selftest` (this file is part of that suite).
