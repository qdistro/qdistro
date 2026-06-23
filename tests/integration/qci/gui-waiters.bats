#!/usr/bin/env bats
#
# Host-only tests for the guest-side waiter library core
# (ci/lib/guest/gui-waiters.sh). Exercises the bounded-poll engine and the
# generic file/socket waiters against host state — the systemctl/journal/virsh
# wrappers are thin probes over the same _await core tested here. Asserts the
# masking-critical contract: a waiter returns 0 the instant the condition holds,
# and on TIMEOUT fails LOUD with the last observed state + elapsed seconds.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/guest/gui-waiters.sh"
}

@test "await_file: returns 0 immediately when the file already exists" {
    local f="$BATS_TEST_TMPDIR/here"
    : > "$f"
    run await_file "$f" 2 1
    [ "$status" -eq 0 ]
}

@test "await_file: succeeds when the file appears during the wait" {
    local f="$BATS_TEST_TMPDIR/later"
    ( sleep 1; : > "$f" ) &
    run await_file "$f" 5 1
    [ "$status" -eq 0 ]
    wait
}

@test "await_file: TIMES OUT loudly when the file never appears" {
    run await_file "$BATS_TEST_TMPDIR/never" 1 1
    [ "$status" -ne 0 ]
    [[ "$output" == *"TIMEOUT"* ]]
    [[ "$output" == *"file to exist"* ]]
    [[ "$output" == *"never"* ]]
}

@test "await_socket: distinguishes a socket from a plain file" {
    local plain="$BATS_TEST_TMPDIR/plain"
    : > "$plain"
    # A regular file is NOT a socket -> must time out, not pass.
    run await_socket "$plain" 1 1
    [ "$status" -ne 0 ]
    [[ "$output" == *"TIMEOUT"* ]]
}

@test "_await: reports the probe's last observed state on timeout" {
    # A probe that always fails but echoes a diagnostic line.
    probe() { echo "state=degraded"; return 1; }
    run _await "the thing to be ready" 1 1 probe
    [ "$status" -ne 0 ]
    [[ "$output" == *"last observed: state=degraded"* ]]
}

@test "_await: returns 0 as soon as the probe succeeds (no full wait)" {
    local n="$BATS_TEST_TMPDIR/n"; echo 0 > "$n"
    # Succeeds on the 2nd probe call.
    probe() {
        local c; c=$(cat "$n"); c=$((c + 1)); echo "$c" > "$n"
        [ "$c" -ge 2 ]
    }
    run _await "second try" 10 1 probe
    [ "$status" -eq 0 ]
}
