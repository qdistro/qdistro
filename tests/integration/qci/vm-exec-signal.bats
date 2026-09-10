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

# make_virsh <status-json> — a fake virsh answering guest-exec with pid 4242 and
# every guest-exec-status with the given JSON. Records each status call so we can
# assert vm-exec stopped polling instead of grinding through its retry budget.
make_virsh() {
    cat > "$FAKEBIN/virsh" <<EOF
#!/usr/bin/env bash
for a in "\$@"; do
    case "\$a" in
        *guest-exec-status*)
            echo "status" >> "$STATE"
            cat <<'JSON'
$1
JSON
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

@test "vm-exec: a signal-terminated command exits 128+N instead of polling a reaped PID" {
    make_virsh '{"return":{"exited":true,"signal":15}}'
    PATH="$FAKEBIN:$PATH" run timeout 60 "$VM_EXEC" fake-vm 'pkill -f whatever'
    [ "$status" -eq 143 ]                       # 128 + SIGTERM
    [[ "$output" == *"terminated by signal 15"* ]]
    # Exactly one status call: terminal status is one-shot, so a second call
    # would have hit the reaped-PID error that caused the original 30-retry loop.
    [ "$(wc -l < "$STATE")" -eq 1 ]
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

@test "vm-exec: a stale-PID error explains the pkill self-match instead of blaming qga" {
    make_virsh '{"error":{"class":"GenericError","desc":"PID ld does not exist"}}'
    # Keep the retry budget tiny so the test does not sit through the backoff.
    PATH="$FAKEBIN:$PATH" QDISTRO_VM_AGENT_MAX_ERRORS=2 run timeout 120 "$VM_EXEC" fake-vm 'pkill -f whatever'
    [ "$status" -ne 0 ]
    [[ "$output" == *"semantic error"* ]]
    [[ "$output" != *"not responding"* ]]
    [[ "$output" == *"pkill -f"* ]]
    [[ "$output" == *"[p]attern"* ]]
}
