#!/usr/bin/env bats
#
# Host-only test for the classifier-drift alarm sidecar capture
# (ci/lib/gates/gui.sh::gui_capture_unmatched_tail, H6b). A FAILING agent attempt
# that matched NO infra/tooling marker fell through to a generic classifier; the
# gui gate snapshots the last ~20 log lines into a per-scenario sidecar so a
# drifted marker string is preserved for inspection (report.py counts these rows).

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    TMP="$(mktemp -d)"
    # rel_path is defined in core.sh; stub it so the helper is testable standalone.
    rel_path() { printf '%s' "$1"; }
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/gates/gui.sh"
}

teardown() { [ -n "${TMP:-}" ] && rm -rf "$TMP"; }

@test "capture writes the last 20 log lines into the sidecar" {
    local log="$TMP/x.agent.log" sidecar="$TMP/x.unmatched-tail.txt"
    seq 1 40 > "$log"
    gui_capture_unmatched_tail "$log" "$sidecar"
    [ -f "$sidecar" ]
    # The tail (line 40) is present, an early line (line 5) is not.
    grep -qx '40' "$sidecar"
    ! grep -qx '5' "$sidecar"
    # The drift header is written.
    grep -q 'classifier-drift watch' "$sidecar"
}

@test "capture is a silent no-op on a missing log" {
    local sidecar="$TMP/none.unmatched-tail.txt"
    run gui_capture_unmatched_tail "$TMP/does-not-exist.agent.log" "$sidecar"
    [ "$status" -eq 0 ]
    [ ! -e "$sidecar" ]
}

@test "capture appends (a second failing attempt does not clobber the first)" {
    local log="$TMP/y.agent.log" sidecar="$TMP/y.unmatched-tail.txt"
    printf 'first-run-line\n' > "$log"
    gui_capture_unmatched_tail "$log" "$sidecar"
    printf 'second-run-line\n' > "$log"
    gui_capture_unmatched_tail "$log" "$sidecar"
    grep -q 'first-run-line' "$sidecar"
    grep -q 'second-run-line' "$sidecar"
}
