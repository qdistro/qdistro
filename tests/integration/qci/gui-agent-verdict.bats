#!/usr/bin/env bats
#
# Host-only unit tests for the GUI gate's fail-closed agent status/rc -> verdict
# mapping (ci/lib/gates/gui.sh::gui_agent_verdict). NO VM is booted: the function
# is pure (reads only its two arguments), so the pass/fail/skip decision — the
# single most masking-sensitive line in the GUI gate — is host-testable.
#
# Contract under test (FAIL CLOSED):
#   - Only an explicit PASS with rc=0 is a pass.
#   - Any SKIP (any rc) is a skip.
#   - FAIL/ERROR (any rc) is a fail.
#   - UNKNOWN (agent exited without a parseable verdict) is a fail — at EVERY rc,
#     including rc=0, which used to be a silent pass (the fail-open hole this
#     closes).
#   - PASS with a NONZERO rc is a fail (claimed pass but the runner returned
#     nonzero — a contradiction, kept red).
#   - Any malformed/empty status is a fail.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    # gui.sh is a sourced module (function definitions only, no top-level
    # execution); pull it in so the pure helper is callable on the host.
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/gates/gui.sh"
}

# Echo just the verdict field (before the TAB) for a given status:rc.
verdict() {
    gui_agent_verdict "$1" "$2" | cut -f1
}

artifact_status() {
    local d="$BATS_TMPDIR/status-artifact"
    rm -rf "$d"
    mkdir -p "$d"
    printf '%s' "$1" > "$d/status.txt"
    agent_artifact_status "$d" "$d/agent.log"
}

@test "verdict: PASS:0 -> pass" {
    [ "$(verdict PASS 0)" = pass ]
}

@test "verdict: PASS:1 -> fail (claimed pass, nonzero rc)" {
    [ "$(verdict PASS 1)" = fail ]
}

@test "verdict: PASS:124 -> fail (claimed pass, timed out)" {
    [ "$(verdict PASS 124)" = fail ]
}

@test "verdict: SKIP:0 -> skip" {
    [ "$(verdict SKIP 0)" = skip ]
}

@test "verdict: SKIP:1 -> skip (skip regardless of rc)" {
    [ "$(verdict SKIP 1)" = skip ]
}

@test "verdict: FAIL:0 -> fail" {
    [ "$(verdict FAIL 0)" = fail ]
}

@test "verdict: FAIL:1 -> fail" {
    [ "$(verdict FAIL 1)" = fail ]
}

@test "verdict: ERROR:0 -> fail" {
    [ "$(verdict ERROR 0)" = fail ]
}

@test "verdict: UNKNOWN:0 -> fail (the fail-open hole, now closed)" {
    [ "$(verdict UNKNOWN 0)" = fail ]
}

@test "verdict: UNKNOWN:124 -> fail (agent timed out, no status)" {
    [ "$(verdict UNKNOWN 124)" = fail ]
}

@test "verdict: empty status:0 -> fail (malformed)" {
    [ "$(verdict '' 0)" = fail ]
}

@test "verdict: garbage status -> fail" {
    [ "$(verdict WAT 0)" = fail ]
}

@test "artifact status: literal-n typo is normalized" {
    [ "$(artifact_status PASSn)" = PASS ]
    [ "$(artifact_status FAILn)" = FAIL ]
}

@test "artifact status: unrelated PASS prefix still fails closed" {
    [ "$(artifact_status PASSING)" = UNKNOWN ]
}

@test "verdict: UNKNOWN:0 note records status and rc for triage" {
    run gui_agent_verdict UNKNOWN 0
    [ "$status" -eq 0 ]
    [[ "$output" == *"status=UNKNOWN"* ]]
    [[ "$output" == *"rc=0"* ]]
    [[ "$output" == *"fail closed"* ]]
}

@test "verdict: FAIL note records status and rc" {
    run gui_agent_verdict FAIL 1
    [[ "$output" == *"status=FAIL"* ]]
    [[ "$output" == *"rc=1"* ]]
}
