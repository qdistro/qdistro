#!/usr/bin/env bats
#
# Host-only tests for the classified-retry decision logic
# (ci/lib/gates/gui.sh::gui_classify_failure / gui_classifier_retriable /
# gui_detect_transport_marker). This is the MASKING-CRITICAL code: it decides
# what may be auto-retried, and a wrong "retriable" verdict could flake-pass a
# real product failure. The pure classifiers are host-testable without a VM.
#
# Invariants under test:
#   - status=FAIL / status=ERROR are NEVER retriable (real product/test signal),
#     even with rc=124 / a connectivity marker present.
#   - UNKNOWN:0 (clean exit, no verdict) is NEVER retriable.
#   - UNKNOWN:124 splits into transport-timeout (retriable — ONLY when an
#     unambiguous qemu guest-agent CONNECTIVITY-loss marker is present) vs
#     agent-timeout (NOT retriable — a slow agent / possible product hang).
#   - A generic vm-exec timeout is NOT a connectivity marker (possible product
#     hang). ONLY transport-timeout is retriable.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/gates/gui.sh"
}

# --- gui_classify_failure: args are (status, rc, transport_marker) ---

@test "classify: FAIL -> product-fail (never retriable)" {
    [ "$(gui_classify_failure FAIL 1 0)" = product-fail ]
    run gui_classifier_retriable product-fail
    [ "$status" -ne 0 ]
}

@test "classify: ERROR -> product-error (never retriable)" {
    [ "$(gui_classify_failure ERROR 1 0)" = product-error ]
    run gui_classifier_retriable product-error
    [ "$status" -ne 0 ]
}

@test "classify: UNKNOWN:0 -> no-verdict (never retriable)" {
    [ "$(gui_classify_failure UNKNOWN 0 0)" = no-verdict ]
    run gui_classifier_retriable no-verdict
    [ "$status" -ne 0 ]
}

@test "classify: UNKNOWN:124 + connectivity marker -> transport-timeout (retriable)" {
    [ "$(gui_classify_failure UNKNOWN 124 1)" = transport-timeout ]
    run gui_classifier_retriable transport-timeout
    [ "$status" -eq 0 ]
}

@test "classify: UNKNOWN:124 + NO connectivity marker -> agent-timeout (NOT retriable)" {
    [ "$(gui_classify_failure UNKNOWN 124 0)" = agent-timeout ]
    run gui_classifier_retriable agent-timeout
    [ "$status" -ne 0 ]
}

@test "classify: FAIL:124 -> product-fail, NOT a timeout class" {
    # A real product/test FAIL that also happened to time out is product evidence,
    # never an infra timeout. The connectivity marker must not flip it retriable.
    [ "$(gui_classify_failure FAIL 124 1)" = product-fail ]
}

@test "classify: PASS with a timeout rc is unknown (inconsistent), not retriable" {
    # Claimed PASS but rc=124: an inconsistent agent result, keyed off UNKNOWN so
    # it is NOT a timeout class.
    [ "$(gui_classify_failure PASS 124 1)" = unknown ]
    run gui_classifier_retriable unknown
    [ "$status" -ne 0 ]
}

@test "classify: UNKNOWN with a non-124 rc is unknown, not a timeout class" {
    [ "$(gui_classify_failure UNKNOWN 1 1)" = unknown ]
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

@test "transport marker: detects qemu guest-agent connectivity loss" {
    local log="$BATS_TEST_TMPDIR/a.log"
    echo "virsh: error: Guest agent is not responding: QEMU guest agent is not connected" > "$log"
    run gui_detect_transport_marker "$log"
    [ "$status" -eq 0 ]
}

@test "transport marker: a generic vm-exec timeout is NOT a transport marker" {
    # vm-exec's own deadline fires on a wedged GUEST command — equally a PRODUCT
    # hang. It must NOT be classed as retriable transport (masking risk, codex).
    local log="$BATS_TEST_TMPDIR/b.log"
    echo "[vm-exec] command timed out after 1800s; rc=124" > "$log"
    run gui_detect_transport_marker "$log"
    [ "$status" -ne 0 ]
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
