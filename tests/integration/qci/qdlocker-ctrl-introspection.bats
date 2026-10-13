#!/usr/bin/env bats
#
# Host-only test of qdlocker_ctrl and its waiters
# (qdlocker/tests/gui/qdlocker-helpers.sh) when ctrl introspection is off.
#
# The defect it pins (presentation-gui/04, full-20261012T155315Z-2626707): a
# driver ran qdwin_session_healthy instead of qdlocker_session_healthy, so the
# introspection marker was never installed. Every `status` answered
# `error: command unavailable`, the waiters burned their timeouts and the run
# was graded FAIL ("1.1 lock wait") although the oracle never existed.
# qdlocker_ctrl must flag that reply as a harness ERROR (rc 3) and the waiters
# must stop at once with rc 3 instead of timing out with rc 1.
#
# The fake vm-exec answers with whatever $T/reply holds.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    T="$BATS_TEST_TMPDIR"
    mkdir -p "$T/bin"
    cat > "$T/bin/fake-vm-exec" <<EOF
#!/bin/bash
cat "$T/reply"
EOF
    chmod +x "$T/bin/fake-vm-exec"
    export QDWIN_REPO="$REPO_ROOT/qdwin"
    export QDWIN_VM_EXEC="$T/bin/fake-vm-exec"
    # shellcheck disable=SC1091
    source "$REPO_ROOT/qdlocker/tests/gui/qdlocker-helpers.sh"
    QDWIN_VM_EXEC="$T/bin/fake-vm-exec"
    qdwin_set_vm fake-vm
}

@test "qdlocker_ctrl passes a real status reply through with rc 0" {
    echo 'locked=True prompt-len=0 pam-ready=True' > "$T/reply"
    run qdlocker_ctrl status
    [ "$status" -eq 0 ]
    [ "$output" = 'locked=True prompt-len=0 pam-ready=True' ]
}

@test "qdlocker_ctrl flags disabled introspection as a harness ERROR (rc 3)" {
    echo 'error: command unavailable' > "$T/reply"
    run qdlocker_ctrl status
    [ "$status" -eq 3 ]
    [[ "$output" == *"error: command unavailable"* ]]
    [[ "$output" == *"ERROR(harness)"*"qdlocker_session_healthy"* ]]
}

@test "the lock/unlock waiters stop at once with rc 3, not a timeout" {
    echo 'error: command unavailable' > "$T/reply"
    SECONDS=0
    run qdlocker_wait_for_lock 5
    [ "$status" -eq 3 ]
    [[ "$output" == *"ERROR(harness)"* ]]
    [[ "$output" != *timeout* ]]
    run qdlocker_wait_for_unlock 5
    [ "$status" -eq 3 ]
    [ "$SECONDS" -lt 4 ]
}

@test "the waiters still succeed and still time out on real replies" {
    echo 'locked=True prompt-len=0 pam-ready=True' > "$T/reply"
    run qdlocker_wait_for_lock 2
    [ "$status" -eq 0 ]
    echo 'locked=False prompt-len=0 pam-ready=True' > "$T/reply"
    run qdlocker_wait_for_lock 1
    [ "$status" -eq 1 ]
    [[ "$output" == *"timeout after 1s"* ]]
}
