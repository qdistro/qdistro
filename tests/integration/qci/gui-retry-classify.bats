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

@test "classifier_retriable: only the infra signatures are retriable" {
    for c in product-fail product-error no-verdict agent-timeout unknown ""; do
        run gui_classifier_retriable "$c"
        [ "$status" -ne 0 ]
    done
    for c in transport-timeout agent-tooling agent-api-unreachable; do
        run gui_classifier_retriable "$c"
        [ "$status" -eq 0 ]
    done
}

# --- agent-tooling: a FAIL whose evidence shows the agent's OWN command was
# malformed is not a trustworthy product result. ONLY status=FAIL is flipped. ---

@test "classify: FAIL + agent-tooling marker -> agent-tooling (retriable)" {
    [ "$(gui_classify_failure FAIL 1 0 1)" = agent-tooling ]
    run gui_classifier_retriable agent-tooling
    [ "$status" -eq 0 ]
}

@test "classify: FAIL + NO tooling marker -> product-fail (unchanged default)" {
    [ "$(gui_classify_failure FAIL 1 0 0)" = product-fail ]
    # The 4th arg defaults to 0, so the historic 3-arg call is unaffected.
    [ "$(gui_classify_failure FAIL 1 0)" = product-fail ]
}

@test "classify: ERROR + tooling marker stays product-error (only FAIL is flipped)" {
    [ "$(gui_classify_failure ERROR 1 0 1)" = product-error ]
}

@test "classify: UNKNOWN:124 + tooling marker but no transport marker is NOT agent-tooling" {
    # A timeout with no connectivity loss is agent-timeout regardless of any
    # tooling string; the tooling flip applies only to an explicit status=FAIL.
    [ "$(gui_classify_failure UNKNOWN 124 0 1)" = agent-timeout ]
}

@test "classify: transport marker still wins for UNKNOWN:124 even with tooling marker" {
    [ "$(gui_classify_failure UNKNOWN 124 1 1)" = transport-timeout ]
}

# --- gui_detect_agent_tooling_marker: log scan ---

@test "tooling marker: detects 'bash: -c: option requires an argument'" {
    local log="$BATS_TEST_TMPDIR/t.log"
    printf '[vm-exec] --- stderr ---\nbash: -c: option requires an argument\n' > "$log"
    run gui_detect_agent_tooling_marker "$log"
    [ "$status" -eq 0 ]
}

@test "tooling marker: detects an unterminated quoted string / unexpected EOF" {
    local log="$BATS_TEST_TMPDIR/t2.log"
    echo "bash: -c: line 1: unexpected EOF while looking for matching \`\"'" > "$log"
    run gui_detect_agent_tooling_marker "$log"
    [ "$status" -eq 0 ]
}

@test "tooling marker: an ordinary assertion FAIL log is NOT a tooling marker" {
    # A genuine product FAIL that merely reports a missing event must NOT be
    # mistaken for an agent command-construction error.
    local log="$BATS_TEST_TMPDIR/t3.log"
    printf 'Assert 2.1: FAIL: No request_close event found for handle 13\n' > "$log"
    run gui_detect_agent_tooling_marker "$log"
    [ "$status" -ne 0 ]
}

@test "tooling marker: a bare 'command not found' is NOT a tooling marker (too broad)" {
    local log="$BATS_TEST_TMPDIR/t4.log"
    echo "foo: command not found" > "$log"
    run gui_detect_agent_tooling_marker "$log"
    [ "$status" -ne 0 ]
}

@test "tooling marker: a PROSE mention of a diagnostic does NOT match (anchor)" {
    # The de-biased prompt teaches these exact phrases, so an agent quoting one in
    # its narrative (not a real shell stderr line) must NOT flip a product FAIL.
    local log="$BATS_TEST_TMPDIR/t5.log"
    printf 'I checked for "syntax error near unexpected token" but found none.\n' > "$log"
    run gui_detect_agent_tooling_marker "$log"
    [ "$status" -ne 0 ]
    # And a FAIL note that merely repeats "option requires an argument" in prose.
    printf 'Note: the docs warn that -c with no option requires an argument later.\n' > "$log"
    run gui_detect_agent_tooling_marker "$log"
    [ "$status" -ne 0 ]
}

@test "tooling marker: a real /bin/sh-prefixed dash syntax error matches" {
    local log="$BATS_TEST_TMPDIR/t6.log"
    echo "sh: 1: Syntax error: Unterminated quoted string" > "$log"
    run gui_detect_agent_tooling_marker "$log"
    [ "$status" -eq 0 ]
}

@test "tooling marker: missing log file is not a marker" {
    run gui_detect_agent_tooling_marker "$BATS_TEST_TMPDIR/nope.log"
    [ "$status" -ne 0 ]
}

# --- agent-api-unreachable: an UNKNOWN attempt whose evidence shows the agent CLI
# could not open a socket to the LLM provider AT ALL is pure infra, not a product
# result. rc-independent (the observed outage produced both rc=0 and rc=1), scoped
# to status=UNKNOWN, checked before the no-verdict branch. ---

@test "classify: UNKNOWN + api marker -> agent-api-unreachable (retriable), any rc" {
    [ "$(gui_classify_failure UNKNOWN 1 0 0 1)" = agent-api-unreachable ]
    [ "$(gui_classify_failure UNKNOWN 0 0 0 1)" = agent-api-unreachable ]
    [ "$(gui_classify_failure UNKNOWN 124 0 0 1)" = agent-api-unreachable ]
    run gui_classifier_retriable agent-api-unreachable
    [ "$status" -eq 0 ]
}

@test "classify: api marker takes precedence over no-verdict for UNKNOWN:0" {
    # Without the marker a clean-exit UNKNOWN is no-verdict (never retriable); the
    # provider outage must NOT be miscounted as an agent bug.
    [ "$(gui_classify_failure UNKNOWN 0 0 0 0)" = no-verdict ]
    [ "$(gui_classify_failure UNKNOWN 0 0 0 1)" = agent-api-unreachable ]
}

@test "classify: api marker does NOT flip a product FAIL/ERROR (scope is UNKNOWN)" {
    # A real assertion FAIL/ERROR stays a product result even if a provider blip
    # string is present — the agent ran the asserts.
    [ "$(gui_classify_failure FAIL 1 0 0 1)" = product-fail ]
    [ "$(gui_classify_failure ERROR 1 0 0 1)" = product-error ]
}

@test "classify: 5th arg defaults to 0 (historic 3/4-arg calls unaffected)" {
    [ "$(gui_classify_failure UNKNOWN 1 0)" = unknown ]
    [ "$(gui_classify_failure UNKNOWN 0 0 0)" = no-verdict ]
}

@test "classify: api marker wins even when rc=124 and transport marker is present" {
    # Precedence lock-in: both are retriable infra, but the API marker is more
    # specific to the observed outage. A later reorder must be intentional.
    [ "$(gui_classify_failure UNKNOWN 124 1 0 1)" = agent-api-unreachable ]
}

# --- gui_detect_agent_api_marker: log scan ---

@test "api marker: detects 'Unable to connect to API (FailedToOpenSocket)'" {
    local log="$BATS_TEST_TMPDIR/a1.log"
    printf 'API Error: Unable to connect to API (FailedToOpenSocket)\n' > "$log"
    run gui_detect_agent_api_marker "$log"
    [ "$status" -eq 0 ]
}

@test "api marker: detects 'Unable to connect to API (ConnectionRefused)'" {
    local log="$BATS_TEST_TMPDIR/a2.log"
    printf 'some earlier output\nAPI Error: Unable to connect to API (ConnectionRefused)\n' > "$log"
    run gui_detect_agent_api_marker "$log"
    [ "$status" -eq 0 ]
}

@test "api marker: a PROSE mention of the provider error does NOT match (anchor)" {
    # An agent narrative discussing the failure mode must not flip a verdict.
    local log="$BATS_TEST_TMPDIR/a3.log"
    printf 'The test checks what happens when we get an API Error: Unable to connect to API (ConnectionRefused) response.\n' > "$log"
    run gui_detect_agent_api_marker "$log"
    [ "$status" -ne 0 ]
}

@test "api marker: a generic connection/timeout string does NOT match (too broad)" {
    local log="$BATS_TEST_TMPDIR/a4.log"
    printf 'curl: (7) Failed to connect to example.com port 443: Connection refused\n' > "$log"
    run gui_detect_agent_api_marker "$log"
    [ "$status" -ne 0 ]
    printf 'API Error: Unable to connect to API (Overloaded)\n' > "$log"
    run gui_detect_agent_api_marker "$log"
    [ "$status" -ne 0 ]
}

@test "api marker: suffix text does NOT match the exact CLI line (end anchor)" {
    local log="$BATS_TEST_TMPDIR/a5.log"
    printf 'API Error: Unable to connect to API (ConnectionRefused): retrying\n' > "$log"
    run gui_detect_agent_api_marker "$log"
    [ "$status" -ne 0 ]
}

@test "api marker: leading/trailing whitespace around the exact CLI line is accepted" {
    # The [[:space:]]* allowance is intentional (log indentation / trailing CR).
    local log="$BATS_TEST_TMPDIR/a6.log"
    printf '  API Error: Unable to connect to API (FailedToOpenSocket)  \n' > "$log"
    run gui_detect_agent_api_marker "$log"
    [ "$status" -eq 0 ]
}

@test "api marker: missing log file is not a marker" {
    run gui_detect_agent_api_marker "$BATS_TEST_TMPDIR/nope.log"
    [ "$status" -ne 0 ]
}

# --- gui_retry_max: QCI_GUI_RETRY knob -> max additional attempts ---

@test "retry_max: disabled values map to 0 (report-only)" {
    for v in "" 0 off false no bogus; do
        [ "$(gui_retry_max "$v")" = 0 ]
    done
}

@test "retry_max: legacy boolean truthy values map to 1" {
    for v in on classified true yes 1; do
        [ "$(gui_retry_max "$v")" = 1 ]
    done
}

@test "retry_max: a bare integer passes through" {
    [ "$(gui_retry_max 2)" = 2 ]
    [ "$(gui_retry_max 3)" = 3 ]
}

@test "retry_max: an oversized integer is capped at GUI_RETRY_CAP" {
    GUI_RETRY_CAP=5
    [ "$(gui_retry_max 99)" = 5 ]
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
