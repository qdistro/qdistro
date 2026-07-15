#!/usr/bin/env bats
# Invalid explicit GUI scenarios must fail before any golden, VM, or agent work.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    source "$REPO_ROOT/ci/lib/gates/gui.sh"
    EXIT_USAGE=2
    EXIT_GUI=70
    QCI_MARKERS="$BATS_TEST_TMPDIR/markers"
    : > "$QCI_MARKERS"

    qci_assert_run_dir() { return 0; }
    record_blocked() { printf 'blocked:%s:%s\n' "$2" "$5" >> "$QCI_MARKERS"; }
    gui_isolate_host_desktop() { echo isolated >> "$QCI_MARKERS"; }
    ensure_run_golden() { echo golden >> "$QCI_MARKERS"; }
    acquire_vm() { echo acquired >> "$QCI_MARKERS"; return 1; }
    agent_scenarios() { printf '%s\n' "$TEST_SCENARIO"; }
}

@test "gui gate rejects a missing scenario before desktop setup or provisioning" {
    local missing="$BATS_TEST_TMPDIR/does-not-exist.md"
    TEST_SCENARIO=$missing

    run gate_gui

    [ "$status" -eq "$EXIT_USAGE" ]
    grep -q "blocked:$missing:" "$QCI_MARKERS"
    ! grep -qE '^(isolated|golden|acquired)$' "$QCI_MARKERS"
}

@test "scenario validation rejects a non-markdown file" {
    local scenario="$BATS_TEST_TMPDIR/scenario.txt"
    : > "$scenario"
    TEST_SCENARIO=$scenario

    run gui_validate_scenarios

    [ "$status" -eq "$EXIT_USAGE" ]
    grep -q "blocked:$scenario:" "$QCI_MARKERS"
}

@test "scenario validation accepts an existing readable markdown file" {
    local scenario="$BATS_TEST_TMPDIR/scenario.md"
    : > "$scenario"
    TEST_SCENARIO=$scenario

    run gui_validate_scenarios

    [ "$status" -eq 0 ]
    [ ! -s "$QCI_MARKERS" ]
}
