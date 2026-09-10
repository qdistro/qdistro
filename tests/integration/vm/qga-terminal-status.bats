#!/usr/bin/env bats
#
# REAL-QGA contract test, and the in-VM half of the scenario-59 fix.
#
# tests/integration/qci/vm-exec-signal.bats drives vm-exec's control flow with a
# FAKE virsh. A fake cannot prove what the guest agent actually does -- and the
# whole scenario-59 investigation turned on exactly that behaviour: qga reports a
# signalled command as {"exited":true,"signal":N} and REAPS the process metadata
# in the SAME response, so any later poll returns "PID <n> does not exist".
# vm-exec used to read only .return.exitcode, see null, and poll that reaped PID
# 30 times before blaming the guest agent. See todo/reviews/out-59-qga.md.
#
# Everything here is asserted against the real agent, so a qga upgrade that
# changes the contract fails HERE rather than resurfacing as a mystery flake in
# a GUI scenario months later.

load helpers

setup() {
    # These assertions are about the qga transport specifically. When the suite
    # is routed over ssh, vm-exec's guest-exec path is not exercised at all and
    # the contract under test does not apply.
    if [ -n "${VM_SSH_PORT:-}" ]; then
        skip "ssh transport in use; qga guest-exec contract not exercised"
    fi
    require jq
}

qga() {   # qga <json> -- raw agent RPC, bypassing vm-exec
    virsh -c qemu:///session qemu-agent-command "$VM_NAME" "$1"
}

qga_start() {   # qga_start <sh-command> -> pid
    qga "$(jq -nc --arg c "$1" \
        '{execute:"guest-exec",arguments:{path:"/bin/sh",arg:["-c",$c],"capture-output":true}}')" \
        | jq -r '.return.pid'
}

qga_status() {  # qga_status <pid> -> raw response (may be an error object)
    qga "$(jq -nc --argjson p "$1" \
        '{execute:"guest-exec-status",arguments:{pid:$p}}')" 2>&1 || true
}

qga_wait_terminal() {   # qga_wait_terminal <pid> -> the terminal response
    local pid=$1 i resp
    for i in $(seq 1 60); do
        resp=$(qga_status "$pid")
        if [ "$(jq -r '.return.exited // false' <<<"$resp" 2>/dev/null)" = "true" ]; then
            printf '%s' "$resp"; return 0
        fi
        sleep 1
    done
    fail_loud "guest-exec pid $pid never reached terminal status"
}

@test "qga: a normal exit reports exitcode and NO signal" {
    local pid resp
    pid=$(qga_start 'exit 3')
    resp=$(qga_wait_terminal "$pid")
    [ "$(jq -r '.return.exitcode' <<<"$resp")" = "3" ]
    [ "$(jq -r '.return|has("signal")' <<<"$resp")" = "false" ]
}

@test "qga: a SIGNALLED command reports signal and NO exitcode" {
    # The response shape vm-exec used to ignore, which made it poll forever.
    local pid resp
    pid=$(qga_start 'kill -TERM $$; sleep 30')
    resp=$(qga_wait_terminal "$pid")
    [ "$(jq -r '.return.signal' <<<"$resp")" = "15" ]
    [ "$(jq -r '.return|has("exitcode")' <<<"$resp")" = "false" ]
}

@test "qga: terminal status is ONE-SHOT -- the next poll finds the PID reaped" {
    # The heart of the six-run scenario-59 mystery. Everything in the fix rests
    # on this being true of the real agent, so assert it against the real agent.
    local pid resp again
    pid=$(qga_start 'kill -TERM $$; sleep 30')
    resp=$(qga_wait_terminal "$pid")
    [ "$(jq -r '.return.signal' <<<"$resp")" = "15" ]
    again=$(qga_status "$pid")
    [[ "$again" == *"does not exist"* ]]
}

@test "vm-exec: a self-matching pkill -f reports SIGTERM, not a retry storm" {
    # vm-exec runs everything as /bin/sh -c '<the whole command>', so the
    # pattern is in the shell's OWN argv and pkill -f kills the shell running
    # it. vm-exec must now surface that immediately as 128+15.
    run "$VM_EXEC" "$VM_NAME" 'pkill -f qga-selfmatch-probe-token; true'
    [ "$status" -eq 143 ]
    [[ "$output" == *"terminated by signal 15"* ]]
}

@test "vm-exec: the bracketed pattern does NOT self-match and succeeds" {
    run "$VM_EXEC" "$VM_NAME" 'pkill -f "[q]ga-selfmatch-probe-token" 2>/dev/null; true'
    [ "$status" -eq 0 ]
    [[ "$output" != *"terminated by signal"* ]]
}

@test "vm-exec: /proc starttime identity round-trips INSIDE the guest" {
    # scenario 59 keys its teardown on the (pid, starttime) pair. That was
    # verified on the host; this pins it in the guest, where it actually runs,
    # including the sed/cut field arithmetic after stripping through comm.
    run "$VM_EXEC" "$VM_NAME" '
        setsid sleep 30 >/dev/null 2>&1 & p=$!
        a=$(sed "s/.*) //" /proc/$p/stat | cut -d" " -f20)
        b=$(sed "s/.*) //" /proc/$p/stat | cut -d" " -f20)
        kill "$p" 2>/dev/null
        case "$a" in "" | *[!0-9]* ) echo "BAD-STARTTIME[$a]"; exit 1 ;; esac
        [ "$a" = "$b" ] || { echo "UNSTABLE[$a!=$b]"; exit 1; }
        echo "IDENTITY-OK starttime=$a"
    '
    [ "$status" -eq 0 ]
    [[ "$output" == *"IDENTITY-OK"* ]]
}
