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

# `run !` (used by the cleanup tests below) needs bats >= 1.5.
bats_require_minimum_version 1.5.0

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    VM_EXEC="$REPO_ROOT/scripts/vm/vm-exec"
    FAKEBIN="$BATS_TEST_TMPDIR/bin"
    mkdir -p "$FAKEBIN"
    STATE="$BATS_TEST_TMPDIR/calls"
    : > "$STATE"
    # vm-exec's orphan registry (see "orphan registry" in vm-exec). Per test, so
    # an entry left by one test's SIGKILL can never be reaped by another's.
    export QDISTRO_VM_EXEC_STATE_DIR="$BATS_TEST_TMPDIR/orphans"
    # The guest boot id every fake identity probe reports.
    FAKE_BOOT=11111111-2222-3333-4444-555555555555
}

# make_virsh <terminal-json> [nonterminal-count] — a fake virsh answering
# guest-exec with pid 4242, then <nonterminal-count> (default 1)
# {"exited":false} status responses, then the terminal response, then the
# reaped-PID error for any FURTHER call. That last part matters: terminal
# status is one-shot in qga, so a correct vm-exec must never poll again, and
# an incorrect one is caught here rather than passing on a forgiving stub.
#
# vm-exec also issues a launch-identity probe (`qd_startof <pid>`) right after
# guest-exec. It is answered here as pid 4243 with a fixed start time, and
# deliberately kept OUT of the $STATE ledger so the status-call assertions below
# still count only the polls against the command itself.
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
        *'"pid":4243'*)
            # launch-identity probe result. The probe script prints a TOKEN,
            # not a bare number: "start=987654 boot=<FAKE_BOOT>\n" base64'd.
            echo '{"return":{"exited":true,"exitcode":0,"out-data":"c3RhcnQ9OTg3NjU0IGJvb3Q9MTExMTExMTEtMjIyMi0zMzMzLTQ0NDQtNTU1NTU1NTU1NTU1Cg=="}}'
            exit 0
            ;;
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
        *qd_startof\ 4242*)
            echo '{"return":{"pid":4243}}'
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

# --- interrupted host-side vm-exec must clean up the guest command ----------
#
# qga's guest-exec is fire-and-forget: the guest shell keeps running after the
# host poller dies. An agent that killed a slow vm-exec and re-issued the same
# driver therefore left TWO driver shells racing on one VM, duplicating every
# request and row so no verdict was attributable -- permissions-gui/45 and /50
# in full-20260914T194046Z-13620 ("host-side vm-exec polling was interrupted
# twice, but the guest-agent commands remained alive").
#
# Cleanup must NOT be a blind `kill <pid>`: by the time a deferred trap runs the
# guest command may be long gone and its PID reused. These tests pin BOTH
# halves of the contract -- the (pid, start-time) identity guard, and the fact
# that vm-exec reports what the cleanup RPC actually did instead of promising a
# reap it never observed.

# A fake virsh that models three distinct guest-exec bodies:
#   the command itself      -> pid 4242 (never reaches terminal status)
#   the identity probe      -> pid 4243 (answers with $stamp_b64, or hangs when
#                              that file is empty)
#   the cleanup script      -> pid 4244 (answers with $cleanup_mode, or hangs
#                              when it is the string `hang`)
# Every guest-exec body is logged so the tests can inspect the cleanup script
# that was actually submitted.
make_signal_virsh() {
    local stamp=$1 cleanup_mode=$2 cleanup_out=${3:-no-live-processes pid=4242 tree=4242}
    EXECLOG="$BATS_TEST_TMPDIR/exec.log"
    : > "$EXECLOG"
    PROBELOG="$BATS_TEST_TMPDIR/probe.log"
    : > "$PROBELOG"
    # The probe script answers with a TOKEN ("start=<n>" or "gone"), never a
    # bare number: a bare number could not tell "already exited" apart from
    # "the probe did not work", which is exactly the round-8/live-VM defect.
    if [ -n "$stamp" ]; then
        printf 'start=%s boot=%s' "$stamp" "$FAKE_BOOT" | base64 -w0 > "$BATS_TEST_TMPDIR/stamp_b64"
    else
        : > "$BATS_TEST_TMPDIR/stamp_b64"
    fi
    printf '%s' "${PROBE_MODE:-normal}" > "$BATS_TEST_TMPDIR/probe_mode"
    printf '%s' "$cleanup_mode" > "$BATS_TEST_TMPDIR/cleanup_mode"
    printf '%s' "$cleanup_out" | base64 -w0 > "$BATS_TEST_TMPDIR/cleanup_out_b64"
    cat > "$FAKEBIN/virsh" <<EOF
#!/usr/bin/env bash
T="$BATS_TEST_TMPDIR"
arg=
for a in "\$@"; do case "\$a" in *execute*) arg="\$a";; esac; done
case "\$arg" in
    *guest-exec-status*)
        case "\$arg" in
            *'"pid":4243'*)
                echo probe >> "\$T/probe.log"
                pm=\$(cat "\$T/probe_mode" 2>/dev/null)
                sb=\$(cat "\$T/stamp_b64")
                case "\$pm" in
                    gone)
                        # The launched shell had already exited: the probe
                        # script says so, explicitly, and exits 0.
                        echo '{"return":{"exited":true,"exitcode":0,"out-data":"Z29uZQo="}}'
                        ;;
                    noout)
                        # THE ROUND-7 SHAPE against a real guest: terminal
                        # status, exit 2, NO out-data at all.
                        echo '{"return":{"exited":true,"exitcode":2}}'
                        ;;
                    *)
                        if [ -z "\$sb" ]; then
                            echo '{"return":{"exited":false}}'
                        else
                            echo "{\"return\":{\"exited\":true,\"exitcode\":0,\"out-data\":\"\$sb\"}}"
                        fi
                        ;;
                esac
                ;;
            *'"pid":4244'*)
                m=\$(cat "\$T/cleanup_mode")
                if [ "\$m" = hang ]; then
                    echo '{"return":{"exited":false}}'
                elif [ "\$m" = blockterm ]; then
                    # A TERM-RESISTANT wedged RPC. Plain \`timeout Ns\` sends TERM
                    # and then WAITS forever for a child that ignores it, so this
                    # is the shape that proves the cap is a HARD wall and not a
                    # polite request. Only \`timeout -k\` can end it.
                    #
                    # stdio of every helper is redirected so the ONLY holder of
                    # the host's command-substitution pipe is this shell itself:
                    # otherwise a surviving grandchild keeps the pipe open and
                    # the measurement would be about fd bookkeeping, not signals.
                    trap '' TERM
                    # Self-destruct so a REGRESSED vm-exec fails this test in
                    # ~60s instead of wedging the whole suite forever.
                    ( sleep 60; kill -KILL \$\$ ) >/dev/null 2>&1 </dev/null &
                    while :; do sleep 5 >/dev/null 2>&1 </dev/null; done
                elif [ "\$m" = orphanpipe ]; then
                    # ROUND-6 SHAPE. The LEADER (this shell, the process the
                    # host's \`timeout\` monitors) EXITS on TERM, while a
                    # descendant IGNORES TERM and RETAINS this process's stdout.
                    #
                    # This is the case \`timeout -k\` does NOT cover: timeout
                    # waits only for the leader, so the leader's exit makes
                    # timeout exit too and the later group KILL is never sent.
                    # If the host captured our stdout through a command
                    # substitution pipe, the orphan holds that pipe's write end
                    # and the host blocks on EOF forever.
                    #
                    # NOTE the deliberate ABSENCE of any stdio redirection on
                    # the background subshell: the round-5 test redirected every
                    # helper's stdio and therefore excluded exactly this
                    # failure.
                    (
                        trap '' TERM
                        # Self-destruct: bounded lifetime so a REGRESSED vm-exec
                        # fails this test in ~45s instead of wedging the suite.
                        i=0
                        while [ \$i -lt 45 ]; do sleep 1; i=\$((i + 1)); done
                    ) &
                    # Short sleeps so the TERM trap runs promptly rather than
                    # being deferred behind a long foreground sleep.
                    trap 'exit 0' TERM
                    while :; do sleep 0.2; done
                elif [ "\$m" = block ]; then
                    # A WEDGED QGA: this RPC never answers. exec so the process
                    # the host-side timeout kills IS the sleep -- otherwise a
                    # surviving grandchild would hold the command substitution's
                    # pipe open and the host would block for the whole sleep
                    # regardless of any host-side cap.
                    exec sleep 300
                else
                    echo "{\"return\":{\"exited\":true,\"exitcode\":\$m,\"out-data\":\"\$(cat "\$T/cleanup_out_b64")\"}}"
                fi
                ;;
            *)
                if [ -e "\$T/statuserr" ]; then
                    echo '{"error":{"class":"GenericError","desc":"injected transient"}}'
                else
                    echo '{"return":{"exited":false}}'
                fi ;;
        esac
        ;;
    *'"guest-exec"'*)
        printf '%s\n' "\$arg" >> "\$T/exec.log"
        case "\$arg" in
            *qd_kill_checked*)
                if [ -e "\$T/killfail" ]; then
                    echo '{"error":{"class":"GenericError","desc":"injected submission failure"}}'
                else
                    echo '{"return":{"pid":4244}}'
                fi ;;
            *qd_startof\ 4242*) echo '{"return":{"pid":4243}}' ;;
            *)                echo '{"return":{"pid":4242}}' ;;
        esac
        ;;
    *) case " \$* " in
           *" domuuid "*) if [ -s "\$T/domuuid" ]; then cat "\$T/domuuid"; else exit 1; fi ;;
           *) echo '{"return":{}}' ;;
       esac ;;
esac
exit 0
EOF
    chmod +x "$FAKEBIN/virsh"
}

# Start vm-exec in the background, wait until its poll loop is up (the command
# guest-exec has been logged), TERM it, and return its wait status in $TERM_RC
# with its output in $TERM_OUT.
term_vm_exec() {
    PATH="$FAKEBIN:$PATH" "$VM_EXEC" fake-vm 'sleep 600' \
        >"$BATS_TEST_TMPDIR/out" 2>&1 &
    local pid=$! _tick
    for _tick in $(seq 1 200); do
        [ -s "$EXECLOG" ] && break
        sleep 0.1
    done
    [ -s "$EXECLOG" ]
    if [ -n "${1:-}" ]; then
        # Wait for a specific host-side line (e.g. the degraded-identity WARN)
        # before interrupting, so the signal lands after the branch under test.
        for _tick in $(seq 1 300); do
            grep -qF "$1" "$BATS_TEST_TMPDIR/out" && break
            sleep 0.1
        done
    else
        # Give the identity probe a moment to land before interrupting, so the
        # happy-path tests exercise the identity-verified branch.
        for _tick in $(seq 1 100); do
            [ "$(wc -l < "$EXECLOG")" -ge 2 ] && break
            sleep 0.1
        done
    fi
    local t0 t1
    t0=$(mono_s)
    kill -TERM "$pid"
    TERM_RC=0
    wait "$pid" || TERM_RC=$?
    t1=$(mono_s)
    TERM_ELAPSED=$((t1 - t0))
    TERM_OUT=$(cat "$BATS_TEST_TMPDIR/out")
}

# Monotonic whole seconds, same source vm-exec uses. Deliberately NOT date/
# EPOCHSECONDS: the assertion below is about a monotonic bound, so the test's
# own clock must not be able to jump with NTP either.
mono_s() {
    local u
    read -r u _ < /proc/uptime
    printf '%s' "${u%%.*}"
}

@test "vm-exec: SIGTERM pins the launch identity into the cleanup script and re-raises" {
    # Wait for identity capture to COMPLETE, not merely to have been issued.
    # Waiting on EXECLOG alone is a race: the trap is armed before
    # capture_guest_identity runs, and GUEST_START_TIME is assigned only after
    # the probe's command substitution returns, so a TERM landing in between is
    # deferred and then runs the cleanup with an EMPTY stamp -- which is this
    # test's failed assertion. On an idle host the probe finishes inside the
    # 100ms poll granularity and the window is missed; it is not a flake, and I
    # wrongly called it one. A PATH shim delaying every `jq` by 250ms fails it
    # 2/2 (fable, todo/reviews/qci-A2-260917-fable-review.md section 4).
    make_signal_virsh 987654 0
    QDISTRO_VM_KILL_VERIFY_TIMEOUT=10 term_vm_exec "guest identity pinned"

    # Died of the signal it was sent, so the caller's wait status is honest.
    [ "$TERM_RC" -eq 143 ]
    [[ "$TERM_OUT" == *"received SIGTERM"* ]]
    # The identity probe ran and succeeded: no degraded-cleanup warning.
    [[ "$TERM_OUT" != *"identity probe for guest PID"* ]]
    # The cleanup script carries BOTH halves of the identity, so the guest can
    # refuse a recycled PID. This is the whole point of the fix: a body that
    # only named the pid would pass the old "a kill script was submitted" test.
    grep -q 'qd_kill_checked KILL' "$EXECLOG"
    grep -q 'p=4242' "$EXECLOG"
    grep -q 'stamp=\\"987654\\"' "$EXECLOG"
    grep -q 'identity-mismatch' "$EXECLOG"
    # And the outcome reported is the one the guest actually returned.
    [[ "$TERM_OUT" == *"cleanup verified for guest PID 4242"* ]]
}

@test "vm-exec: a cleanup RPC that never completes is reported as unverified, not as a reap" {
    make_signal_virsh 987654 hang
    QDISTRO_VM_KILL_VERIFY_TIMEOUT=2 term_vm_exec

    [ "$TERM_RC" -eq 143 ]
    [[ "$TERM_OUT" == *"cannot confirm the guest command was reaped"* ]]
    [[ "$TERM_OUT" != *"cleanup verified"* ]]
}

@test "vm-exec: a guest-side identity mismatch is reported as a refusal to signal" {
    make_signal_virsh 987654 3 'identity-mismatch pid=4242 start=111 expected=987654'
    QDISTRO_VM_KILL_VERIFY_TIMEOUT=10 term_vm_exec

    [ "$TERM_RC" -eq 143 ]
    [[ "$TERM_OUT" == *"REFUSED to signal a possibly recycled PID"* ]]
    [[ "$TERM_OUT" == *"identity-mismatch"* ]]
    [[ "$TERM_OUT" != *"cleanup verified"* ]]
}

@test "vm-exec: surviving guest processes are reported as NOT verified" {
    make_signal_virsh 987654 5 'still-alive pid=4242 after TERM+KILL'
    QDISTRO_VM_KILL_VERIFY_TIMEOUT=10 term_vm_exec

    [ "$TERM_RC" -eq 143 ]
    [[ "$TERM_OUT" == *"could NOT be verified"* ]]
    [[ "$TERM_OUT" == *"survived TERM+KILL"* ]]
}

@test "vm-exec: an unanswerable identity probe makes cleanup REFUSE, and says so at launch" {
    # Empty stamp file => the probe's status never goes terminal.
    make_signal_virsh '' 3 'identity-unknown pid=4242: no launch identity was captured; refusing to signal a possibly recycled pid'
    QDISTRO_VM_IDENT_POLLS=1 QDISTRO_VM_KILL_VERIFY_TIMEOUT=10 \
        term_vm_exec "identity probe for guest PID 4242 did not answer"

    [ "$TERM_RC" -eq 143 ]
    [[ "$TERM_OUT" == *"identity probe for guest PID 4242 did not answer"* ]]
    # It must say WHICH way it failed, so a real malfunction is not confused
    # with the benign already-exited race.
    [[ "$TERM_OUT" == *"probe-failed(rc=1)"* ]]
    # The warning must predict the actual behaviour (refusal + possible orphan),
    # not the removed age-guard.
    [[ "$TERM_OUT" == *"cleanup will REFUSE to signal it"* ]]
    [[ "$TERM_OUT" == *"may leave it running in the guest"* ]]
    [[ "$TERM_OUT" == *"REFUSED to signal a possibly recycled PID"* ]]
    [[ "$TERM_OUT" != *"cleanup verified"* ]]
    # The submitted script carries an empty stamp and no age escape hatch.
    grep -q 'stamp=\\"\\"' "$EXECLOG"
    run ! grep -q 'maxage' "$EXECLOG"
}

# --- the in-guest cleanup script itself, run against REAL host processes -----
#
# The script is POSIX sh over /proc and ps, so it can be extracted from vm-exec
# and exercised directly. No VM and no fake: these tests pin the identity guard
# against actual PID identities, which is the only way to show that a recycled
# PID is not signalled.

render_kill_script() {   # <pid> <stamp> [boot]  (empty stamp == no captured identity; empty boot == no boot check)
    local body
    body=$(sed -n '/^KILL_TREE_SH=/,/^KILL_EOF$/p' "$VM_EXEC" | sed '1d;$d')
    [ -n "$body" ] || return 1
    body=${body//__PID__/$1}
    body=${body//__STAMP__/$2}
    body=${body//__BOOT__/${3:-}}
    printf '%s\n' "$body" > "$BATS_TEST_TMPDIR/kill.sh"
}

host_startof() {
    awk 'match($0,/\)[^)]*$/){s=substr($0,RSTART+2);split(s,f," ");print f[20]}' \
        "/proc/$1/stat" 2>/dev/null
}

@test "kill script: a matching (pid, start-time) identity is signalled and verified gone" {
    sleep 60 &
    local victim=$!
    local stamp
    stamp=$(host_startof "$victim")
    [ -n "$stamp" ]

    render_kill_script "$victim" "$stamp"
    run sh "$BATS_TEST_TMPDIR/kill.sh"
    [ "$status" -eq 0 ]
    [[ "$output" == *"identity-verified"* ]]
    # The success message states what was OBSERVED -- that no signalable process
    # of the pinned tree is left -- rather than "reaped", which would claim the
    # parent's wait(2) as well. A zombie is still a process table entry.
    [[ "$output" == *"no-live-processes"* ]]
    [[ "$output" != *"reaped pid="* ]]
    run ! kill -0 "$victim"
    wait "$victim" 2>/dev/null || true
}

@test "kill script: a recycled PID (start-time mismatch) is NOT signalled" {
    sleep 60 &
    local victim=$!
    local stamp
    stamp=$(host_startof "$victim")
    [ -n "$stamp" ]

    # Same PID, different start time: exactly what a caller sees when the
    # original command exited and the guest kernel handed the number out again.
    render_kill_script "$victim" "$((stamp + 1))"
    run sh "$BATS_TEST_TMPDIR/kill.sh"
    [ "$status" -eq 3 ]
    [[ "$output" == *"identity-mismatch"* ]]
    # The innocent process is untouched -- the regression this guard exists for.
    kill -0 "$victim"
    kill -KILL "$victim" 2>/dev/null || true
    wait "$victim" 2>/dev/null || true
}

@test "kill script: an already-exited PID is a no-op, not a blind kill" {
    sleep 0.1 &
    local victim=$!
    wait "$victim" 2>/dev/null || true

    render_kill_script "$victim" 987654
    run sh "$BATS_TEST_TMPDIR/kill.sh"
    [ "$status" -eq 0 ]
    [[ "$output" == *"already-gone"* ]]
}

@test "kill script: with NO captured identity, nothing is signalled -- not even a brand-new PID" {
    # Round 3: the old age-based fallback signalled any process younger than
    # QDISTRO_VM_IDENT_FALLBACK_MAX_AGE when the identity probe had not
    # answered. That fails open in its own threat model: PID reuse is MOST
    # likely right after our command exits, so a recycled PID is typically
    # young. This victim is a fraction of a second old -- the most favourable
    # case the fallback had -- and must still be refused.
    sleep 60 &
    local victim=$!

    render_kill_script "$victim" ""
    run sh "$BATS_TEST_TMPDIR/kill.sh"
    [ "$status" -eq 3 ]
    [[ "$output" == *"identity-unknown"* ]]
    [[ "$output" == *"no launch identity was captured"* ]]
    [[ "$output" == *"refusing to signal"* ]]
    # Untouched: the refusal is the whole point, an orphan is the accepted cost.
    kill -0 "$victim"
    kill -KILL "$victim" 2>/dev/null || true
    wait "$victim" 2>/dev/null || true
}

@test "kill script: no captured identity does not even leak an age window" {
    # Same refusal for an OLD process, so the exit code carries one meaning
    # ("could not be identity-checked") rather than two.
    sleep 60 &
    local victim=$!
    sleep 2

    render_kill_script "$victim" ""
    run sh "$BATS_TEST_TMPDIR/kill.sh"
    [ "$status" -eq 3 ]
    [[ "$output" == *"no launch identity was captured"* ]]
    # No age arithmetic survives in the script at all.
    run ! grep -q 'maxage' "$BATS_TEST_TMPDIR/kill.sh"
    kill -0 "$victim"
    kill -KILL "$victim" 2>/dev/null || true
    wait "$victim" 2>/dev/null || true
}

@test "kill script: descendants collected after the identity check are signalled too" {
    local childfile="$BATS_TEST_TMPDIR/child.pid"
    sh -c 'sleep 60 & echo $! > "$1"; wait' _ "$childfile" &
    local parent=$!
    local _tick
    for _tick in $(seq 1 100); do
        [ -s "$childfile" ] && break
        sleep 0.1
    done
    local child
    child=$(cat "$childfile")
    [ -n "$child" ]
    kill -0 "$child"

    local stamp
    stamp=$(host_startof "$parent")
    render_kill_script "$parent" "$stamp"
    run sh "$BATS_TEST_TMPDIR/kill.sh"
    [ "$status" -eq 0 ]
    [[ "$output" == *"no-live-processes"* ]]
    run ! kill -0 "$parent"
    run ! kill -0 "$child"
    wait "$parent" 2>/dev/null || true
}

@test "kill script: a TERM-ignoring descendant is KILLed even when its ROOT exits" {
    # Round 3 defect: escalation used to be gated on the ROOT surviving TERM --
    #   if [ -n "$(qd_startof "$p")" ]; then kill -KILL $list; fi
    # so a child that ignores TERM under a root that dies of it was never KILLed
    # at all. The script then reported exit 6 "descendants-alive", i.e. a
    # cleanup failure for work it had not actually attempted.
    local childfile="$BATS_TEST_TMPDIR/stubborn.pid"
    cat > "$BATS_TEST_TMPDIR/stubborn.sh" <<'EOS'
trap "" TERM
echo $$ > "$1"
while :; do sleep 5; done
EOS
    cat > "$BATS_TEST_TMPDIR/root.sh" <<'EOS'
sh "$1" "$2" &
sleep 600
EOS
    # Detach stdio: a TERM-ignoring survivor would otherwise keep bats' output
    # pipe open and turn a FAILING assertion below into a hang instead of a
    # failure. Without the fix this test must fail fast, not wedge the suite.
    sh "$BATS_TEST_TMPDIR/root.sh" "$BATS_TEST_TMPDIR/stubborn.sh" "$childfile" \
        >/dev/null 2>&1 </dev/null &
    local root=$!
    local _tick
    for _tick in $(seq 1 100); do
        [ -s "$childfile" ] && break
        sleep 0.1
    done
    local child
    child=$(cat "$childfile")
    [ -n "$child" ]
    kill -0 "$child"

    local stamp
    stamp=$(host_startof "$root")
    [ -n "$stamp" ]
    render_kill_script "$root" "$stamp"
    run sh "$BATS_TEST_TMPDIR/kill.sh"

    # The whole tree is gone, and the script says so because it observed it.
    [ "$status" -eq 0 ]
    [[ "$output" == *"no-live-processes"* ]]
    [[ "$output" != *"descendants-alive"* ]]
    run ! kill -0 "$root"
    run ! kill -0 "$child"
    # Belt and braces: never leave a TERM-immune process behind on a failure.
    kill -KILL "$child" "$root" 2>/dev/null || true
    wait "$root" 2>/dev/null || true
}

@test "kill script: a TERM-ignoring ROOT is escalated and reported as still-alive" {
    # The other half of the same escalation: when KILL cannot be delivered the
    # report must name the root (exit 5), never claim a reap.
    cat > "$BATS_TEST_TMPDIR/ignoring.sh" <<'EOS'
trap "" TERM
while :; do sleep 5; done
EOS
    sh "$BATS_TEST_TMPDIR/ignoring.sh" >/dev/null 2>&1 </dev/null &
    local victim=$!
    local stamp
    stamp=$(host_startof "$victim")
    [ -n "$stamp" ]

    render_kill_script "$victim" "$stamp"
    run sh "$BATS_TEST_TMPDIR/kill.sh"
    # KILL is undeniable, so the observed outcome is "nothing live left" --
    # which is exactly the point: exit 5 is reserved for a root that survived a
    # real KILL.
    [ "$status" -eq 0 ]
    [[ "$output" == *"no-live-processes"* ]]
    run ! kill -0 "$victim"
    wait "$victim" 2>/dev/null || true
}

@test "vm-exec: a surviving DESCENDANT (exit 6) is also reported as NOT verified" {
    make_signal_virsh 987654 6 'descendants-alive: 4299'
    QDISTRO_VM_KILL_VERIFY_TIMEOUT=10 term_vm_exec

    [ "$TERM_RC" -eq 143 ]
    [[ "$TERM_OUT" == *"could NOT be verified"* ]]
    [[ "$TERM_OUT" == *"descendants-alive"* ]]
    [[ "$TERM_OUT" != *"cleanup verified"* ]]
}

@test "vm-exec: a WEDGED cleanup RPC still honours the verify deadline in wall-clock seconds" {
    # Round 3 defect: the verify loop counted ITERATIONS, and each iteration is
    # an agent_rpc that can itself block for QDISTRO_VM_AGENT_RPC_TIMEOUT. With
    # the documented 30s bound that was 30 x (30s + 1s) ~= 930s of a deferred
    # trap refusing to re-raise TERM -- under QCI_JOBS=8 an apparently hung
    # gate, strictly worse than the orphan the cleanup exists to prevent.
    #
    # Here the cleanup status RPC NEVER answers and the per-RPC cap is 60s,
    # deliberately larger than the 4s verify budget: only a real monotonic
    # deadline that caps each individual RPC to the REMAINING budget can bring
    # this back in time. Old shape: >= 4 x (60 + 1) = 244s. New shape: ~5s.
    make_signal_virsh 987654 block
    QDISTRO_VM_KILL_VERIFY_TIMEOUT=4 QDISTRO_VM_AGENT_RPC_TIMEOUT=60 term_vm_exec

    [ "$TERM_RC" -eq 143 ]
    [[ "$TERM_OUT" == *"did not finish within 4s"* ]]
    [[ "$TERM_OUT" == *"cannot confirm the guest command was reaped"* ]]
    [[ "$TERM_OUT" != *"cleanup verified"* ]]
    # Upper bound: the budget, plus one trailing `sleep 1`, plus the trap's
    # deferral behind the (instant) main-loop poll. Generous, but two orders of
    # magnitude below the pre-fix behaviour.
    [ "$TERM_ELAPSED" -lt 20 ]
    # Lower bound: it really did spend its budget rather than giving up at once,
    # so this pins the bound, not merely a fast failure.
    [ "$TERM_ELAPSED" -ge 3 ]
}

# --- Round-5 review additions (todo/reviews/qci-r4-review.md) ---------------

@test "vm-exec: a TERM-RESISTANT cleanup RPC is KILLed, so the wall-clock bound is HARD" {
    # Round-4 defect: `agent_rpc` used `timeout "${cap}s"` with no --kill-after.
    # GNU timeout sends TERM at expiry and then WAITS, so a virsh that catches
    # or ignores TERM (or sits in an uninterruptible RPC) outlives the cap
    # FOREVER. Every "hard bound" in vm-exec -- the cleanup budget and the
    # signal-to-re-raise claim alike -- was therefore unprovable: one wedged RPC
    # holds the deferred trap indefinitely.
    #
    # The `block` test above uses a TERM-RESPECTING sleep, so it passes with or
    # without the fix. This one does not: the fake ignores TERM outright, and
    # only `timeout -k` can end it.
    #
    # Budget 4s, per-RPC cap 60s (deliberately larger), KILL grace 2s:
    #   cap_within(4, 60) = 4 - 2 = 2  =>  the RPC is dead by 2 + 2 = 4s.
    make_signal_virsh 987654 blockterm
    QDISTRO_VM_KILL_VERIFY_TIMEOUT=4 QDISTRO_VM_AGENT_RPC_TIMEOUT=60 \
        QDISTRO_VM_SIGKILL_GRACE=2 term_vm_exec

    [ "$TERM_RC" -eq 143 ]
    [[ "$TERM_OUT" == *"did not finish within 4s"* ]]
    [[ "$TERM_OUT" == *"cannot confirm the guest command was reaped"* ]]
    [[ "$TERM_OUT" != *"cleanup verified"* ]]
    # The bound, with room for the trap deferral behind the (instant) main-loop
    # poll. Without --kill-after this is 60s+ -- in the un-self-destructing case,
    # unbounded.
    [ "$TERM_ELAPSED" -lt 20 ]
    # And it really did spend its budget rather than giving up at once.
    [ "$TERM_ELAPSED" -ge 3 ]
}

@test "vm-exec: agent_rpc always arms a KILL escalation, never a bare TERM cap" {
    # A static guard so the mechanism cannot be silently reverted: every bounded
    # invocation in vm-exec goes through `timeout -k`.
    # The reverted form would be `timeout "${cap}s" virsh ...`.
    run ! grep -n 'timeout "' "$VM_EXEC"
    grep -q 'timeout -k "\${SIGKILL_GRACE}s" "\${cap}s"' "$VM_EXEC"
}

# --- descendant identity: the round-3 root fix, applied one level down ------
#
# Round-4 defect: `qd_survivors` tested only "is this number non-zombie" and
# THREW THE START TIME AWAY, then `kill -KILL $alive` acted on a bare PID. A
# descendant that exits after the poll and whose number is reused before the
# kill was therefore signalled as a stranger -- the exact bug round 3 closed for
# the root, left open at the descendant boundary.
#
# The pin table ($map) and the pre-signal re-read are what fix it. These tests
# load the script's FUNCTIONS against REAL host processes and drive the pin
# table directly, which is the only way to present a "same PID, different start
# time" descendant deterministically.

render_kill_funcs() {
    render_kill_script 1 1
    # Everything above the main body: the helper functions only.
    sed -n '1,/^cur=\$(qd_startof/p' "$BATS_TEST_TMPDIR/kill.sh" | sed '$d' \
        > "$BATS_TEST_TMPDIR/killfuncs.sh"
    grep -q 'qd_kill_checked()' "$BATS_TEST_TMPDIR/killfuncs.sh"
}

# drive_pin <pinned-start-time> <pid>  -- run the script's own helpers with a
# pin table that claims <pinned-start-time> for <pid>.
drive_pin() {
    cat > "$BATS_TEST_TMPDIR/drive.sh" <<EOS
. "$BATS_TEST_TMPDIR/killfuncs.sh"
map="999999:1 $2:$1"
echo "survivors:[\$(qd_survivors "$2")]"
qd_kill_checked KILL "$2"
EOS
}

@test "kill script: a DESCENDANT whose PID was recycled is NOT signalled" {
    sleep 60 &
    local victim=$!
    local stamp
    stamp=$(host_startof "$victim")
    [ -n "$stamp" ]

    render_kill_funcs
    # Same PID, different start time -- a descendant that exited between the
    # BFS and the escalation, whose number the kernel handed to someone else.
    drive_pin "$((stamp + 1))" "$victim"
    run sh "$BATS_TEST_TMPDIR/drive.sh"

    [ "$status" -eq 0 ]
    # Not a survivor: OUR descendant is gone, so there is nothing to escalate.
    [[ "$output" == *"survivors:[]"* ]]
    # And the re-read immediately before the kill refuses out loud rather than
    # signalling the stranger now holding the number.
    [[ "$output" == *"skipped-recycled pid=$victim"* ]]
    # The innocent process is untouched -- the regression this guard exists for.
    kill -0 "$victim"
    kill -KILL "$victim" 2>/dev/null || true
    wait "$victim" 2>/dev/null || true
}

@test "kill script: a DESCENDANT whose pinned identity still holds IS signalled" {
    # The positive control for the test above: the guard must not be a blanket
    # refusal, or "identity-checked cleanup" would just be "no cleanup".
    sleep 60 &
    local victim=$!
    local stamp
    stamp=$(host_startof "$victim")
    [ -n "$stamp" ]

    render_kill_funcs
    drive_pin "$stamp" "$victim"
    run sh "$BATS_TEST_TMPDIR/drive.sh"

    [ "$status" -eq 0 ]
    [[ "$output" == *"survivors:[ $victim]"* ]]
    [[ "$output" != *"skipped-recycled"* ]]
    run ! kill -0 "$victim"
    wait "$victim" 2>/dev/null || true
}

@test "kill script: no bare-PID escalation survives anywhere in the tree walk" {
    # Static guard on the mechanism: every signal must go through the
    # identity-re-reading helper. `kill -KILL $alive` and `kill -TERM $list`
    # were the two bare-PID sites the round-4 review named.
    render_kill_script 4242 987654
    run ! grep -nE 'kill -(TERM|KILL) \$' "$BATS_TEST_TMPDIR/kill.sh"
    grep -q 'qd_kill_checked TERM' "$BATS_TEST_TMPDIR/kill.sh"
    grep -q 'qd_kill_checked KILL' "$BATS_TEST_TMPDIR/kill.sh"
}

@test "kill script: descendants are pinned WITH the BFS list, not just the root" {
    # End-to-end shape of the fix: the map built during the walk carries a
    # (pid, starttime) entry for every descendant, and a descendant is only
    # admitted when its ppid still matches the parent it was discovered under.
    local childfile="$BATS_TEST_TMPDIR/child.pid"
    sh -c 'sleep 60 & echo $! > "$1"; wait' _ "$childfile" &
    local parent=$!
    local _tick
    for _tick in $(seq 1 100); do
        [ -s "$childfile" ] && break
        sleep 0.1
    done
    local child
    child=$(cat "$childfile")
    [ -n "$child" ]
    local childstamp
    childstamp=$(host_startof "$child")
    [ -n "$childstamp" ]

    local stamp
    stamp=$(host_startof "$parent")
    render_kill_script "$parent" "$stamp"
    # Echo the pin table the walk produced, without changing the script's
    # logic. It must go BEFORE the trailing `exit 0`, hence `sed '$i'`.
    sed -i '$i echo "map=[$map]"' "$BATS_TEST_TMPDIR/kill.sh"
    run sh "$BATS_TEST_TMPDIR/kill.sh"

    [ "$status" -eq 0 ]
    [[ "$output" == *"map=["* ]]
    [[ "$output" == *"$parent:$stamp"* ]]
    [[ "$output" == *"$child:$childstamp"* ]]
    [[ "$output" == *"no-live-processes"* ]]
    run ! kill -0 "$child"
    wait "$parent" 2>/dev/null || true
}

@test "kill script: a zombie tree is reported as no-live-processes, never as reaped" {
    # The round-4 wording point: treating Z as non-live is right for signalling
    # and resource cleanup, but "reaped" claimed the parent's wait(2) as well.
    # A zombie is still a process table entry. Report what was observed.
    render_kill_funcs
    grep -q 'zombies-pending-parent-wait' "$VM_EXEC"
    render_kill_script 4242 987654
    run ! grep -qE '^echo "reaped' "$BATS_TEST_TMPDIR/kill.sh"
    grep -q 'no-live-processes' "$BATS_TEST_TMPDIR/kill.sh"
}

# --- Round-6 review additions (todo/reviews/qci-r6-review.md) --------------
#
# Round 5 claimed a hard host-time bound and wrote a mutation test for it. The
# round-6 review showed the test EXCLUDED the failure it claimed to cover: it
# redirected every helper's stdio so only the leader could hold the host's
# command-substitution pipe, which is precisely the case `timeout -k` already
# handles. The two tests below cover the two shapes it missed.

@test "vm-exec: a TERM-ignoring DESCENDANT holding the RPC's stdout cannot wedge vm-exec" {
    # THE ROUND-6 DEFECT, reproduced. The fake virsh's leader EXITS on TERM
    # while a child IGNORES TERM and keeps the leader's stdout. GNU `timeout`
    # waits only for the leader, so the leader's exit ends `timeout` and the
    # group KILL it promised at cap+grace is never delivered. With the output
    # captured through a command-substitution pipe, that orphan holds the pipe's
    # write end and vm-exec waits for EOF forever -- deferred signal trap and
    # all.
    #
    # What closes it is bounded_run()'s file capture: the child's stdout is a
    # regular file, so no descendant ever inherits the pipe and the return
    # depends on `timeout`'s own exit alone.
    #
    # Arithmetic, budget 4s / grace 2s / RPC cap 60s:
    #   cap_within(4, 60) = 4 - 2 = 2  -> the leader is TERMed at 2s and exits;
    #   bounded_run returns immediately; the loop re-checks and breaks by ~5s.
    # The orphan self-destructs after 45s, so a REGRESSED vm-exec fails this
    # assertion in ~46s instead of wedging the suite.
    make_signal_virsh 987654 orphanpipe
    QDISTRO_VM_KILL_VERIFY_TIMEOUT=4 QDISTRO_VM_AGENT_RPC_TIMEOUT=60 \
        QDISTRO_VM_SIGKILL_GRACE=2 term_vm_exec

    [ "$TERM_RC" -eq 143 ]
    [[ "$TERM_OUT" == *"did not finish within 4s"* ]]
    [[ "$TERM_OUT" == *"cannot confirm the guest command was reaped"* ]]
    [[ "$TERM_OUT" != *"cleanup verified"* ]]
    # The whole point: bounded in RETURN, not merely "the monitored process was
    # signalled". Pre-fix this is ~46s (and, without the self-destruct, never).
    [ "$TERM_ELAPSED" -lt 25 ]
    # And it really did spend its budget rather than bailing out instantly.
    [ "$TERM_ELAPSED" -ge 3 ]
}

@test "vm-exec: bounded commands capture stdout through a FILE, never the caller's pipe" {
    # Static guard on the mechanism above, so it cannot be quietly reverted to
    # a plain `$(timeout -k ... cmd)`.
    grep -q '^bounded_run() {' "$VM_EXEC"
    grep -q '>&"\$wfd"' "$VM_EXEC"
    # Exactly ONE executable `timeout -k` invocation in the whole script: if a
    # second one appears it is running outside the file capture.
    [ "$(grep -cE '^[[:space:]]*(exec )?timeout -k ' "$VM_EXEC")" -eq 1 ]
    # And the three wrappers all delegate to it rather than calling timeout.
    grep -q 'capped_local() {' "$VM_EXEC"
    grep -q 'bounded_run "\$LOCAL_OP_TIMEOUT" "\$@"' "$VM_EXEC"
    grep -q 'bounded_run "\$(cap_within "\$left" "\$LOCAL_OP_TIMEOUT")" "\$@"' "$VM_EXEC"
    grep -q 'bounded_run "\$cap" virsh' "$VM_EXEC"
}

# A fake jq that is SLOW and TERM-RESISTANT for exactly one argv shape (matched
# on <marker>, the cleanup script's guest pid) and delegates transparently to
# the real jq for every other call. This lets a test burn a local helper's whole
# cap at one precise site without perturbing the rest of vm-exec's jq usage.
install_slow_jq() {   # <argv-marker> <seconds>
    local marker=$1 secs=$2 realjq
    realjq=$(command -v jq)
    [ -n "$realjq" ]
    cat > "$FAKEBIN/jq" <<EOF
#!/usr/bin/env bash
slow=
for a in "\$@"; do [ "\$a" = "$marker" ] && slow=1; done
if [ -n "\$slow" ]; then
    # Resist TERM, exactly as the round-6 scenario describes.
    trap '' TERM
    i=0
    while [ \$i -lt $secs ]; do sleep 1; i=\$((i + 1)); done
fi
exec "$realjq" "\$@"
EOF
    chmod +x "$FAKEBIN/jq"
}

@test "vm-exec: the cleanup RPC budget is recomputed AFTER its request is built" {
    # ROUND-6 DEFECT 2. The status loop read `left` at the top of the iteration
    # and then passed THAT value to rpc_cap AFTER a local jq had already run:
    #
    #   st=$(agent_rpc "$(budgeted_local "$deadline" jq ...)" "$(rpc_cap "$left")")
    #                    ^-- can burn its whole cap + KILL grace   ^-- stale
    #
    # so a TERM-resisting jq bought the following RPC a full pre-jq budget and
    # the return landed well past the deadline.
    #
    # Knobs: budget 20s, local cap 18s, grace 2s, RPC cap 60s, jq sleeps 12s,
    # and the cleanup status RPC never answers (`block`).
    #
    #   t=0   left=20; jq cap = cap_within(20,18) = 16; jq returns at t=12
    #   FIXED: left is re-read = 8; rpc_cap(8) = 6; RPC dies t=18; sleep -> 19
    #          left=1; jq cap=1, KILLed at t=22; sleep -> 23 = 20 + 2 + 1, the
    #          documented KILL_VERIFY_TIMEOUT + SIGKILL_GRACE + 1 bound.
    #   STALE: rpc_cap(20) = 18; RPC dies t=30; sleep -> ~31s, i.e. deadline
    #          + grace + AGENT-RPC-shaped overshoot.
    make_signal_virsh 987654 block
    install_slow_jq 4244 12
    QDISTRO_VM_KILL_VERIFY_TIMEOUT=20 QDISTRO_VM_LOCAL_OP_TIMEOUT=18 \
        QDISTRO_VM_SIGKILL_GRACE=2 QDISTRO_VM_AGENT_RPC_TIMEOUT=60 term_vm_exec

    [ "$TERM_RC" -eq 143 ]
    [[ "$TERM_OUT" == *"did not finish within 20s"* ]]
    [[ "$TERM_OUT" != *"cleanup verified"* ]]
    # It really did spend its budget (the slow jq alone is 12s).
    [ "$TERM_ELAPSED" -ge 15 ]
    # KILL_VERIFY_TIMEOUT + SIGKILL_GRACE + 1 = 23, plus slack for the trap
    # deferral behind the (instant) main-loop poll. The stale-budget shape
    # lands at ~31s.
    [ "$TERM_ELAPSED" -le 26 ]
}

# --- Round-8 review additions (todo/reviews/qci-r8-review.md) --------------
#
# Round 7 captured every bounded command's stdout through ONE reusable file and
# replayed it with `$(<file)`. The round-8 review showed the round-6 test above
# could not expose what that left open, because its orphan HOLDS fd 1 without
# ever WRITING to it:
#
#   * a descendant that keeps writing makes the replay of a live-growing
#     regular file need never reach EOF -- an unbounded wait, outside `timeout`;
#   * bytes it appends after the next `: > $QD_CAP_FILE` land in the NEXT
#     call's jq/virsh result;
#   * the inode outlives its pathname, so the writer can consume disk without
#     limit.
#
# The tests below drive the repaired bounded_run with a descendant that
# survives its leader and actively writes BOTH stdout and stderr afterwards.

# An adversarial command: the LEADER exits on TERM, while a descendant IGNORES
# TERM and keeps writing BOTH stdout and stderr for ~20s afterwards. $1 selects
# whether it also ignores SIGXFSZ.
#
#   xfsz-ignore : the file-size limit turns into an EFBIG write error, so the
#                 descendant STAYS ALIVE and keeps trying. That is the shape
#                 needed to prove the NEXT call is uncontaminated while a
#                 survivor is still writing.
#   xfsz-default: the default disposition applies, so the kernel KILLS the
#                 descendant at the ceiling. That is the shape that proves
#                 RLIMIT_FSIZE is really being inherited and enforced.
write_flooder() {   # <xfsz-ignore|xfsz-default>
    FLOODSH="$BATS_TEST_TMPDIR/flooder.sh"
    cat > "$FLOODSH" <<FLOOD_EOF
#!/usr/bin/env bash
(
  trap '' TERM
  [ "$1" = xfsz-ignore ] && trap '' XFSZ
  pad=\$(printf 'CONTAMINATION%.0s' \$(seq 1 300))
  i=0
  while [ \$i -lt 400 ]; do
    printf '%s\n' "\$pad" 2>/dev/null || true
    printf 'ERRFLOOD %s\n' "\$i" >&2 2>/dev/null || true
    i=\$((i + 1))
    sleep 0.05
  done
) &
trap 'exit 0' TERM
while :; do sleep 0.2; done
FLOOD_EOF
    chmod +x "$FLOODSH"
}

# Extract the bounded-capture block from vm-exec VERBATIM (same marker
# convention as render_kill_script above) and wrap it in a driver, so these
# assertions are about the shipped code and not about a copy of it.
write_cap_driver() {   # <ceiling-bytes> <driver-body>
    local ceiling=$1 body=$2 out="$BATS_TEST_TMPDIR/capdrv.sh"
    {
        echo '#!/usr/bin/env bash'
        echo 'set -euo pipefail'
        echo 'SIGKILL_GRACE=2'
        echo "QDISTRO_VM_CAPTURE_MAX_BYTES=$ceiling"
        echo 'read_mono() { local u; read -r u _ < /proc/uptime; printf "%s" "${u%%.*}"; }'
        sed -n '/^# >>> bounded-capture block/,/^# <<< bounded-capture block/p' "$VM_EXEC"
        echo 'RES=$1; FLOODSH=$2; ERRFILE=$3'
        printf '%s\n' "$body"
    } > "$out"
    chmod +x "$out"
    CAPDRV="$out"
    # The extraction must have found the block, not produced an empty stub.
    grep -q '^bounded_run() {' "$out"
}

# Run the driver with stdio on FILES and a hard host-side wait.
#
# NOTE THE CALL SHAPE, it is deliberate. bounded_run bounds the CHILD's fd 1
# only; a descendant retaining the driver's fd 2 would hold a `run`-style
# command-substitution pipe open forever -- exactly the caller-side hole the
# round-8 review names at ci/lib/gates/gui.sh:1773. A test written with bats
# `run` here would wedge, which is why it is not written that way.
run_cap_driver() {   # <max-wait-seconds>
    local maxwait=$1 _tick pid
    CAPRES="$BATS_TEST_TMPDIR/capres"
    CAPOUT="$BATS_TEST_TMPDIR/capout"
    CAPERR="$BATS_TEST_TMPDIR/caperr"
    : > "$CAPRES"; : > "$CAPOUT"; : > "$CAPERR"
    "$CAPDRV" "$CAPRES" "$FLOODSH" "$CAPERR" >"$CAPOUT" 2>"$CAPERR" &
    pid=$!
    CAP_RC=""
    for _tick in $(seq 1 "$((maxwait * 10))"); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.1
    done
    if kill -0 "$pid" 2>/dev/null; then
        kill -KILL "$pid" 2>/dev/null || true
        wait "$pid" 2>/dev/null || true
        return 1
    fi
    CAP_RC=0
    wait "$pid" || CAP_RC=$?
    return 0
}

@test "bounded_run: a descendant writing stdout+stderr after its leader exits cannot stall the replay, is truncated at the ceiling, and cannot contaminate the next call" {
    # THE ROUND-8 DEFECT, reproduced. Ceiling 8192 so the flood reaches it in
    # well under a second; cap 2s and grace 2s so the leader is TERMed quickly.
    write_flooder xfsz-ignore
    write_cap_driver 8192 '
t0=$(read_mono)
set +e
A=$(bounded_run 2 "$FLOODSH")
A_RC=$?
set -e
t1=$(read_mono)
A_LEN=${#A}
# Leave the descendant running and come back to it: this is the window in
# which round 7 let it append into the NEXT call.
sleep 3
ERR1=$(stat -c %s "$ERRFILE")
sleep 2
ERR2=$(stat -c %s "$ERRFILE")
echo "MARK-B" >&2
set +e
B=$(bounded_run 5 printf CLEAN)
B_RC=$?
set -e
LEFTOVER=$(ls -A "$QD_CAP_DIR" | wc -l)
{
  echo "A_RC=$A_RC"
  echo "A_LEN=$A_LEN"
  echo "A_ELAPSED=$((t1 - t0))"
  echo "ERR1=$ERR1"
  echo "ERR2=$ERR2"
  echo "B=[$B]"
  echo "B_RC=$B_RC"
  echo "LEFTOVER=$LEFTOVER"
} > "$RES"
'
    # 25s hard wait: the driver's own sleeps total 5s and the descendant lives
    # ~20s, so a driver that has not finished by then is stuck in the replay.
    # (Against the round-7 shape this test fails on the SIZE assertion below
    # rather than on this wait -- a shell writer with 50ms gaps still lets a
    # `$(<file)` replay reach EOF. What round 7 actually loses is any bound on
    # how much it replays; see the fast-writer test below for the cost of that.)
    if ! run_cap_driver 25; then
        echo "driver did not finish: the replay of a live-growing capture did not terminate" >&2
        cat "$BATS_TEST_TMPDIR/capres" >&2 || true
        return 1
    fi
    [ "$CAP_RC" -eq 0 ]

    local A_RC A_LEN A_ELAPSED ERR1 ERR2 B B_RC LEFTOVER
    eval "$(cat "$CAPRES")"

    # (a) The replay TERMINATED, and promptly: the leader is TERMed at 2s,
    #     `timeout` exits at once, and the replay is a builtin read of at most
    #     8192 characters from a regular file.
    [ -n "${A_ELAPSED:-}" ]
    [ "$A_ELAPSED" -le 8 ]
    # GNU timeout reports 124 when it had to signal the command.
    [ "$A_RC" -eq 124 ]

    # (b) The byte ceiling HELD and was REPORTED, not silently applied.
    [ "$A_LEN" -le 8192 ]
    [ "$A_LEN" -ge 4096 ]
    # Truncation is REPORTED. The report channel is bounded_run's stderr (its
    # callers all run it in a command substitution, so a flag variable could
    # not reach them), and it must be emitted exactly once, for call A.
    grep -q "reached the 8192-byte capture ceiling and was TRUNCATED" "$CAPERR"
    [ "$(grep -c 'capture ceiling and was TRUNCATED' "$CAPERR")" -eq 1 ]

    # (c) The descendant really was still alive and writing stderr across the
    #     window -- otherwise (d) would prove nothing.
    [ "$ERR2" -gt "$ERR1" ]
    grep -q 'ERRFLOOD' "$CAPERR"

    # (d) The NEXT bounded_run is uncontaminated: exactly its own output, no
    #     truncation, nothing appended by the survivor.
    [ "$B" = "[CLEAN]" ]
    [ "$B_RC" -eq 0 ]
    if sed -n '/MARK-B/,$p' "$CAPERR" | grep -q 'capture ceiling and was TRUNCATED'; then
        echo "the CLEAN call was itself truncated: the survivor reached its capture" >&2
        return 1
    fi

    # (e) Every capture file was unlinked at the moment it was opened, so the
    #     survivor's inode is unreachable by path and nothing accumulates in
    #     the capture directory.
    [ "$LEFTOVER" -eq 0 ]
}

@test "bounded_run: the file-size limit is inherited by a surviving descendant and the kernel enforces it" {
    # The disk-growth half of the round-8 finding: unlinking the pathname does
    # NOT reclaim an inode a writer still holds, so the per-call file alone
    # cannot bound disk. RLIMIT_FSIZE can, and `ulimit -f N` sets the HARD
    # limit too, so a same-UID descendant cannot raise it again.
    #
    # Observable proof that the limit reached the DESCENDANT (not just the
    # leader): with the default SIGXFSZ disposition the kernel kills it at the
    # ceiling. Its stderr flood therefore stops early, long before the 400
    # iterations / ~20s it would otherwise have run for.
    write_flooder xfsz-default
    write_cap_driver 4096 '
set +e
bounded_run 2 "$FLOODSH" > /dev/null
set -e
sleep 2
ERR1=$(stat -c %s "$ERRFILE")
sleep 4
ERR2=$(stat -c %s "$ERRFILE")
echo "MARK-C" >&2
set +e
C=$(bounded_run 5 printf OK)
set -e
{
  echo "ERR1=$ERR1"
  echo "ERR2=$ERR2"
  echo "C=[$C]"
  echo "LEFTOVER=$(ls -A "$QD_CAP_DIR" | wc -l)"
} > "$RES"
'
    if ! run_cap_driver 25; then
        echo "driver did not finish" >&2
        return 1
    fi
    [ "$CAP_RC" -eq 0 ]

    local ERR1 ERR2 C LEFTOVER
    eval "$(cat "$CAPRES")"

    # It was killed at the ceiling: no further stderr between the two samples,
    # 4s apart, inside a window in which it would still have been looping.
    [ "$ERR2" -eq "$ERR1" ]
    # ...and it really did run first, so the assertion above is not vacuous.
    grep -q 'ERRFLOOD' "$CAPERR"
    [ "$C" = "[OK]" ]
    if sed -n '/MARK-C/,$p' "$CAPERR" | grep -q 'capture ceiling and was TRUNCATED'; then
        echo "the OK call was itself truncated" >&2
        return 1
    fi
    [ "$LEFTOVER" -eq 0 ]
}

# A descendant that writes at C speed rather than shell speed. `trap '' TERM`
# then `exec` preserves SIG_IGN across the exec, so the writer itself ignores
# the TERM `timeout` sends to the group, and `timeout` exits as soon as its
# direct child (the leader) does -- the promised group KILL never lands.
write_fast_flooder() {   # <megabytes> <text|nul>
    FLOODSH="$BATS_TEST_TMPDIR/flooder.sh"
    local gen="dd if=/dev/zero bs=1M count=$1 status=none"
    if [ "${2:-text}" = text ]; then
        # Printable bytes, so the capture is something bash can represent.
        # (This used to say the CHARACTER ceiling was the binding mechanism.
        # It is not, and has not been since the replay became `head -c`: the
        # bound is the BYTE ceiling. What printable bytes still buy is a
        # capture whose contents survive assignment to a shell variable, NUL
        # being the byte bash cannot store. astra, A-astra finding 6.)
        gen="$gen | tr \"\\\\0\" A"
    fi
    cat > "$FLOODSH" <<FAST_EOF
#!/usr/bin/env bash
bash -c 'trap "" TERM; $gen' &
trap 'exit 0' TERM
while :; do sleep 0.2; done
FAST_EOF
    chmod +x "$FLOODSH"
}

@test "bounded_run: a fast TERM-ignoring descendant cannot make the replay unbounded" {
    # The COST half of the round-8 finding. With one reusable capture file and
    # a `$(<file)` replay, whatever such a writer emits is written to disk and
    # then read into a shell variable in full: 64 MiB here, and nothing in the
    # design says 64 rather than 64000. With the repair the kernel stops the
    # writer at the ceiling and the replay reads at most the ceiling.
    write_fast_flooder 64 text
    write_cap_driver 8192 '
t0=$(read_mono)
set +e
A=$(bounded_run 2 "$FLOODSH")
A_RC=$?
set -e
t1=$(read_mono)
{
  echo "A_LEN=${#A}"
  echo "A_ELAPSED=$((t1 - t0))"
  echo "LEFTOVER=$(ls -A "$QD_CAP_DIR" | wc -l)"
} > "$RES"
'
    if ! run_cap_driver 30; then
        echo "driver did not finish" >&2
        return 1
    fi
    [ "$CAP_RC" -eq 0 ]
    local A_LEN A_ELAPSED LEFTOVER
    eval "$(cat "$CAPRES")"
    # At most the ceiling reaches the caller -- not 64 MiB.
    [ "$A_LEN" -le 8192 ]
    [ "$A_LEN" -ge 4096 ]
    [ "$A_ELAPSED" -le 8 ]
    [ "$LEFTOVER" -eq 0 ]
    grep -q 'capture ceiling and was TRUNCATED' "$CAPERR"
}

@test "bounded_run: a NUL-flooding descendant is bounded by the size limit, and IS reported as truncated" {
    # THE STATED LIMIT OF THE REPAIR, pinned so nobody later reads the ceiling
    # as stronger than it is:
    # bash cannot hold a NUL in a variable, so a NUL-bearing capture still
    # reaches the caller as an EMPTY string -- that half is unchanged and
    # unfixable in a shell variable. What CHANGED on 2026-09-17 is the other
    # half. This test used to assert that NO truncation was reported for such a
    # capture, because the old `read -N` ceiling counted CHARACTERS and NULs
    # did not count, so the ceiling was never seen to be reached. That was a
    # documented gap: output was silently cut off and nothing said so.
    #
    # The ceiling test now reads the capture's SIZE, which NULs do occupy, so
    # the gap is CLOSED: a NUL flood that fills the capture is reported as
    # truncated, correctly. Asserted positively below.
    #
    # vm-exec fails closed on such a body anyway: it is not valid JSON, so the
    # response validator exits 76 rather than acting on it.
    write_fast_flooder 64 nul
    write_cap_driver 8192 '
t0=$(read_mono)
set +e
A=$(bounded_run 2 "$FLOODSH")
set -e
t1=$(read_mono)
{
  echo "A_LEN=${#A}"
  echo "A_ELAPSED=$((t1 - t0))"
  echo "LEFTOVER=$(ls -A "$QD_CAP_DIR" | wc -l)"
} > "$RES"
'
    if ! run_cap_driver 30; then
        echo "driver did not finish" >&2
        return 1
    fi
    [ "$CAP_RC" -eq 0 ]
    local A_LEN A_ELAPSED LEFTOVER
    eval "$(cat "$CAPRES")"
    [ "$A_LEN" -eq 0 ]
    [ "$A_ELAPSED" -le 8 ]
    [ "$LEFTOVER" -eq 0 ]
    # The NUL flood fills the capture to the ceiling, and that IS truncation.
    if ! grep -q 'capture ceiling and was TRUNCATED' "$CAPERR"; then
        echo "a NUL flood filled the capture but no truncation was reported" >&2
        return 1
    fi
    # The REMAINING limitation must still be written down where the mechanism
    # is: the bytes are counted, but they cannot be returned in a variable.
    grep -q 'For output containing NUL bytes' "$VM_EXEC"
}

@test "bounded_run: static guards on the per-call unlinked file, the size limit and the ceiled replay" {
    # Three separate mechanisms, none of which alone closes all three failure
    # modes the round-8 review named, so all three are pinned here.
    # 1. per-call file, unlinked before the bounded command runs
    grep -q 'f="\$QD_CAP_DIR/cap.\$BASHPID.\$QD_CAP_SEQ"' "$VM_EXEC"
    # The counter must not be described as a uniqueness guarantee: bounded_run
    # always runs in a command substitution, so its increment never persists.
    grep -q 'not a uniqueness guarantee' "$VM_EXEC"
    # The unlink must happen AND be checked: an unchecked one let a failing
    # `rm` run the command anyway with the capture still NAMED (sol A2 s1).
    grep -q 'if ! rm -f "\$f" || \[ -e "\$f" \]; then' "$VM_EXEC"
    if grep -qE '^    rm -f "\$f"$' "$VM_EXEC"; then
        echo "unchecked unlink reintroduced in bounded_run" >&2
        return 1
    fi
    # The single reusable capture file must not come back.
    if grep -qE '^[^#]*QD_CAP_FILE' "$VM_EXEC"; then
        echo "QD_CAP_FILE reintroduced: one reusable capture inode is the round-8 defect" >&2
        return 1
    fi
    # 2. kernel-enforced size limit on the child and every descendant, on BOTH
    #    the capped and the explicitly-unbounded branch
    [ "$(grep -cE '^[[:space:]]*ulimit -f "\$QD_CAP_MAX_BLOCKS"' "$VM_EXEC")" -eq 2 ]
    # 3. BYTE-ceiled replay, not an unbounded `$(<file)` and not `read -N`.
    #    `read -N` counts CHARACTERS and a dropped NUL does not count, so it
    #    reads PAST the ceiling on NUL-bearing output -- 4,194,305 bytes for a
    #    nominal 64 on the fixture in qci-A-260917-sol-review.md section 1.
    grep -q 'data=$(head -c "$QD_CAP_MAX_BYTES"' "$VM_EXEC"
    if grep -qE '^[^#]*read -r? ?-N "\$QD_CAP_MAX_BYTES"' "$VM_EXEC"; then
        echo "character-counted read -N replay reintroduced (NULs escape the byte ceiling)" >&2
        return 1
    fi
    # The ceiling test must come from the capture's SIZE: not ${#data} (bash
    # drops NULs, so a character count cannot detect a NUL-filled capture), not
    # a `read -N 1` probe (which re-enters the same defect and scans to the
    # first non-NUL byte), and not a `head -c 1` probe past the replay -- when
    # `ulimit -f` cuts the file at exactly the ceiling that probe sees a clean
    # EOF and would call truncated output complete.
    grep -q 'cap_size=$(stat -Lc %s "/proc/self/fd/$rfd"' "$VM_EXEC"
    if grep -q 'printf .%s. "\$(<' "$VM_EXEC"; then
        echo "unbounded \$(<file) replay reintroduced" >&2
        return 1
    fi
    # And truncation is reported, never silent.
    grep -q 'capture ceiling and was TRUNCATED' "$VM_EXEC"
    # The ceiling is configurable with a documented default.
    grep -q 'QD_CAP_MAX_BYTES=\${QDISTRO_VM_CAPTURE_MAX_BYTES:-67108864}' "$VM_EXEC"
}

@test "vm-exec: the TIMEOUT comment no longer claims a wall-clock maximum it cannot supply" {
    # Round 7 advertised "TIMEOUT + 111s" as a wall-clock maximum while the
    # `delta > DEADLINE_JUMP_THRESHOLD => charge 1s` heuristic let ELAPSED
    # advance ~1s per real minute under sustained host pressure. The claim is
    # gone; what replaced it must still say what IS bounded.
    if grep -qE 'up to ~111s after TIMEOUT' "$VM_EXEC"; then
        echo "the withdrawn wall-clock claim is back" >&2
        return 1
    fi
    grep -q 'ELAPSED IS A DEADLINE COUNTER, NOT A WALL CLOCK' "$VM_EXEC"
    grep -q 'ONCE ELAPSED HAS REACHED TIMEOUT' "$VM_EXEC"
    grep -q 'exit 124 can be delayed by hours' "$VM_EXEC"
}

# --- Live-VM finding: the identity probe raced every short guest command ----
#
# Against a real guest (qci-gui-admin-260916-132237-233635-7446) vm-exec printed
# "could not pin guest PID N's start time" on EVERY invocation. The guest
# script, the awk parser and the qga transport were all fine; what failed was
# the RACE. qga's guest-exec is fire-and-forget, so by the time the probe's own
# guest-exec round trip lands, a short command (`echo ok` -- most of what
# vm-exec is used for) has already exited. Round 7's probe script then printed
# NOTHING and let awk's exit 2 carry the meaning, which the host could not tell
# apart from a malfunction. It then polled the SAME pid again, which qga had
# already reaped, and reported the resulting error as a probe failure.
#
# The fake-virsh suite could not catch any of this because the fake always
# answered the probe with a start time.

# Source vm-exec's STARTOF_SH/IDENT_SH definitions into THIS shell, so the
# script under test is the shipped one and not a copy.
render_ident_script() {   # <pid>
    local region="$BATS_TEST_TMPDIR/ident-region.sh"
    sed -n '/^# >>> ident-probe script/,/^# <<< ident-probe script/p' "$VM_EXEC" > "$region"
    # The extraction must have found the block, not swept up the rest of the
    # file: a terminator-based range silently over-reads when the block changes.
    grep -q '^qd_startof() {' "$region"
    # shellcheck disable=SC1090
    source "$region"
    [ -n "${IDENT_SH:-}" ]
    printf '%s' "${IDENT_SH//__PID__/$1}" > "$BATS_TEST_TMPDIR/ident.sh"
}

@test "identity probe script: prints a definite token for a LIVE pid and for a DEAD one, and exits 0 either way" {
    # THE LIVE-VM DEFECT, reproduced without a VM. Round 7's script printed the
    # start time or nothing, and exited 2 (awk's status on a missing file) when
    # the pid was gone -- so "already finished" and "the probe broke" were the
    # same observation on the host.

    # A pid that certainly exists: this very shell.
    render_ident_script "$$"
    run /bin/sh "$BATS_TEST_TMPDIR/ident.sh"
    [ "$status" -eq 0 ]
    [[ "$output" == start=* ]]
    local got=${output#start=}
    got=${got%% boot=*}
    [[ "$got" =~ ^[0-9]+$ ]]
    # ...and it carries THIS machine's boot id, which binds the identity to one
    # boot (the orphan registry depends on it).
    [ "${output##* boot=}" = "$(cat /proc/sys/kernel/random/boot_id)" ]
    # ...and it is the real value, cross-checked against field 22 directly.
    [ "$got" = "$(awk '{print $22}' "/proc/$$/stat")" ]

    # A pid that certainly does not: the max pid plus one.
    local dead
    dead=$(( $(cat /proc/sys/kernel/pid_max) + 1 ))
    [ ! -e "/proc/$dead" ]
    render_ident_script "$dead"
    run /bin/sh "$BATS_TEST_TMPDIR/ident.sh"
    # EXIT 0, not awk's 2, and a token rather than silence.
    [ "$status" -eq 0 ]
    [ "$output" = "gone" ]
}

@test "vm-exec: an ALREADY-EXITED guest command is not reported as a probe failure" {
    # The observed live case: the launched shell finished before the probe
    # reached it. There is nothing to pin and nothing an interrupt could leave
    # behind, so vm-exec must stay SILENT -- round 7 warned here on essentially
    # every call, which is how the message that matters became noise.
    PROBE_MODE=gone make_signal_virsh 987654 0
    QDISTRO_VM_KILL_VERIFY_TIMEOUT=10 term_vm_exec

    [ "$TERM_RC" -eq 143 ]
    if [[ "$TERM_OUT" == *"identity probe for guest PID"* ]]; then
        echo "a benign already-exited probe was reported as a malfunction" >&2
        echo "$TERM_OUT" >&2
        return 1
    fi
    [[ "$TERM_OUT" != *"could not pin"* ]]
    # It really did probe, so the assertion above is not vacuous.
    [ "$(wc -l < "$PROBELOG")" -ge 1 ]
    # No identity was pinned, so the submitted cleanup script carries an empty
    # stamp and the guest refuses to signal. That is the correct fail-closed
    # behaviour, and it is unchanged.
    grep -q 'stamp=\\"\\"' "$EXECLOG"
}

@test "vm-exec: a terminal probe status with NO output is a REPORTED failure, and the reaped pid is not polled again" {
    # ROUND 7's actual behaviour against a real guest, now that the fake can
    # reproduce it: terminal status, exit 2, no out-data. Two separate defects
    # are pinned here.
    #
    # 1. It must be REPORTED, and distinguishably: rc=3 means "the probe script
    #    produced nothing", not "the guest agent failed".
    # 2. Terminal status is ONE-SHOT. Round 7 read "no out-data" as "not
    #    finished yet", slept, and polled the same pid again -- which qga had
    #    already reaped, so that RPC could only fail. Exactly one probe status
    #    RPC may be issued.
    PROBE_MODE=noout make_signal_virsh 987654 3 \
        'identity-unknown pid=4242: no launch identity was captured; refusing to signal a possibly recycled pid'
    QDISTRO_VM_KILL_VERIFY_TIMEOUT=10 \
        term_vm_exec "identity probe for guest PID 4242 did not answer"

    [ "$TERM_RC" -eq 143 ]
    [[ "$TERM_OUT" == *"probe-failed(rc=3)"* ]]
    [[ "$TERM_OUT" == *"cleanup will REFUSE to signal it"* ]]
    # ONE status RPC against the probe pid. Round 7 issued a second one.
    [ "$(wc -l < "$PROBELOG")" -eq 1 ]
}

@test "vm-exec: the probe classifier distinguishes running from exited-without-output" {
    # Static guard on the mechanism the two tests above rely on: the jq
    # classifier must emit distinct tokens, not collapse both cases into "".
    grep -q '"E " + (.return\["out-data"\] // "") else "R" end' "$VM_EXEC"
    grep -q 'Terminal. Do NOT poll again' "$VM_EXEC"
    # And the four probe outcomes must be named, not folded into one warning.
    grep -q 'GUEST_IDENT_STATE=already-exited' "$VM_EXEC"
    grep -q 'GUEST_IDENT_STATE=pinned' "$VM_EXEC"
    grep -q 'GUEST_IDENT_STATE="probe-failed(rc=\$rc)"' "$VM_EXEC"
}


# --- orphan registry: a SIGKILLed vm-exec's guest command is reaped by the next
# vm-exec on the same VM (permissions-gui/13, gui-20260925T065332Z-3908053).
#
# SIGKILL runs no trap, so the signal-path cleanup above cannot help. The codex
# agent's shell tool SIGKILLs every process of a command when it returns, so a
# backgrounded vm-exec in a driver that exits early dies exactly that way, and
# its guest driver lives on: in pg/13 two such orphans were released by the
# next attempt's `touch s1-go` and sent two extra RelayMessage requests.
# Review: todo/test-blankscreenshots/reviews/blankss-pg13-code-r1-astra.md.

ODIR() { printf '%s' "$QDISTRO_VM_EXEC_STATE_DIR/fake-vm"; }

# The owner's COMPLETED record (published by rename), not the identity line:
# "guest identity pinned" is printed BEFORE registration, so a SIGKILL timed on
# it can land before the record exists (astra r1 #4).
await_record_of() {   # <host pid>
    local _tick f
    for _tick in $(seq 1 300); do
        for f in "$(ODIR)/$1"-*; do [ -f "$f" ] && return 0; done
        sleep 0.1
    done
    return 1
}

# Launch vm-exec in the background against the signal fake and wait until its
# record is published. Sets $BG_PID.
bg_registered_vm_exec() {
    local out=$1 cmd=$2
    PATH="$FAKEBIN:$PATH" "$VM_EXEC" fake-vm "$cmd" >"$out" 2>&1 &
    BG_PID=$!
    await_record_of "$BG_PID"
}

# Wait until the fake has logged a guest-exec body containing $1.
await_exec_body() {
    local _tick
    for _tick in $(seq 1 300); do
        grep -qF -- "$1" "$EXECLOG" && return 0
        sleep 0.1
    done
    return 1
}

registry_entries() {
    find "$QDISTRO_VM_EXEC_STATE_DIR" -type f ! -name '.lock' 2>/dev/null | wc -l
}

# A record for an owner that is certainly dead: pid_max+1 never exists.
plant_dead_record() {   # <name-suffix> <guest pid> <stamp> <boot> [uuid]
    local dead=$(( $(cat /proc/sys/kernel/pid_max) + 1 ))
    mkdir -p "$(ODIR)"
    printf '%s %s %s %s\n' "$2" "$3" "$4" "${5:--}" > "$(ODIR)/$dead-$1"
}

# SIGKILL a registered vm-exec. Sets nothing; leaves its record behind.
sigkill_registered() {
    bg_registered_vm_exec "$BATS_TEST_TMPDIR/out1" "${1:-first-driver}"
    kill -KILL "$BG_PID"
    wait "$BG_PID" || true
}

# Start a second vm-exec, wait until its OWN command is launched, TERM it.
run_second_until_launched() {   # <token>
    PATH="$FAKEBIN:$PATH" "$VM_EXEC" fake-vm "$1" >"$BATS_TEST_TMPDIR/out2" 2>&1 &
    local p=$!
    await_exec_body "$1"
    kill -TERM "$p"
    wait "$p" || true
}

@test "vm-exec: a SIGKILLed vm-exec's guest command is reaped by the NEXT vm-exec, before it launches its own" {
    make_signal_virsh 987654 0
    export QDISTRO_VM_KILL_VERIFY_TIMEOUT=10
    sigkill_registered
    # SIGKILL ran no code: the record is still there, and no cleanup was sent.
    [ "$(registry_entries)" -eq 1 ]
    grep -q "^4242 987654 $FAKE_BOOT " "$(ODIR)"/*-*
    run ! grep -q 'qd_kill_checked' "$EXECLOG"

    run_second_until_launched second-driver

    local kill_line cmd_line
    kill_line=$(grep -n 'qd_kill_checked' "$EXECLOG" | head -1 | cut -d: -f1)
    cmd_line=$(grep -n 'second-driver' "$EXECLOG" | head -1 | cut -d: -f1)
    [ -n "$kill_line" ] && [ -n "$cmd_line" ]
    # The cleanup carried the orphan's FULL identity, boot id included ...
    sed -n "${kill_line}p" "$EXECLOG" | grep -q 'stamp=\\"987654\\"'
    sed -n "${kill_line}p" "$EXECLOG" | grep -q "boot=\\\\\"$FAKE_BOOT\\\\\""
    # ... and was submitted BEFORE the new command, so the two never overlap.
    [ "$kill_line" -lt "$cmd_line" ]
    grep -q 'reaping orphaned guest command PID 4242' "$BATS_TEST_TMPDIR/out2"
    grep -q 'cleanup verified for guest PID 4242' "$BATS_TEST_TMPDIR/out2"
    [ "$(registry_entries)" -eq 0 ]
}

@test "vm-exec: SIGKILL right after registration is still reaped even when publishing is SLOW (window widened)" {
    # A 1s `mv` puts a wide gap between "guest identity pinned" and the published
    # record. A test synchronised on the pinned line would SIGKILL inside it.
    make_signal_virsh 987654 0
    export QDISTRO_VM_KILL_VERIFY_TIMEOUT=10
    local realmv; realmv=$(command -v mv)
    printf '#!/bin/sh\nsleep 1\nexec %s "$@"\n' "$realmv" > "$FAKEBIN/mv"
    chmod +x "$FAKEBIN/mv"
    sigkill_registered
    grep -q 'guest identity pinned' "$BATS_TEST_TMPDIR/out1"
    [ "$(registry_entries)" -eq 1 ]
    rm -f "$FAKEBIN/mv"
    run_second_until_launched second-driver
    grep -q 'reaping orphaned guest command PID 4242' "$BATS_TEST_TMPDIR/out2"
    [ "$(registry_entries)" -eq 0 ]
}

@test "vm-exec: a LIVE vm-exec's guest command is never reaped by a concurrent one" {
    make_signal_virsh 987654 0
    export QDISTRO_VM_KILL_VERIFY_TIMEOUT=10
    bg_registered_vm_exec "$BATS_TEST_TMPDIR/out1" 'first-driver'
    local first=$BG_PID
    bg_registered_vm_exec "$BATS_TEST_TMPDIR/out2" 'second-driver'
    local second=$BG_PID
    run ! grep -q 'qd_kill_checked' "$EXECLOG"
    run ! grep -q 'reaping orphaned' "$BATS_TEST_TMPDIR/out2"
    [ "$(registry_entries)" -eq 2 ]
    kill -TERM "$first" "$second"
    wait "$first" || true
    wait "$second" || true
}

@test "vm-exec: an entry whose owner PID was RECYCLED is still reaped" {
    # The owner check is (pid, start time), not a bare pid.
    make_signal_virsh 987654 0
    export QDISTRO_VM_KILL_VERIFY_TIMEOUT=10
    mkdir -p "$(ODIR)"
    printf '4242 987654 %s -\n' "$FAKE_BOOT" > "$(ODIR)/$$-1"
    run_second_until_launched second-driver
    grep -q 'reaping orphaned guest command PID 4242' "$BATS_TEST_TMPDIR/out2"
    [ "$(registry_entries)" -eq 0 ]
}

@test "vm-exec: an owner whose /proc entry cannot be READ is not treated as dead" {
    # Indeterminate is not absence (astra r1): a failed read of a live owner's
    # stat must never let its command be reaped.
    make_signal_virsh 987654 0
    export QDISTRO_VM_KILL_VERIFY_TIMEOUT=10
    mkdir -p "$(ODIR)"
    printf '4242 987654 %s -\n' "$FAKE_BOOT" > "$(ODIR)/$$-1"
    local realcat; realcat=$(command -v cat)
    printf '#!/bin/sh\n[ "$1" = "/proc/%s/stat" ] && exit 1\nexec %s "$@"\n' "$$" "$realcat" > "$FAKEBIN/cat"
    chmod +x "$FAKEBIN/cat"
    run_second_until_launched second-driver
    rm -f "$FAKEBIN/cat"
    grep -q "cannot read host pid $$" "$BATS_TEST_TMPDIR/out2"
    run ! grep -q 'qd_kill_checked' <(head -1 "$EXECLOG")
    run ! grep -q 'reaping orphaned' "$BATS_TEST_TMPDIR/out2"
    [ -f "$(ODIR)/$$-1" ]
}

@test "vm-exec: normal completion and a VERIFIED signal cleanup both retract the record" {
    make_virsh '{"return":{"exited":true,"exitcode":0}}'
    PATH="$FAKEBIN:$PATH" run timeout 60 "$VM_EXEC" fake-vm true
    [ "$status" -eq 0 ]
    [[ "$output" == *"guest identity pinned"* ]]
    [[ "$output" != *"orphan registry"* ]]     # it DID register, silently
    [ "$(registry_entries)" -eq 0 ]

    make_signal_virsh 987654 0
    QDISTRO_VM_KILL_VERIFY_TIMEOUT=10 term_vm_exec "guest identity pinned"
    [ "$TERM_RC" -eq 143 ]
    [ "$(registry_entries)" -eq 0 ]
}

@test "vm-exec: an UNVERIFIED signal cleanup and a poll-error exit KEEP the record" {
    # EXIT must not retract a record while the guest command may still run.
    make_signal_virsh 987654 5 'still-alive pid=4242 after TERM+KILL'
    export QDISTRO_VM_KILL_VERIFY_TIMEOUT=10
    bg_registered_vm_exec "$BATS_TEST_TMPDIR/out1" 'first-driver'
    kill -TERM "$BG_PID"; wait "$BG_PID" || true
    grep -q 'could NOT be verified' "$BATS_TEST_TMPDIR/out1"
    [ "$(registry_entries)" -eq 1 ]

    rm -rf "$QDISTRO_VM_EXEC_STATE_DIR"
    make_signal_virsh 987654 0
    bg_registered_vm_exec "$BATS_TEST_TMPDIR/out3" 'poll-error-driver'
    touch "$BATS_TEST_TMPDIR/statuserr"
    local rc=0
    wait "$BG_PID" || rc=$?
    rm -f "$BATS_TEST_TMPDIR/statuserr"
    [ "$rc" -eq 1 ]
    [ "$(registry_entries)" -eq 1 ]
}

@test "vm-exec: a FAILED cleanup keeps the record and REFUSES the launch; recovery then succeeds" {
    make_signal_virsh 987654 0
    export QDISTRO_VM_KILL_VERIFY_TIMEOUT=10
    sigkill_registered
    touch "$BATS_TEST_TMPDIR/killfail"
    PATH="$FAKEBIN:$PATH" run timeout 60 "$VM_EXEC" fake-vm 'refused-driver'
    [ "$status" -eq 75 ]
    [[ "$output" == *"refusing to launch on fake-vm"* ]]
    [[ "$output" == *"could not be confirmed gone"* ]]
    # Nothing was started, and the orphan is still on file.
    run ! grep -q 'refused-driver' "$EXECLOG"
    [ "$(registry_entries)" -eq 1 ]

    # The agent recovers: the retry reaps it and launches.
    rm -f "$BATS_TEST_TMPDIR/killfail"
    run_second_until_launched retry-driver
    grep -q 'cleanup verified for guest PID 4242' "$BATS_TEST_TMPDIR/out2"
    [ "$(registry_entries)" -eq 0 ]
}

@test "vm-exec: a caller that cannot get the reaper lock in time REFUSES rather than launching" {
    # Two stale records whose cleanups never finish hold the lock for ~2x the
    # verify budget; a caller with a short lock wait must not slip past.
    make_signal_virsh 987654 hang
    export QDISTRO_VM_KILL_VERIFY_TIMEOUT=3
    plant_dead_record a 4242 987654 "$FAKE_BOOT"
    plant_dead_record b 4242 987654 "$FAKE_BOOT"
    PATH="$FAKEBIN:$PATH" "$VM_EXEC" fake-vm 'holder-driver' >"$BATS_TEST_TMPDIR/holder" 2>&1 &
    local holder=$!
    await_exec_body qd_kill_checked
    local rc=0
    PATH="$FAKEBIN:$PATH" QDISTRO_VM_EXEC_REAP_LOCK_WAIT=1 \
        timeout 60 "$VM_EXEC" fake-vm 'waiting-driver' >"$BATS_TEST_TMPDIR/waiter" 2>&1 || rc=$?
    [ "$rc" -eq 75 ]
    grep -q 'held the orphan registry lock' "$BATS_TEST_TMPDIR/waiter"
    run ! grep -q 'waiting-driver' "$EXECLOG"
    # The holder could not confirm either orphan gone: it refuses too, keeps both.
    rc=0; wait "$holder" || rc=$?
    [ "$rc" -eq 75 ]
    run ! grep -q 'holder-driver' "$EXECLOG"
    [ "$(registry_entries)" -eq 2 ]
}

@test "vm-exec: a record from an EARLIER BOOT is resolved without a kill, via the in-guest boot check" {
    # Same VM name, new boot: the guest reports boot-mismatch and signals nothing.
    make_signal_virsh 987654 4 'boot-mismatch pid=4242 boot=x expected=y: the recorded process belonged to an earlier boot; NOT signalled'
    export QDISTRO_VM_KILL_VERIFY_TIMEOUT=10
    plant_dead_record a 4242 987654 99999999-8888-7777-6666-555555555555
    run_second_until_launched second-driver
    # The submitted script carried the RECORDED boot id for the guest to check.
    grep -q 'boot=\\"99999999-8888-7777-6666-555555555555\\"' "$EXECLOG"
    grep -q 'belongs to an earlier boot' "$BATS_TEST_TMPDIR/out2"
    [ "$(registry_entries)" -eq 0 ]
}

@test "vm-exec: a record from a RE-CREATED domain (uuid differs) is dropped with no RPC" {
    make_signal_virsh 987654 0
    export QDISTRO_VM_KILL_VERIFY_TIMEOUT=10
    echo aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee > "$BATS_TEST_TMPDIR/domuuid"
    plant_dead_record a 4242 987654 "$FAKE_BOOT" 12345678-1234-1234-1234-123456789abc
    run_second_until_launched second-driver
    grep -q 'belongs to an earlier domain' "$BATS_TEST_TMPDIR/out2"
    run ! grep -q 'qd_kill_checked' <(sed -n '1,/second-driver/p' "$EXECLOG")
    [ "$(registry_entries)" -eq 0 ]
    # And the new command's own record carries the current uuid.
}

@test "vm-exec: registry failures are REPORTED, never fatal to the command" {
    make_signal_virsh 987654 0
    export QDISTRO_VM_KILL_VERIFY_TIMEOUT=10
    # 1. Unwritable state root: the command runs, and says it is unprotected.
    mkdir -p "$QDISTRO_VM_EXEC_STATE_DIR"; chmod 555 "$QDISTRO_VM_EXEC_STATE_DIR"
    run_second_until_launched unprotected-driver
    chmod 755 "$QDISTRO_VM_EXEC_STATE_DIR"
    grep -q 'orphan registry: cannot create' "$BATS_TEST_TMPDIR/out2"

    # 2. A resolved record that cannot be removed (writable .lock, read-only
    #    directory) is reported and does NOT abort the requested command.
    rm -rf "$QDISTRO_VM_EXEC_STATE_DIR"
    plant_dead_record a 4242 987654 "$FAKE_BOOT"
    : > "$(ODIR)/.lock"
    chmod 555 "$(ODIR)"
    run_second_until_launched readonly-driver
    chmod 755 "$(ODIR)"
    grep -q 'cleanup verified for guest PID 4242' "$BATS_TEST_TMPDIR/out2"
    grep -q 'could not be removed' "$BATS_TEST_TMPDIR/out2"
    grep -q 'readonly-driver' "$EXECLOG"
}

@test "kill script: a matching (pid, start-time) from an EARLIER BOOT is NOT signalled" {
    # The same-name/new-boot regression against the REAL script and a REAL live
    # process whose pid and start time match the record exactly.
    sleep 60 &
    local victim=$!
    local stamp; stamp=$(host_startof "$victim")
    render_kill_script "$victim" "$stamp" 00000000-0000-0000-0000-000000000000
    run sh "$BATS_TEST_TMPDIR/kill.sh"
    [ "$status" -eq 4 ]
    [[ "$output" == *"boot-mismatch"* ]]
    kill -0 "$victim"
    # Same record with THIS boot's id is signalled.
    render_kill_script "$victim" "$stamp" "$(cat /proc/sys/kernel/random/boot_id)"
    run sh "$BATS_TEST_TMPDIR/kill.sh"
    [ "$status" -eq 0 ]
    [[ "$output" == *"identity-verified"* ]]
    run ! kill -0 "$victim"
}
