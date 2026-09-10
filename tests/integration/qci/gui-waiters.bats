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

@test "await_x11_window: passes when a visible window id is returned" {
    runuser() { echo 73400327; }
    export -f runuser
    run await_x11_window "admin approvals" admin :0 2 1
    [ "$status" -eq 0 ]
}

@test "await_x11_window: TIMES OUT loudly when no window maps" {
    runuser() { echo "xdotool: no matching window"; return 1; }
    export -f runuser
    run await_x11_window "admin approvals" admin :0 1 1
    [ "$status" -ne 0 ]
    [[ "$output" == *"TIMEOUT"* ]]
    [[ "$output" == *"visible X11 window matching: admin approvals"* ]]
    [[ "$output" == *"xdotool: no matching window"* ]]
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

# --- success must be OBSERVABLE -------------------------------------------
# Regression for permissions-gui/59 S2 (run full-20260909T224527Z): the broker
# HAD logged `lineage_enforce=True` 1s after the restart, but the waiter
# returned 0 in silence, the step was graded by grepping the command's stdout,
# and an empty stdout was read as a product failure. A passing gate must say so.

@test "_await: announces SUCCESS on stdout with the probe's observation" {
    probe() { echo "matched: [broker] lineage_enforce=True"; return 0; }
    run _await "the posture line" 2 1 probe
    [ "$status" -eq 0 ]
    [[ "$output" == *"[await] OK"* ]]
    [[ "$output" == *"the posture line"* ]]
    [[ "$output" == *"matched: [broker] lineage_enforce=True"* ]]
}

@test "_await: success line goes to STDOUT (not stderr)" {
    probe() { echo "ready=yes"; return 0; }
    local out err
    out=$(_await "the thing" 2 1 probe 2>"$BATS_TEST_TMPDIR/err")
    err=$(cat "$BATS_TEST_TMPDIR/err")
    [[ "$out" == *"[await] OK"* ]]
    [[ "$out" == *"ready=yes"* ]]
    [ -z "$err" ]
}

@test "await_journal_line_after_cursor: prints the matched journal line on success" {
    journalctl() { echo "[broker] lineage_enforce=True (False=shadow/audit-only)"; }
    export -f journalctl
    run await_journal_line_after_cursor "s=cur;i=1" "lineage_enforce=True" 2 1 \
        -u qdistro-admin-broker.service
    [ "$status" -eq 0 ]
    [[ "$output" == *"lineage_enforce=True"* ]]
}

@test "await_journal_line_after_cursor: a shadow-mode line does NOT satisfy the enforce pattern" {
    journalctl() { echo "[broker] lineage_enforce=False (False=shadow/audit-only)"; }
    export -f journalctl
    run await_journal_line_after_cursor "s=cur;i=1" "lineage_enforce=True" 1 1 \
        -u qdistro-admin-broker.service
    [ "$status" -ne 0 ]
    [[ "$output" == *"TIMEOUT"* ]]
}

@test "_await: QCI_AWAIT_QUIET=1 suppresses the success announcement only" {
    probe() { echo "ready=yes"; return 0; }
    QCI_AWAIT_QUIET=1 run _await "the thing" 2 1 probe
    [ "$status" -eq 0 ]
    [ -z "$output" ]
    # A TIMEOUT is still loud even under quiet.
    probe() { echo "state=degraded"; return 1; }
    QCI_AWAIT_QUIET=1 run _await "the thing" 1 1 probe
    [ "$status" -ne 0 ]
    [[ "$output" == *"TIMEOUT"* ]]
    [[ "$output" == *"state=degraded"* ]]
}

@test "_await: a chatty probe's observation is capped with an ANNOUNCED truncation" {
    probe() { seq 1 50; return 0; }
    QCI_AWAIT_OBSERVED_MAX_LINES=5 run _await "the chatty thing" 2 1 probe
    [ "$status" -eq 0 ]
    [[ "$output" == *"[await] OK"* ]]
    [[ "$output" == *"truncated, 45 more line(s)"* ]]
    [[ "$output" != *"50"* ]]
}

# --- Regressions for the observation-cap defects found in codex sol review ---
# (todo/reviews/out-59-qga.md). The pre-existing cap test above uses 50 SHORT
# lines, which fit in the pipe buffer, so it could not catch either bug.

@test "_await: a large success under set -euo pipefail does NOT become a failure" {
    # The original cap used `printf | head`, which closes the read end early.
    # printf then takes SIGPIPE (141) and, under pipefail, that 141 propagated
    # out of a SUCCESSFUL wait. Output must exceed the pipe buffer (64 KiB) for
    # the race to bite, hence 200k lines rather than 50.
    run bash -c '
        set -euo pipefail
        . '"$REPO_ROOT/ci/lib/guest/gui-waiters.sh"'
        QCI_AWAIT_OBSERVED_MAX_LINES=5
        big() { seq 1 200000; }
        _await "huge" 5 1 big
        echo "SURVIVED rc=$?"
    '
    [ "$status" -eq 0 ]
    [[ "$output" == *"SURVIVED rc=0"* ]]
    [[ "$output" == *"[await] OK after"* ]]
    [[ "$output" == *"truncated, 199995 more line(s)"* ]]
}

@test "_await: a single oversized line is byte-capped, not emitted whole" {
    # The line cap alone is defeated by one long line (e.g. a whole dbus reply),
    # which is the case that can reach qga's per-stream capture cap.
    run bash -c '
        set -euo pipefail
        . '"$REPO_ROOT/ci/lib/guest/gui-waiters.sh"'
        QCI_AWAIT_OBSERVED_MAX_BYTES=200
        oneline() { printf "x%.0s" $(seq 1 200000); echo; }
        _await "oneline" 5 1 oneline
    '
    [ "$status" -eq 0 ]
    [[ "$output" == *"more byte(s)"* ]]
    # Whole-line emission was 200048 bytes; the cap must hold it far below that.
    [ "${#output}" -lt 1000 ]
}

@test "_await: a malformed cap falls back instead of aborting a passing wait" {
    run bash -c '
        set -euo pipefail
        . '"$REPO_ROOT/ci/lib/guest/gui-waiters.sh"'
        QCI_AWAIT_OBSERVED_MAX_LINES=notanumber
        QCI_AWAIT_OBSERVED_MAX_BYTES=-5
        _await "smallprobe" 5 1 echo hello
    '
    [ "$status" -eq 0 ]
    [[ "$output" == *"[await] observed: hello"* ]]
}
