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

@test "await_system_unit_active: passes when systemctl reports active" {
    systemctl() { [ "$1" = is-active ] && echo active; }
    export -f systemctl
    run await_system_unit_active some.service 2 1
    [ "$status" -eq 0 ]
}

@test "await_system_unit_active: TIMES OUT loudly on a non-active state" {
    systemctl() { [ "$1" = is-active ] && echo activating; }
    export -f systemctl
    run await_system_unit_active some.service 1 1
    [ "$status" -ne 0 ]
    [[ "$output" == *"TIMEOUT"* ]]
    [[ "$output" == *"system unit active: some.service"* ]]
    [[ "$output" == *"state=activating"* ]]
}

@test "await_system_unit_active: succeeds once the unit flips to active mid-wait" {
    local n="$BATS_TEST_TMPDIR/svc"; echo 0 > "$n"
    systemctl() {
        [ "$1" = is-active ] || return 0
        local c; c=$(cat "$n"); c=$((c + 1)); echo "$c" > "$n"
        if [ "$c" -ge 2 ]; then echo active; else echo activating; fi
    }
    export -f systemctl
    run await_system_unit_active some.service 5 1
    [ "$status" -eq 0 ]
}

@test "await_broker_pending_action: requires the exact pending action" {
    dbus-send() {
        printf 'string "app.send-to:3000:org.qdistro.Qnotebook.uid3000"\n'
    }
    export -f dbus-send
    run await_broker_pending_action \
        app.send-to:3000:org.qdistro.Qnotebook.uid3000 2 1
    [ "$status" -eq 0 ]

    run await_broker_pending_action \
        app.send-to:2000:org.qdistro.Qnotebook.uid2000 1 1
    [ "$status" -ne 0 ]
    [[ "$output" == *"TIMEOUT"* ]]
    [[ "$output" == *"app.send-to:3000:org.qdistro.Qnotebook.uid3000"* ]]
}

@test "await_broker_pending_action: rejects an empty action" {
    run await_broker_pending_action "" 1 1
    [ "$status" -eq 2 ]
    [[ "$output" == *"must be non-empty"* ]]
}

@test "await_domain_gone: passes when the domain is absent (virsh errors)" {
    virsh() { return 1; }   # domstate on an undefined domain errors, empty stdout
    export -f virsh
    run await_domain_gone gone-dom 2 1
    [ "$status" -eq 0 ]
}

@test "await_domain_gone: passes on a terminal non-running state" {
    virsh() { [ "$1" = domstate ] && echo "shut off"; }
    export -f virsh
    run await_domain_gone dom 2 1
    [ "$status" -eq 0 ]
}

@test "await_domain_gone: accepts a crashed domain as reaped (terminal)" {
    virsh() { [ "$1" = domstate ] && echo crashed; }
    export -f virsh
    run await_domain_gone dom 2 1
    [ "$status" -eq 0 ]
}

@test "await_domain_gone: TIMES OUT loudly while the domain is still running" {
    virsh() { [ "$1" = domstate ] && echo running; }
    export -f virsh
    run await_domain_gone dom 1 1
    [ "$status" -ne 0 ]
    [[ "$output" == *"TIMEOUT"* ]]
    [[ "$output" == *"domain reaped (absent or terminally stopped): dom"* ]]
    [[ "$output" == *"domstate=running"* ]]
}

@test "await_domain_gone: does NOT accept a live/transitional state (paused) as reaped" {
    # paused/pmsuspended/blocked are non-running but the domain still exists —
    # they must NOT satisfy the reap check.
    virsh() { [ "$1" = domstate ] && echo paused; }
    export -f virsh
    run await_domain_gone dom 1 1
    [ "$status" -ne 0 ]
    [[ "$output" == *"TIMEOUT"* ]]
    [[ "$output" == *"domstate=paused"* ]]
}

@test "await_domain_gone: succeeds once the domain stops running mid-wait" {
    local n="$BATS_TEST_TMPDIR/dom"; echo 0 > "$n"
    virsh() {
        [ "$1" = domstate ] || return 0
        local c; c=$(cat "$n"); c=$((c + 1)); echo "$c" > "$n"
        if [ "$c" -ge 2 ]; then echo "shut off"; else echo running; fi
    }
    export -f virsh
    run await_domain_gone dom 5 1
    [ "$status" -eq 0 ]
}
