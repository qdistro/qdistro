#!/usr/bin/env bats
#
# Host-only regressions for scripts/vm/vm-exec's terminal-status handling.
#
# qga reports a signal-terminated command as {"exited":true,"signal":N} with NO
# `exitcode` field, and REAPS the process metadata in that same response.
# vm-exec used to read only `.return.exitcode`, see null, and keep polling the
# already-reaped PID until its retry budget ran out -- surfacing as
# `PID <n> does not exist` and a bogus "guest agent not responding". That made
# permissions-gui/59 look like a flaky guest agent for six runs.
# See todo/reviews/out-59-qga.md.
#
# No VM required: agent_rpc shells out to `virsh`, so a fake virsh on PATH
# drives vm-exec through each terminal shape.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    VM_EXEC="$REPO_ROOT/scripts/vm/vm-exec"
    FAKEBIN="$BATS_TEST_TMPDIR/bin"
    mkdir -p "$FAKEBIN"
    STATE="$BATS_TEST_TMPDIR/calls"
    : > "$STATE"
}

# make_virsh <terminal-json> [nonterminal-count] — a fake virsh answering
# guest-exec with pid 4242, then <nonterminal-count> (default 1)
# {"exited":false} status responses, then the terminal response, then the
# reaped-PID error for any FURTHER call. That last part matters: terminal
# status is one-shot in qga, so a correct vm-exec must never poll again, and
# an incorrect one is caught here rather than passing on a forgiving stub.
make_virsh() {
    local terminal=$1 nonterm=${2:-1}
    # Reset the call ledger: a test that reconfigures the fake (e.g. loops over
    # several bodies) must not resume a previous body's call count, or it lands
    # in the post-terminal reaped branch instead of the shape under test.
    : > "$STATE"
    printf '%s' "$terminal" > "$BATS_TEST_TMPDIR/terminal.json"
    printf '%s' "$nonterm" > "$BATS_TEST_TMPDIR/nonterm"
    cat > "$FAKEBIN/virsh" <<EOF
#!/usr/bin/env bash
for a in "\$@"; do
    case "\$a" in
        *guest-exec-status*)
            echo "status" >> "$STATE"
            n=\$(wc -l < "$STATE")
            nonterm=\$(cat "$BATS_TEST_TMPDIR/nonterm")
            if [ "\$n" -le "\$nonterm" ]; then
                echo '{"return":{"exited":false}}'
            elif [ "\$n" -eq \$((nonterm + 1)) ]; then
                cat "$BATS_TEST_TMPDIR/terminal.json"
            elif grep -q '"error"' "$BATS_TEST_TMPDIR/terminal.json"; then
                # An ERROR response is not one-shot -- qga will keep returning it.
                # Only a successful terminal status reaps the bookkeeping.
                cat "$BATS_TEST_TMPDIR/terminal.json"
            else
                # One-shot: metadata already reaped by the terminal response.
                echo '{"error":{"class":"GenericError","desc":"PID ld does not exist"}}'
            fi
            exit 0
            ;;
        *'"guest-exec"'*)
            echo '{"return":{"pid":4242}}'
            exit 0
            ;;
    esac
done
echo '{"return":{}}'
EOF
    chmod +x "$FAKEBIN/virsh"
}

status_calls() { wc -l < "$STATE"; }

@test "vm-exec: a signal-terminated command exits 128+N instead of polling a reaped PID" {
    make_virsh '{"return":{"exited":true,"signal":15}}'
    PATH="$FAKEBIN:$PATH" run timeout 60 "$VM_EXEC" fake-vm 'pkill -f whatever'
    [ "$status" -eq 143 ]                       # 128 + SIGTERM
    [[ "$output" == *"terminated by signal 15"* ]]
    # One nonterminal poll then the terminal one. A third call would mean it
    # kept polling past terminal status and hit the reaped-PID error -- the
    # original 30-retry loop.
    [ "$(status_calls)" -eq 2 ]
}

@test "vm-exec: a normal exitcode still wins and is returned unchanged" {
    make_virsh '{"return":{"exited":true,"exitcode":3}}'
    PATH="$FAKEBIN:$PATH" run timeout 60 "$VM_EXEC" fake-vm 'exit 3'
    [ "$status" -eq 3 ]
    [[ "$output" != *"terminated by signal"* ]]
}

@test "vm-exec: exitcode 0 succeeds and reports no signal" {
    make_virsh '{"return":{"exited":true,"exitcode":0}}'
    PATH="$FAKEBIN:$PATH" run timeout 60 "$VM_EXEC" fake-vm true
    [ "$status" -eq 0 ]
    [[ "$output" != *"terminated by signal"* ]]
}

@test "vm-exec: a GENERIC semantic error still retries, and is described honestly" {
    # Not every qga error is terminal. A generic error is worth retrying -- but
    # it must not be reported as "not responding": an error RESPONSE proves qga
    # is alive and serving RPCs. (The reaped-PID error is terminal instead; see
    # the dedicated test below.)
    make_virsh '{"error":{"class":"GenericError","desc":"something transient"}}' 0
    PATH="$FAKEBIN:$PATH" QDISTRO_VM_AGENT_MAX_ERRORS=2 run timeout 120 "$VM_EXEC" fake-vm 'true'
    [ "$status" -ne 0 ]
    [[ "$output" == *"semantic error"* ]]
    [[ "$output" != *"not responding"* ]]
    # It retried rather than failing on the first error.
    [ "$(status_calls)" -ge 2 ]
}

# --- Round-2 review additions (todo/reviews/out-round2.md) ---

@test "vm-exec: partial stdout/stderr of a killed command is still decoded" {
    # A killed command's partial output is often the most useful evidence, and
    # out-data "errorAAA" is valid base64 that the old `grep -q "error"` check
    # misread as a QMP error -- which made the signal branch unreachable.
    # decodes to "j\xab\x00" plus "partial-stderr".
    make_virsh '{"return":{"exited":true,"signal":9,"out-data":"errorAAA","err-data":"cGFydGlhbC1zdGRlcnI="}}'
    PATH="$FAKEBIN:$PATH" run timeout 60 "$VM_EXEC" fake-vm 'kill -9 $$'
    [ "$status" -eq 137 ]                       # 128 + SIGKILL
    [[ "$output" == *"partial-stderr"* ]]
    [[ "$output" == *"terminated by signal 9"* ]]
    [ "$(status_calls)" -eq 2 ]
}

@test "vm-exec: an inherited TERMSIG cannot fabricate a signal death" {
    # TERMSIG is poll state. An exported TERMSIG in the caller's environment
    # used to survive into the exit reporting and claim a signal death for a
    # command that exited normally on its first status call.
    make_virsh '{"return":{"exited":true,"exitcode":0}}' 0
    PATH="$FAKEBIN:$PATH" TERMSIG=15 run timeout 60 "$VM_EXEC" fake-vm true
    [ "$status" -eq 0 ]
    [[ "$output" != *"terminated by signal"* ]]
}

@test "vm-exec: an out-of-range signal is a protocol error, never success" {
    # signal 128 would compute exit 256, which the caller observes as 0 --
    # a killed command reported as a clean pass.
    make_virsh '{"return":{"exited":true,"signal":128}}'
    PATH="$FAKEBIN:$PATH" run timeout 60 "$VM_EXEC" fake-vm 'whatever'
    [ "$status" -ne 0 ]
    [[ "$output" == *"unsupported signal value"* ]]
}

@test "vm-exec: a reaped-PID error fails immediately instead of retrying" {
    # Terminal status is one-shot, so retrying a reaped PID can never recover;
    # it only burns the 30-retry budget and then blames the guest agent.
    make_virsh '{"error":{"class":"GenericError","desc":"PID ld does not exist"}}' 0
    PATH="$FAKEBIN:$PATH" run timeout 60 "$VM_EXEC" fake-vm 'pkill -f whatever'
    [ "$status" -ne 0 ]
    [[ "$output" == *"reaped"* ]]
    [[ "$output" != *"not responding"* ]]
    [[ "$output" == *"[p]attern"* ]]
    # Immediately: exactly one status call, no retry storm.
    [ "$(status_calls)" -eq 1 ]
}

# --- Round-3 review additions (todo/reviews/out-round3.md) ---
# Every response body must be validated BEFORE any field is read.

@test "vm-exec: an EMPTY status response never returns success" {
    # The worst shape: jq accepts empty input with status 0 and emits nothing,
    # so EXITCODE became "", "" != "null" broke the poll loop, and the script
    # fell off the end returning 0 -- a command whose status was never
    # established reported as a clean PASS.
    make_virsh '' 0
    PATH="$FAKEBIN:$PATH" run timeout 60 "$VM_EXEC" fake-vm 'true'
    [ "$status" -eq 76 ]
    [[ "$output" == *"malformed or empty"* ]]
}

@test "vm-exec: a malformed non-JSON status response is a protocol error" {
    make_virsh 'this is not json at all' 0
    PATH="$FAKEBIN:$PATH" run timeout 60 "$VM_EXEC" fake-vm 'true'
    [ "$status" -eq 76 ]
    [[ "$output" == *"malformed or empty"* ]]
}

@test "vm-exec: valid JSON of the WRONG shape is rejected, not polled to timeout" {
    # {} and null are valid JSON but carry no return/error member. These used to
    # yield null fields and poll until the overall command timeout.
    local body
    for body in '{}' 'null' '[]' '"a string"' '{"return":42}' '{"error":"oops"}'; do
        make_virsh "$body" 0
        PATH="$FAKEBIN:$PATH" run timeout 60 "$VM_EXEC" fake-vm 'true'
        [ "$status" -eq 76 ]
        [[ "$output" == *"malformed or empty"* ]]
    done
}

@test "vm-exec: a non-numeric exitcode or signal is a protocol error" {
    # The QAPI fields are typed int; a string "08" previously reached the
    # shell's octal-sensitive arithmetic ("010" scored 136 instead of 138).
    local body
    for body in '{"return":{"exited":true,"signal":"010"}}' \
                '{"return":{"exited":true,"signal":"08"}}' \
                '{"return":{"exited":true,"exitcode":"0"}}'; do
        make_virsh "$body"
        PATH="$FAKEBIN:$PATH" run timeout 60 "$VM_EXEC" fake-vm 'true'
        [ "$status" -eq 76 ]
        [[ "$output" == *"malformed or empty"* ]]
    done
}

# --- Round-4 review addition (todo/reviews/out-round4.md) ---

@test "vm-exec: an integral-but-float exitcode is parsed, never silently 0" {
    # jq 1.8 types 0.0/3.0 as "number" AND calls them integral, but renders them
    # "0.0"/"3.0". The shell cannot compare those numerically; the failed
    # comparison sits in an `if`, so the script fell through and returned 0 --
    # a FAILING command reported as a clean pass. Rendering floor fixes it.
    make_virsh '{"return":{"exited":true,"exitcode":3.0}}'
    PATH="$FAKEBIN:$PATH" run timeout 60 "$VM_EXEC" fake-vm 'exit 3'
    [ "$status" -eq 3 ]
}

@test "vm-exec: an integral-but-float signal still maps to 128+N" {
    make_virsh '{"return":{"exited":true,"signal":15.0}}'
    PATH="$FAKEBIN:$PATH" run timeout 60 "$VM_EXEC" fake-vm 'pkill -f x'
    [ "$status" -eq 143 ]
}

@test "vm-exec: a genuinely fractional exitcode or signal is a protocol error" {
    local body
    for body in '{"return":{"exited":true,"exitcode":1.5}}' \
                '{"return":{"exited":true,"signal":15.5}}'; do
        make_virsh "$body"
        PATH="$FAKEBIN:$PATH" run timeout 60 "$VM_EXEC" fake-vm 'true'
        [ "$status" -eq 76 ]
        [[ "$output" == *"malformed or empty"* ]]
    done
}
