#!/usr/bin/env bats
#
# Host-only tests for the classified-retry decision logic
# (ci/lib/gates/gui.sh::gui_classify_failure / gui_classifier_retriable /
# gui_detect_transport_marker). This is the MASKING-CRITICAL code: it decides
# what may be auto-retried, and a wrong "retriable" verdict could flake-pass a
# real product failure. The pure classifiers are host-testable without a VM.
#
# Invariants under test:
#   - status=FAIL / status=ERROR are NEVER retriable (real product/test signal).
#   - UNKNOWN:0 (clean exit, no verdict) is NEVER retriable.
#   - rc=124 + no status splits into transport-timeout (retriable, infra marker
#     present) vs agent-timeout (NOT retriable — possible product hang).
#   - ONLY transport-timeout is retriable.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/gates/gui.sh"
}

# --- gui_classify_failure: status_present / transport flags as last two args ---

@test "classify: FAIL -> product-fail (never retriable)" {
    [ "$(gui_classify_failure FAIL 1 1 0)" = product-fail ]
    run gui_classifier_retriable product-fail
    [ "$status" -ne 0 ]
}

@test "classify: ERROR -> product-error (never retriable)" {
    [ "$(gui_classify_failure ERROR 1 0 0)" = product-error ]
    run gui_classifier_retriable product-error
    [ "$status" -ne 0 ]
}

@test "classify: UNKNOWN:0 -> no-verdict (never retriable)" {
    [ "$(gui_classify_failure UNKNOWN 0 0 0)" = no-verdict ]
    run gui_classifier_retriable no-verdict
    [ "$status" -ne 0 ]
}

@test "classify: rc=124 + no status + transport marker -> transport-timeout (retriable)" {
    [ "$(gui_classify_failure UNKNOWN 124 0 1)" = transport-timeout ]
    run gui_classifier_retriable transport-timeout
    [ "$status" -eq 0 ]
}

@test "classify: rc=124 + no status + NO transport marker -> agent-timeout (NOT retriable)" {
    [ "$(gui_classify_failure UNKNOWN 124 0 0)" = agent-timeout ]
    run gui_classifier_retriable agent-timeout
    [ "$status" -ne 0 ]
}

@test "classify: rc=124 but a status.txt IS present -> not a timeout class" {
    # status present means the agent rendered a verdict; the 124 is incidental.
    # Must NOT become transport/agent-timeout (those require no status).
    result="$(gui_classify_failure UNKNOWN 124 1 1)"
    [ "$result" != transport-timeout ]
    [ "$result" != agent-timeout ]
}

@test "classify: PASS with nonzero rc is unknown, not retriable" {
    [ "$(gui_classify_failure PASS 1 1 0)" = unknown ]
    run gui_classifier_retriable unknown
    [ "$status" -ne 0 ]
}

@test "classifier_retriable: only transport-timeout is retriable" {
    for c in product-fail product-error no-verdict agent-timeout unknown ""; do
        run gui_classifier_retriable "$c"
        [ "$status" -ne 0 ]
    done
    run gui_classifier_retriable transport-timeout
    [ "$status" -eq 0 ]
}

# --- gui_detect_transport_marker: log scan ---

@test "transport marker: detects qemu guest-agent failure in the log" {
    local log="$BATS_TEST_TMPDIR/a.log"
    echo "virsh: error: Guest agent is not responding: QEMU guest agent is not connected" > "$log"
    run gui_detect_transport_marker "$log"
    [ "$status" -eq 0 ]
}

@test "transport marker: detects vm-exec 124 in the log" {
    local log="$BATS_TEST_TMPDIR/b.log"
    echo "[vm-exec] command timed out after 1800s; rc=124" > "$log"
    run gui_detect_transport_marker "$log"
    [ "$status" -eq 0 ]
}

@test "transport marker: a plain slow agent log is NOT a transport marker" {
    local log="$BATS_TEST_TMPDIR/c.log"
    printf 'reading scenario...\nrunning step 3...\ntaking screenshot...\n' > "$log"
    run gui_detect_transport_marker "$log"
    [ "$status" -ne 0 ]
}

@test "transport marker: missing log file is not a marker" {
    run gui_detect_transport_marker "$BATS_TEST_TMPDIR/nope.log"
    [ "$status" -ne 0 ]
}
