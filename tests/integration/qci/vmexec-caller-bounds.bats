#!/usr/bin/env bats
#
# Host-only regressions for the CALLER side of scripts/vm/vm-exec.
#
# THE DEFECT CLASS. `x=$(vm-exec ... 2>&1)` and `vm-exec ... 2>&1 | reader`
# both hand vm-exec's fd 2 to a PIPE. vm-exec redirects its own children's
# fd 1 to an internal, unlinked capture file, but fd 2 is inherited straight
# through to every virsh/jq descendant it starts. A descendant that outlives
# vm-exec and keeps that descriptor holds the pipe open, and the shell waits
# for the PIPE to reach EOF -- not for vm-exec to exit. The call then hangs
# after the guest command is long dead, and an outer `timeout` on vm-exec does
# not help, because the shell is blocked on a read rather than on the child.
#
# Rounds 8 and 9 both found live instances of this AFTER it had been declared
# closed. These tests pin the two properties the round-9 review asked for by
# name, plus a scanner that fails on a NEW, DIRECTLY SPELLED instance in either repo --
# so the next instance IN TRACKED SOURCE is caught by CI rather than by a reviewer (the agent's run-time drivers under ci/runs are not scanned and cannot be -- see the scanner docstring).
#
# No VM is required: a fake vm-exec on a fake VM_TOOLS drives the helpers.

bats_require_minimum_version 1.5.0

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    GUI_SH="$REPO_ROOT/ci/lib/gates/gui.sh"
    FAKE_TOOLS="$BATS_TEST_TMPDIR/tools"
    mkdir -p "$FAKE_TOOLS"
    LOGFILE="$BATS_TEST_TMPDIR/log.txt"
    : > "$LOGFILE"
}

teardown() {
    # The leak test deliberately starts a detached writer; do not leave it
    # running past the test that created it.
    [ -f "$BATS_TEST_TMPDIR/leaker.pid" ] &&
        kill -KILL "$(cat "$BATS_TEST_TMPDIR/leaker.pid")" 2>/dev/null
    return 0
}

# Extract await_vmexec_success (and the two knobs declared with it) from gui.sh
# without sourcing the whole gate, then run <code> against it with a fake
# vm-exec. `log` is stubbed to append to $LOGFILE so the TIMEOUT line is
# inspectable. Prints the driver's stdout; sets $status via `run`.
drive_await() {
    local code=$1
    {
        echo 'log(){ printf "%s\n" "$*" >> "'"$LOGFILE"'"; }'
        echo 'VM_TOOLS="'"$FAKE_TOOLS"'"'
        sed -n '/^# Host-side waiter: retry a guest command over vm-exec/,/^}$/p' "$GUI_SH"
        echo "$code"
    } > "$BATS_TEST_TMPDIR/drive.sh"
    bash "$BATS_TEST_TMPDIR/drive.sh"
}

# --------------------------------------------------------------------------
# 1. A surviving fd-2 writer must not contaminate a LATER attempt's capture.
# --------------------------------------------------------------------------

@test "await_vmexec_success: a descendant that outlives attempt 1 cannot write into a later attempt's capture" {
    # The fake vm-exec stands in for vm-exec + its virsh/jq descendants. On the
    # FIRST attempt it forks a writer that keeps the inherited fd 2 and writes
    # LEAK half a second later -- i.e. while attempt TWO is running.
    #
    # THE FIXTURE IS TUNED TO BE OBSERVABLE, and that tuning is the point.
    # `>"$cf" 2>&1` gives fd 1 and fd 2 one shared open description, so after
    # attempt 1 prints "ATTEMPT1\n" the survivor's offset is 9. The old shape
    # truncated the SAME inode and reopened it at offset 0, so attempt 2's own
    # "ATTEMPT2\n" also ended at 9 and the survivor's LEAK landed immediately
    # after it -- contiguous, no NUL hole, and therefore visible in the logged
    # text. (With more attempts, or a writer that keeps going, the survivor's
    # offset runs ahead and the contamination sits behind a sparse NUL hole
    # that bash's `read` silently stops at, so the SAME defect would be
    # invisible to this assertion. A two-attempt fixture is what makes this a
    # real differential rather than a test that passes either way; verified by
    # running it against the pre-fix body, where it fails.)
    #
    # Under the per-call open-then-unlink shape attempt 2 gets a BRAND-NEW
    # inode the survivor cannot reach.
    cat > "$FAKE_TOOLS/vm-exec" <<EOF
#!/bin/bash
n=\$(cat "$BATS_TEST_TMPDIR/n" 2>/dev/null || echo 0)
n=\$((n + 1)); printf '%s' "\$n" > "$BATS_TEST_TMPDIR/n"
echo "ATTEMPT\$n"
if [ "\$n" = 1 ]; then
    # Detached, keeps fd 2 (attempt 1's capture), fires during attempt 2, and
    # RECORDS the write so the assertions below cannot pass vacuously.
    setsid bash -c '
        sleep 0.5
        echo "LEAK" >&2
        echo w >> "$BATS_TEST_TMPDIR/witness"' >/dev/null &
    printf '%s' "\$!" > "$BATS_TEST_TMPDIR/leaker.pid"
else
    sleep 2
fi
exit 1
EOF
    chmod +x "$FAKE_TOOLS/vm-exec"

    run drive_await 'await_vmexec_success testvm 2 0 readiness-probe; echo "rc=$?"'
    [ "$status" -eq 0 ]
    grep -qx 'rc=1' <<<"$output"   # rc=127 satisfied the substring match

    # NON-VACUITY: exactly the shape described above actually happened -- a
    # second attempt ran, and the survivor really did write while it was
    # running. Without these the "no LEAK" assertion could pass because
    # nothing was ever written.
    [ "$(cat "$BATS_TEST_TMPDIR/n")" -eq 2 ]
    [ -s "$BATS_TEST_TMPDIR/witness" ]

    # THE PROPERTY: the capture the TIMEOUT line reports is attempt 2's own
    # output, uncontaminated by attempt 1's survivor.
    run cat "$LOGFILE"
    [[ "$output" == *"TIMEOUT"* ]]
    [[ "$output" == *"ATTEMPT2"* ]]
    if [[ "$output" == *"LEAK"* ]]; then
        echo "attempt-1 survivor's bytes appeared in attempt 2's capture:" >&2
        echo "$output" >&2
        return 1
    fi
}

# --------------------------------------------------------------------------
# 2. The readiness deadline must be ENFORCED, not checked after the fact.
# --------------------------------------------------------------------------

@test "await_vmexec_success: a LATE success is not accepted (round 9's 2s-success/1s-budget probe)" {
    # Round 9: a fake vm-exec that sleeps 2s then exits 0, called as
    # `await_vmexec_success testvm 1 1 true`, RETURNED 0 after 2.01 seconds on
    # a 1-second budget -- because the old loop invoked vm-exec and only
    # compared elapsed time once it returned. The invocation is now capped by
    # the REMAINING budget, so the late success is killed and reported as a
    # timeout instead of being returned as a pass.
    printf '#!/bin/bash\nsleep 2\nexit 0\n' > "$FAKE_TOOLS/vm-exec"
    chmod +x "$FAKE_TOOLS/vm-exec"

    run drive_await 's=$SECONDS; await_vmexec_success testvm 1 1 true; echo "rc=$? elapsed=$((SECONDS-s))"'
    [ "$status" -eq 0 ]
    [[ "$output" == *"rc=1 "* ]]
    local elapsed=${output##*elapsed=}
    # Budget 1s + the SIGKILL grace. Generous, but strictly less than the 2s
    # the command wanted: a helper that waited for the command would land at 2+.
    [ "$elapsed" -le 1 ]
}

@test "await_vmexec_success: a NEVER-returning readiness command still returns within the budget" {
    # The stronger case the late-success probe only approximates: a command
    # that never returns at all. The old loop never reached its elapsed check,
    # so it waited forever. `timeout -k` on each invocation bounds it.
    printf '#!/bin/bash\nexec sleep 3600\n' > "$FAKE_TOOLS/vm-exec"
    chmod +x "$FAKE_TOOLS/vm-exec"

    run drive_await 's=$SECONDS; await_vmexec_success testvm 2 1 true; echo "rc=$? elapsed=$((SECONDS-s))"'
    [ "$status" -eq 0 ]
    [[ "$output" == *"rc=1 "* ]]
    local elapsed=${output##*elapsed=}
    # 2s budget + the 5s default kill grace, with slack for a loaded host.
    [ "$elapsed" -le 12 ]
}

@test "await_vmexec_success: a command that succeeds inside the budget still returns 0" {
    # Guard against closing the hole by simply always failing.
    printf '#!/bin/bash\nexit 0\n' > "$FAKE_TOOLS/vm-exec"
    chmod +x "$FAKE_TOOLS/vm-exec"

    run drive_await 'await_vmexec_success testvm 5 1 true; echo "rc=$?"'
    [ "$status" -eq 0 ]
    [[ "$output" == *"rc=0"* ]]
}

@test "await_vmexec_success: no capture file is left NAMED while the command runs" {
    # The file is opened and unlinked BEFORE the command starts, so a running
    # command must see zero qci-await.* names in TMPDIR. (The old shape left
    # one named for the whole attempt.)
    local probe="$BATS_TEST_TMPDIR/seen"
    cat > "$FAKE_TOOLS/vm-exec" <<EOF
#!/bin/bash
ls "$BATS_TEST_TMPDIR/tmp" > "$probe" 2>/dev/null
exit 0
EOF
    chmod +x "$FAKE_TOOLS/vm-exec"
    mkdir -p "$BATS_TEST_TMPDIR/tmp"
    run drive_await 'TMPDIR='"$BATS_TEST_TMPDIR"'/tmp await_vmexec_success testvm 5 1 true'
    [ "$status" -eq 0 ]
    run grep -c 'qci-await' "$probe"
    [ "$output" = "0" ]
}

# --------------------------------------------------------------------------
# 3. vm-exec itself: the file-size bound fails CLOSED.
# --------------------------------------------------------------------------

@test "vm-exec: an unappliable file-size limit is a refusal, not a warning" {
    # Round 9 item: a WARN on the one path whose entire purpose is to supply a
    # bound is not a bound. STATIC guard, and it says so: the refusing branch is
    # reachable only on a host that denies `ulimit -f` outright while reporting
    # an unlimited (or higher) hard limit, which cannot be arranged from a test
    # on a normal Linux host -- when the inherited hard limit is LOWER, the
    # preceding branch accepts it and nothing is refused. So this pins the code
    # shape, not an observed refusal.
    local vmx="$REPO_ROOT/scripts/vm/vm-exec"
    grep -q 'ERROR: cannot apply a file-size limit' "$vmx"
    # The refusal must actually exit, not just print.
    grep -A2 'ERROR: cannot apply a file-size limit' "$vmx" | grep -q 'exit 1'
    # And the only way past it is the documented, explicit opt-out.
    grep -q 'QDISTRO_VM_ALLOW_UNBOUNDED_CAPTURE' "$vmx"
}

@test "vm-exec: an inherited hard limit BELOW the ceiling is accepted, not refused" {
    # The other side of the branch above, and this one IS observable: a tighter
    # inherited limit still binds, so vm-exec must run. Without the second
    # branch the fail-closed change would break every host with a restrictive
    # ulimit. `-i` is used so the usage error (not a limit refusal) is what we
    # read back.
    run bash -c 'ulimit -f 64 2>/dev/null || exit 111
                 QDISTRO_VM_CAPTURE_MAX_BYTES=1048576 "$1" 2>&1' _ "$REPO_ROOT/scripts/vm/vm-exec"
    [ "$status" -ne 111 ]
    if [[ "$output" == *"cannot apply a file-size limit"* ]]; then
        echo "a tighter inherited hard limit was wrongly refused:" >&2
        echo "$output" >&2
        return 1
    fi
}

# --------------------------------------------------------------------------
# 4. The scanner: no NEW directly spelled instance of the defect in either repo.
#    It cannot see fd 2 inherited from an enclosing context (wrappers, bats'
#    own `run`, `exec 3> >(...)`); it is a regression lint, not a proof.
# --------------------------------------------------------------------------

@test "caller audit: no host-side 2>&1 hands vm-exec's fd 2 to a pipe" {
    # WHAT THIS SEARCHES FOR, stated so the next reviewer can judge it rather
    # than trust it. Joining `\`-continuations first (round 9's misses were all
    # hidden by one), it finds every logical line in qdistro/ and, when present,
    # the sibling qdwin/ that
    #   (a) mentions a vm-exec invocation in any spelling -- /vm.?exec/i covers
    #       vm-exec, $VMEXEC, $VM_EXEC, $QDWIN_VM_EXEC, vm_exec(); and
    #   (b) contains a `2>&1` that is NOT preceded, on the same command, by a
    #       `>`/`>>` redirect of fd 1 (so `>file 2>&1` and `>/dev/null 2>&1`,
    #       which put fd 1 on a FILE, are not flagged).
    # Each hit is then reported unless it is a KNOWN-SAFE guest-side case, i.e.
    # the `2>&1` sits inside the quoted remote-command string and therefore runs
    # in the guest with no host descriptor at all.
    #
    # WHAT IT DOES NOT COVER, and why the audit is not "complete" on its
    # strength alone: it does not see a vm-exec call whose fd 2 is a pipe
    # INHERITED from an enclosing context (a wrapper function called inside
    # `$( ... 2>&1 )`, or bats' own `run`, which merges with `"$@" 2>&1` inside
    # a command substitution). Those are enumerated in the round-9 report, not
    # by this test.
    run python3 "$REPO_ROOT/ci/bin/vmexec-fd2-scan.py" \
        "$REPO_ROOT"
    if [ "$status" -ne 0 ]; then
        echo "$output" >&2
        return 1
    fi
}

@test "caller audit: the scanner's UNPARSEABLE files are exactly the three audited by hand" {
    # The scanner prints `UNPARSEABLE: <path>` for a file whose quoting it
    # could not follow to the end -- its `2>&1` sites were NOT checked, so a
    # clean exit does not cover it. Those files must be enumerated, not
    # ignored. All three below were read by hand during the round-9 audit and
    # carry no host-side hazard:
    #   * scripts/vm/build-baked-baseweed.sh -- defeats the scanner with
    #     `` `# comment` `` continuation markers; it only MENTIONS vm-exec in
    #     comments and every `2>&1` there follows a `>` to a file or /dev/null.
    #   * tests/integration/permissions-gui/AGENTS.md -- prose plus heredocs
    #     opened inside `$( )`; its single `2>&1` is in a prose sentence.
    #   * tests/integration/workflow-gui/05-approval-queue-gated-run.md -- same
    #     heredoc-inside-`$( )` shape; its single `2>&1` is inside a heredoc
    #     body that runs in the GUEST.
    # A NEW blind spot fails this test rather than silently exempting a file.
    run --separate-stderr python3 "$REPO_ROOT/ci/bin/vmexec-fd2-scan.py" \
        "$REPO_ROOT"
    local expected="UNPARSEABLE: scripts/vm/build-baked-baseweed.sh
UNPARSEABLE: tests/integration/permissions-gui/AGENTS.md
UNPARSEABLE: tests/integration/workflow-gui/05-approval-queue-gated-run.md"
    if [ "$stderr" != "$expected" ]; then
        echo "the scanner's blind spots changed." >&2
        echo "expected:" >&2; echo "$expected" >&2
        echo "got:" >&2; echo "$stderr" >&2
        return 1
    fi
}

@test "caller audit: the scanner is not vacuous -- it still catches the round-9 shapes" {
    # A scanner that reports nothing is worthless unless it can be shown to
    # report something. Four fixtures, one per shape round 8/9 actually missed.
    local d="$BATS_TEST_TMPDIR/fixtures"
    mkdir -p "$d"
    # 1. command substitution with 2>&1
    printf 'out=$("$VMEXEC" "$VM" "cmd" 2>&1)\n' > "$d/a.sh"
    # 2. pipeline with 2>&1 (hidden behind a line continuation, as in round 9)
    printf '"$QDWIN_VM_EXEC" "$VMNAME" "cmd" \\\n    2>&1 | tee /x.log\n' > "$d/b.sh"
    # 3. the 2>&1 on a later line of a MULTI-LINE guest string's call
    printf 'x=$("$QDWIN_VM_EXEC" "$VMNAME" "\nline one\nline two\n" 2>&1) || :\n' > "$d/c.sh"
    # 4. the `$vmx` spelling the gates use
    printf 'o=$("$vmx" "$vm" "ping -c 1 1.2.3.4" 2>&1)\n' > "$d/d.sh"
    for f in a b c d; do
        run python3 "$REPO_ROOT/ci/bin/vmexec-fd2-scan.py" "$d"
        [ "$status" -eq 1 ]
        [[ "$output" == *"$f.sh"* ]] || { echo "fixture $f.sh not detected" >&2; \
            echo "$output" >&2; return 1; }
    done
    # And a guest-side 2>&1, which must NOT be reported.
    rm -f "$d"/*.sh
    printf '"$VMEXEC" "$VM" "prog 2>&1 | head -1" >/dev/null 2>&1\n' > "$d/e.sh"
    run python3 "$REPO_ROOT/ci/bin/vmexec-fd2-scan.py" "$d"
    [ "$status" -eq 0 ]
}

# --------------------------------------------------------------------------
# 5. The bats VM lane's own chokepoint: vm_run().
# --------------------------------------------------------------------------
#
# bats-core's `run` merges streams with `"$@" 2>&1` inside a command
# substitution (lib/bats-core/test_functions.bash, 1.14), so `run vm-exec ...`
# IS this defect. vm_run() is the chokepoint most VM tests go through; these
# pin that it captures through a file while keeping $status/$output semantics.
# Host-only: a fake VM_EXEC stands in for the real one and no VM is touched.

_load_vm_helpers() {
    VM_NAME=fake-vm
    VM_EXEC="$FAKE_TOOLS/vm-exec"
    unset VM_SSH_PORT
    # shellcheck source=/dev/null
    source "$REPO_ROOT/tests/integration/vm/helpers.bash"
}

@test "vm_run: merged stdout+stderr and the guest exit status survive the file capture" {
    printf '#!/bin/bash\necho out-line\necho err-line >&2\nexit 7\n' \
        > "$FAKE_TOOLS/vm-exec"
    chmod +x "$FAKE_TOOLS/vm-exec"
    _load_vm_helpers
    vm_run 'anything'
    [ "$status" -eq 7 ]
    [[ "$output" == *"out-line"* ]]
    [[ "$output" == *"err-line"* ]]
}

@test "vm_run: a descendant that keeps fd 2 does not hold the call open" {
    # The whole point: the fake leaves a detached writer holding the
    # descriptors it was given. Under `run "$VM_EXEC" ...` that writer holds
    # bats' substitution pipe and vm_run never returns. Here it holds an
    # unlinked regular file, so the call returns at once.
    cat > "$FAKE_TOOLS/vm-exec" <<EOF
#!/bin/bash
echo real-output
setsid bash -c 'for i in \$(seq 1 100); do echo LEAK >&2; sleep 0.05; done' &
printf '%s' "\$!" > "$BATS_TEST_TMPDIR/leaker.pid"
exit 0
EOF
    chmod +x "$FAKE_TOOLS/vm-exec"
    _load_vm_helpers
    local s=$SECONDS
    vm_run 'anything'
    local elapsed=$((SECONDS - s))
    [ "$status" -eq 0 ]
    [[ "$output" == *"real-output"* ]]
    # It returned promptly rather than waiting for the 5-second writer.
    [ "$elapsed" -le 2 ]
}

# A MERGED capture carries vm-exec's own stderr, and the screenshot reply is
# parsed by matching the WHOLE captured string against a grammar. Those two
# facts together mean any transport line vm-exec prints makes a perfectly good
# capture unparseable and fails the scenario for a non-product reason. It is
# not hypothetical: adding a success-path "guest identity pinned" line to
# vm-exec rejected both attempts of a valid reply and produced rc=1 (sol,
# todo/reviews/qci-A3-260917-sol-review.md finding 1). vm-exec's periodic
# "[vm-exec] Waiting..." lines are the same hazard on any slow capture.
@test "caller audit: the screenshot reply parser survives vm-exec transport chatter" {
    local helpers="$REPO_ROOT/qdwin/tests/gui/qdwin-helpers.sh"
    [ -f "$helpers" ] || skip "qdwin component not present"

    # RUN THE PRODUCTION FUNCTION. An earlier version of this test greped the
    # source and then re-implemented the filter locally; sol INVERTED the real
    # filter (`grep -v` -> `grep`, so it kept the chatter and threw away the
    # reply) and this test still passed. A copy of the logic tests the copy.
    local d="$BATS_TEST_TMPDIR/shot"; mkdir -p "$d/bin"
    # Fake transport: one transport diagnostic line, then a valid reply.
    cat > "$d/bin/vm-exec" <<'SH'
#!/bin/bash
case "$*" in
    *capture\ Virtual-1*)
        echo "[vm-exec] guest identity pinned (pid 4242 start 987654)" >&2
        # the guest path is the token after "capture Virtual-1", up to the
        # literal \n the driver appends inside its printf format
        g=$(sed -n 's/.*capture Virtual-1 \([^ ]*\).*/\1/p' <<<"$*")
        g=${g%%\\n*}
        printf 'ok output=Virtual-1 width=800 height=600 path=%s\n' "$g"
        ;;
    *) echo UNEXPECTED >&2; exit 1 ;;
esac
SH
    chmod +x "$d/bin/vm-exec"

    # Source the real helpers with the dependencies past the parse stubbed out,
    # so the function reaches -- and must get past -- the reply grammar.
    cat > "$d/drv.sh" <<SH
QDWIN_VM_EXEC="$d/bin/vm-exec"
VMNAME=fake-vm
QDWIN_SKIP_HELPER_MAIN=1
source "$helpers" 2>/dev/null || true
qdwin_require_vm() { :; }
qdwin_session_healthy() { :; }
qdwin_recover_and_verify() { return 1; }
qdwin_capture_fail_cleanup() { :; }
qdwin_compositor_pid() { echo 1234; }
qdwin_screenshot "$d/out.png"
echo "rc=\$?"
SH
    run bash "$d/drv.sh"
    # The reply must have PARSED. The fake transport answers ONLY the capture
    # request, so a parsed reply advances to the copy step and dies there with
    # `shell-capture-copy-failed`; an unparsed one dies at the grammar with
    # `capture attempt 1 failed`. The two are mutually exclusive and name the
    # stage reached, which is exactly what this test is about.
    [[ "$output" == *"shell-capture-copy-failed"* ]] || {
        echo "did not reach the copy step; the transport line defeated the grammar" >&2
        echo "$output" >&2
        return 1
    }
    [[ "$output" != *"capture attempt 1 failed"* ]] || {
        echo "the grammar rejected a valid reply that carried a transport line" >&2
        echo "$output" >&2
        return 1
    }
}

# The other half of the same fix: filtering the transport lines OUT of the
# protocol reply must not DESTROY them. They were captured into $reply_diag
# and then never read, so a transport that reported a specific failure
# surfaced to the operator as "capture command timed out" (sol,
# qci-A4-260917-sol-review.md finding 2).
@test "caller audit: a failed capture still reports what the transport said" {
    local helpers="$REPO_ROOT/qdwin/tests/gui/qdwin-helpers.sh"
    [ -f "$helpers" ] || skip "qdwin component not present"

    local d="$BATS_TEST_TMPDIR/shot2"; mkdir -p "$d/bin"
    # Transport fails and says why; there is no protocol reply at all.
    cat > "$d/bin/vm-exec" <<'SH'
#!/bin/bash
echo "[vm-exec] ERROR: TRANSPORT-FAILURE-DETAIL" >&2
exit 1
SH
    chmod +x "$d/bin/vm-exec"
    cat > "$d/drv.sh" <<SH
QDWIN_VM_EXEC="$d/bin/vm-exec"
VMNAME=fake-vm
source "$helpers" 2>/dev/null || true
qdwin_require_vm() { :; }
qdwin_session_healthy() { :; }
qdwin_recover_and_verify() { return 1; }
qdwin_capture_fail_cleanup() { :; }
qdwin_compositor_pid() { echo 1234; }
qdwin_screenshot "$d/out.png"
echo "rc=\$?"
SH
    run bash "$d/drv.sh"
    [[ "$output" == *"TRANSPORT-FAILURE-DETAIL"* ]] || {
        echo "the transport's own explanation was discarded; the operator sees only a generic message" >&2
        echo "$output" >&2
        return 1
    }
}

# A FAILED replay is not empty output. Both sites below were `... || :` and
# returned the PRODUCER's success with an empty string, so an I/O error on the
# capture was indistinguishable from a command that printed nothing (sol,
# qci-A3 finding 3 and qci-A4 finding 1 -- I reported the first of them fixed
# one round BEFORE it was). Kept as tests rather than as review artifacts so
# the next `|| :` here is caught by CI instead of by a reviewer.
@test "bounded_run: a failed replay is a capture failure, not empty output" {
    local d="$BATS_TEST_TMPDIR/br"; mkdir -p "$d/caps"
    # extract the production function verbatim
    sed -n '/^bounded_run() {/,/^}/p' "$REPO_ROOT/scripts/vm/vm-exec" > "$d/br.sh"
    grep -q 'head -c' "$d/br.sh"
    local env="QD_CAP_DIR=$d/caps QD_CAP_SEQ=0 QD_CAP_MAX_BLOCKS=1024 QD_CAP_MAX_BYTES=65536 SIGKILL_GRACE=5"

    run bash -c "$env; . '$d/br.sh'; out=\$(bounded_run 5 printf OK); echo \"rc=\$? out=[\$out]\""
    [[ "$output" == *"rc=0 out=[OK]"* ]]

    run bash -c "$env; . '$d/br.sh'; head() { return 1; }; out=\$(bounded_run 5 printf OK); echo \"rc=\$? out=[\$out]\""
    [[ "$output" == *"rc=125"* ]]
    [[ "$output" == *"out=[]"* ]]
    [[ "$output" == *"UNAVAILABLE, not empty"* ]]
}

# These three exercise the REAL vm_run from tests/integration/vm/helpers.bash.
#
# The version this replaced ran a standalone fixture that carried its own COPY
# of the replay line, so it pinned the fixture and nothing else: astra restored
# the exact bug in production (`head -c "$2" <&3; exit "$1"`, dropping the
# `|| exit 125`) and the test still passed (A-astra finding 4). That is the
# same copied-test defect sol caught in round 4 on the screenshot test -- I
# fixed that one and then reintroduced the pattern here, so these load the
# helper instead of imitating it.
_load_real_vm_run() {
    VM_EXEC="$FAKE_TOOLS/vm-exec"
    VM_NAME=fake-vm
    unset VM_SSH_PORT
    # shellcheck source=/dev/null
    source "$REPO_ROOT/tests/integration/vm/helpers.bash"
}

@test "vm_run (REAL): a healthy capture delivers the guest's output and status" {
    cat > "$FAKE_TOOLS/vm-exec" <<'EOF'
#!/usr/bin/env bash
printf 'GUESTOUT'
exit 7
EOF
    chmod +x "$FAKE_TOOLS/vm-exec"
    _load_real_vm_run
    vm_run "whatever" 2>"$BATS_TEST_TMPDIR/err"
    [ "$status" -eq 7 ]
    [[ "$output" == "GUESTOUT" ]]
    [ ! -s "$BATS_TEST_TMPDIR/err" ]
}

@test "vm_run (REAL): a failed replay is 125 with a diagnostic, not the guest's success" {
    cat > "$FAKE_TOOLS/vm-exec" <<'EOF'
#!/usr/bin/env bash
printf 'GUESTOUT'
exit 0
EOF
    chmod +x "$FAKE_TOOLS/vm-exec"
    _load_real_vm_run
    head() { return 1; }
    export -f head
    vm_run "whatever" 2>"$BATS_TEST_TMPDIR/err"
    unset -f head
    [ "$status" -eq 125 ]
    [[ "$output" == "" ]]
    grep -q "replay FAILED" "$BATS_TEST_TMPDIR/err"
    grep -q "UNAVAILABLE, not empty" "$BATS_TEST_TMPDIR/err"
}

@test "vm_run (REAL): a failed flag-file mktemp is 125 with a diagnostic, not a guest exit" {
    # astra's FOURTH missed mutation (A7): changing this branch's
    # `run bash -c 'exit 125'` to `exit 0` left the entire file passing --
    # 19/19, including all four other vm_run regressions. The branch had no
    # contract of its own.
    #
    # It matters because the command has ALREADY RUN by this point and its
    # capture exists: the original `run false` reported status=1 with empty
    # output and no explanation, which is indistinguishable from an ordinary
    # guest exit 1 (astra, A6 finding 4).
    cat > "$FAKE_TOOLS/vm-exec" <<'EOF'
#!/usr/bin/env bash
printf 'GUESTOUT'
exit 0
EOF
    chmod +x "$FAKE_TOOLS/vm-exec"
    _load_real_vm_run
    # Fail ONLY the flag-file mktemp, not the capture-file one.
    mktemp() {
        case "$*" in
            *vm-run-flag*) return 1 ;;
            *) command mktemp "$@" ;;
        esac
    }
    vm_run "whatever" 2>"$BATS_TEST_TMPDIR/err"
    unset -f mktemp
    [ "$status" -eq 125 ]
    grep -q "could not create the replay flag file" "$BATS_TEST_TMPDIR/err"
    grep -q "UNREADABLE, not empty" "$BATS_TEST_TMPDIR/err"
}

@test "vm_run (REAL): a failed capture-unlink is 125 with a diagnostic and never runs the command" {
    # astra's A8 finding 1. In its ten-site fault matrix vm_run was the ONLY
    # site that answered an injected `rm` failure with 1 instead of 125:
    # `run false` gives status=1, empty output and empty stderr, which is
    # exactly what a guest command that RAN and returned 1 looks like. Here
    # the command never ran -- and proving THAT is the point of the marker
    # file below, because "did not run" is the property the unlink check
    # exists to guarantee.
    cat > "$FAKE_TOOLS/vm-exec" <<EOF
#!/usr/bin/env bash
: > "$BATS_TEST_TMPDIR/PRODUCER_RAN"
printf 'GUESTOUT'
EOF
    chmod +x "$FAKE_TOOLS/vm-exec"
    _load_real_vm_run
    rm() { return 1; }
    vm_run "whatever" 2>"$BATS_TEST_TMPDIR/err"
    unset -f rm
    [ "$status" -eq 125 ]
    [ ! -e "$BATS_TEST_TMPDIR/PRODUCER_RAN" ]
    grep -q "capture setup FAILED" "$BATS_TEST_TMPDIR/err"
    grep -q "was NOT run" "$BATS_TEST_TMPDIR/err"
}

@test "vm_run (REAL): a failed stat is 125 -- completeness UNKNOWN is not success" {
    # astra's FIFTH missed mutation (A8 finding 2): changing this branch's
    # `status=125` to `status=0` left the whole three-file suite green at
    # 92/0/0. Without this, a helper that cannot check the size of a capture
    # its caller will PARSE reports success anyway.
    cat > "$FAKE_TOOLS/vm-exec" <<'EOF'
#!/usr/bin/env bash
printf 'AB'
exit 0
EOF
    chmod +x "$FAKE_TOOLS/vm-exec"
    _load_real_vm_run
    stat() { return 1; }
    export -f stat
    vm_run "whatever" 2>"$BATS_TEST_TMPDIR/err"
    unset -f stat
    [ "$status" -eq 125 ]
    # The bytes that WERE read are still handed back; only the verdict changes.
    [[ "$output" == "AB" ]]
    grep -q "UNKNOWN, not verified" "$BATS_TEST_TMPDIR/err"
}

@test "vm_run (REAL): a non-positive cap is refused as infrastructure, not reported as success" {
    # Found by my own per-branch mutation sweep, not by a reviewer: setting
    # this branch to `exit 0` left the whole suite green. A cap of 0 or -1 is
    # a MISCONFIGURATION -- `head -c -1` means "all but the last byte", which
    # silently returns almost-everything instead of enforcing a bound -- so it
    # has to fail loudly rather than hand back a plausible-looking string.
    cat > "$FAKE_TOOLS/vm-exec" <<'EOF'
#!/usr/bin/env bash
printf 'GUESTOUT'
exit 0
EOF
    chmod +x "$FAKE_TOOLS/vm-exec"
    _load_real_vm_run
    for bad in 0 00 01 -1 abc; do
        QCI_VM_RUN_CAP_BYTES="$bad" vm_run "whatever" 2>"$BATS_TEST_TMPDIR/err"
        [ "$status" -eq 125 ] || { echo "cap '$bad' gave status=$status, expected 125"; return 1; }
        grep -q "must be a positive integer" "$BATS_TEST_TMPDIR/err" \
            || { echo "cap '$bad' produced no diagnostic"; return 1; }
    done
    # EMPTY IS NOT INVALID: the cap is read with `${VAR:-default}`, so an empty
    # or unset value means "use the default" -- the ordinary shell convention.
    # Writing this test taught me the `''` arm in that `case` is therefore
    # UNREACHABLE, here and in the NINE sibling validators (gui.sh, mmnet.sh,
    # qdwin-helpers.sh, qdwin gui/17, 19, 20, 22, permissions-gui/21 and
    # noctalia/05 -- all read with `:-`). It is left in
    # place as defence should the substitution ever become `${VAR-default}`,
    # but it is not a check that fires today, and this pins which of the two
    # behaviours is actually the contract.
    QCI_VM_RUN_CAP_BYTES="" vm_run "whatever" 2>"$BATS_TEST_TMPDIR/err"
    [ "$status" -eq 0 ]
    [[ "$output" == "GUESTOUT" ]]
    [ ! -s "$BATS_TEST_TMPDIR/err" ]
}

@test "vm_run (REAL): a capture that fills the cap EXACTLY is a success, not a false red" {
    # The boundary test. An exact fit is a legitimate write, and failing it
    # would be a false red -- which is precisely what the first version of the
    # completeness check did: it compared the size against the parent's
    # `ulimit -f -H` with `>=`, so a healthy producer writing exactly the
    # limit got a fatal 125 asserting its output "was cut off" (astra, A6
    # finding 2). astra also showed the three tests above all PASS when
    # production `-gt` is mutated to `-ge`, so without this case the boundary
    # is unpinned.
    cat > "$FAKE_TOOLS/vm-exec" <<'EOF'
#!/usr/bin/env bash
printf 'ABCD'
exit 0
EOF
    chmod +x "$FAKE_TOOLS/vm-exec"
    _load_real_vm_run
    QCI_VM_RUN_CAP_BYTES=4 vm_run "whatever" 2>"$BATS_TEST_TMPDIR/err"
    [ "$status" -eq 0 ]
    [[ "$output" == "ABCD" ]]
    [ ! -s "$BATS_TEST_TMPDIR/err" ]
}

@test "vm_run (REAL): output past the cap is reported, not silently truncated" {
    cat > "$FAKE_TOOLS/vm-exec" <<'EOF'
#!/usr/bin/env bash
printf 'XXXX'
printf 'A%.0s' $(seq 1 4098)
printf 'MARKER'
exit 0
EOF
    chmod +x "$FAKE_TOOLS/vm-exec"
    _load_real_vm_run
    QCI_VM_RUN_CAP_BYTES=4 vm_run "whatever" 2>"$BATS_TEST_TMPDIR/err"
    # Before this check vm_run reported status=0 out=[XXXX]: a clean success
    # with everything past the cap, MARKER included, silently gone.
    [ "$status" -eq 125 ]
    grep -q "exceeded the 4-byte replay cap" "$BATS_TEST_TMPDIR/err"
}

# Every `qdwin_*` / `capture_*` function these helper files call must be defined
# somewhere they can reach. Those two prefixes are the whole scope -- this is
# not a general undefined-function checker, and the test name should not imply
# one (sol, B round 2).
#
# Workstream B was written before the round-10 cleanup deleted the unused
# `qdwin_apps_vmx_merged`, and reintegrating B auto-merged cleanly while leaving
# two calls to that now-absent function. `bash -n` stayed clean and every host
# suite stayed green, because nothing here executes the apps lane: the break
# surfaces only in a live run, as `command not found` and an empty evidence
# string, which reads as a product FAIL for Tk, FLTK and Swing. Both B-round-1
# reviewers found it independently. This is the cheap static check that would
# have caught it at the moment of the merge.
#
# Command-position only: a name inside a comment, a string or a heredoc is not
# a call. Sourced libraries count as reachable, so the check follows `.`/`source`
# of a literal path.
@test "caller audit: every qdwin_*/capture_* callee in the lane helpers is defined" {
    local f found=0
    for f in "$REPO_ROOT/qdwin/tests/gui/qdwin-helpers.sh" \
             "$REPO_ROOT/qdwin/tests/apps/qdwin-apps-helpers.sh" \
             "$REPO_ROOT/scripts/vm/vm-gui" \
             "$REPO_ROOT/scripts/vm/lib/capture-attest.sh"; do
        [ -f "$f" ] || continue
        found=1
        run python3 "$BATS_TEST_DIRNAME/undefined_callees.py" "$f"
        if [ "$status" -ne 0 ]; then
            printf 'undefined callee(s) in %s:\n%s\n' "$f" "$output" >&2
            false
        fi
    done
    [ "$found" -eq 1 ]
}

# The apps lane's libvirt-URI parser. It had no host test, and round 2 shipped
# two defects in it: `QDWIN_VIRSH='virsh -c'` spun forever (a `shift 2` with one
# argument left fails and shifts nothing), and any executable whose BASENAME was
# `virsh` was accepted although the capture library invokes the literal `virsh`
# from PATH -- so VM checks could use a wrapper while screenshots did not.
uri_for() {
    QDWIN_VIRSH="$1" QDWIN_WORKSPACE="$REPO_ROOT" \
        timeout 5 bash -c '
            . "$1"/tests/apps/qdwin-apps-helpers.sh 2>/dev/null
            qdwin_apps_libvirt_uri
        ' _ "$REPO_ROOT/qdwin"
}

@test "qdwin apps: the libvirt URI comes from QDWIN_VIRSH" {
    [ -f "$REPO_ROOT/qdwin/tests/apps/qdwin-apps-helpers.sh" ] || skip "qdwin component not present"
    run uri_for 'virsh -c qemu:///system'
    [ "$status" -eq 0 ]
    [ "$output" = "qemu:///system" ]
    run uri_for 'virsh --connect=qemu:///x'
    [ "$output" = "qemu:///x" ]
    run uri_for 'virsh -cqemu:///y'
    [ "$output" = "qemu:///y" ]
    run uri_for 'virsh'
    [ "$output" = "qemu:///session" ]
}

@test "qdwin apps: a URI flag with no operand is refused, not an infinite loop" {
    [ -f "$REPO_ROOT/qdwin/tests/apps/qdwin-apps-helpers.sh" ] || skip "qdwin component not present"
    run uri_for 'virsh -c'
    # 124 would be the timeout firing -- i.e. the hang.
    [ "$status" -eq 1 ]
    [[ "$output" == *"no URI after it"* ]]
}

@test "qdwin apps: a wrapper the capture library would not invoke is refused" {
    [ -f "$REPO_ROOT/qdwin/tests/apps/qdwin-apps-helpers.sh" ] || skip "qdwin component not present"
    run uri_for '/usr/local/bin/virsh -c qemu:///z'
    [ "$status" -eq 1 ]
    run uri_for 'my-virsh-wrapper'
    [ "$status" -eq 1 ]
    run uri_for 'virsh --quiet -c qemu:///w'
    [ "$status" -eq 1 ]
    [[ "$output" == *"does not understand"* ]]
}

# NON-VACUITY for the callee audit. Round 2 shipped it with a command-position
# claim it did not implement: names inside strings were reported (a manufactured
# finding from prose), while `if X`, `if ! X` and `do X` were missed, and a
# `<<<word` here-string was treated as a heredoc and swallowed the rest of the
# file. All of those are `bash -n` clean, so nothing else would catch them.
audit() {
    printf '%s' "$1" > "$BATS_TEST_TMPDIR/probe.sh"
    python3 "$BATS_TEST_DIRNAME/undefined_callees.py" "$BATS_TEST_TMPDIR/probe.sh"
}

@test "callee audit: a name in a STRING is prose, not a call" {
    run audit 'echo "see qdwin_long_gone for why"
printf %s '"'"'qdwin_also_gone'"'"'
'
    [ "$status" -eq 0 ]
}

@test "callee audit: a name in a COMMENT or a heredoc is not a call" {
    run audit '# qdwin_mentioned_in_comment was deleted
cat <<EOF
qdwin_inside_heredoc
EOF
'
    [ "$status" -eq 0 ]
}

@test "callee audit: command position after if / ! / do is a call" {
    local shape
    for shape in 'if qdwin_gone; then :; fi' \
                 'if ! qdwin_gone; then :; fi' \
                 'for i in 1; do qdwin_gone; done' \
                 'while qdwin_gone; do :; done' \
                 '{ qdwin_gone; }' \
                 'qdwin_gone && :' \
                 'x=$(qdwin_gone)' \
                 'x="$(qdwin_gone)"'; do
        run audit "$shape"
        [ "$status" -eq 1 ] || { echo "MISSED: $shape" >&2; false; }
        [ "$output" = qdwin_gone ]
    done
}

@test "callee audit: a here-string does not swallow the rest of the file" {
    run audit 'read -r a <<<"some text"
qdwin_gone
'
    [ "$status" -eq 1 ]
    [ "$output" = qdwin_gone ]
}

@test "callee audit: a defined function is not reported, however it is called" {
    run audit 'qdwin_here() { :; }
if qdwin_here; then :; fi
x="$(qdwin_here)"
'
    [ "$status" -eq 0 ]
}

@test "callee audit: only the two project prefixes are in scope" {
    # Not a general undefined-function checker, and the test must not imply it.
    run audit 'some_other_undefined_function'
    [ "$status" -eq 0 ]
}

# Round-3 review found five more shapes the auditor got wrong. The case-arm one
# was a LIVE miss: renaming qdwin_apps_send_key, which tests/apps calls only
# from `[0-9]) qdwin_apps_send_key ...`, used to leave the audit green.
@test "callee audit: a case-arm body is command position" {
    run audit 'case $x in
  weston) qdwin_gone ;;
esac'
    [ "$status" -eq 1 ]
    [ "$output" = qdwin_gone ]
}

@test "callee audit: a backtick substitution is command position" {
    run audit 'result=`qdwin_gone`'
    [ "$status" -eq 1 ]
    [ "$output" = qdwin_gone ]
}

@test "callee audit: a parameter expansion is NOT a call" {
    run audit 'echo "${qdwin_variable_only:-unset}"
echo ${qdwin_other_variable}'
    [ "$status" -eq 0 ]
}

@test "callee audit: a guarded source still contributes its definitions" {
    printf 'qdwin_provided() { :; }\n' > "$BATS_TEST_TMPDIR/defs.sh"
    run audit "source \"$BATS_TEST_TMPDIR/defs.sh\" || exit 1
qdwin_provided"
    [ "$status" -eq 0 ]
}

@test "callee audit: a definition inside a heredoc does not count as defined" {
    # DEF used to run on the raw text, so a function merely PRINTED by a
    # heredoc satisfied a real call and masked a missing callee.
    run audit 'cat <<EOF
qdwin_gone() { :; }
EOF
qdwin_gone'
    [ "$status" -eq 1 ]
    [ "$output" = qdwin_gone ]
}

@test "qdwin apps: the URI parser refuses everything it cannot express" {
    [ -f "$REPO_ROOT/qdwin/tests/apps/qdwin-apps-helpers.sh" ] || skip "qdwin component not present"
    local bad
    for bad in 'virsh nonsense' \
               'virsh -c qemu:///x trailing' \
               'virsh -c --quiet' \
               'virsh --quiet -c qemu:///w'; do
        run uri_for "$bad"
        [ "$status" -eq 1 ] || { echo "ACCEPTED: $bad -> $output" >&2; false; }
    done
    # and the shapes it CAN express still work
    run uri_for 'virsh -cqemu:///y'
    [ "$output" = "qemu:///y" ]
}

# Round-4 review found four more auditor defects, two in each direction.
@test "callee audit: a word after a substitution is an ARGUMENT, not a call" {
    run audit 'printf "%s\n" "$(printf ok)" qdwin_plain_argument'
    [ "$status" -eq 0 ]
}

@test "callee audit: a declare -f phrase in a COMMENT does not suppress a call" {
    run audit '# declare -f qdwin_gone
qdwin_gone'
    [ "$status" -eq 1 ]
    [ "$output" = qdwin_gone ]
}

@test "callee audit: a real declare -f guard still marks an optional dependency" {
    run audit 'declare -f qdwin_optional >/dev/null 2>&1 && qdwin_optional'
    [ "$status" -eq 0 ]
}

@test "callee audit: a # inside a string does not eat the rest of the line" {
    run audit 'echo "a # b"; qdwin_gone'
    [ "$status" -eq 1 ]
    [ "$output" = qdwin_gone ]
}

@test "callee audit: a backtick call inside double quotes is seen" {
    run audit 'x="`qdwin_gone`"'
    [ "$status" -eq 1 ]
    [ "$output" = qdwin_gone ]
}

# Round-5 review found seven more auditor shapes, in both directions.
@test "callee audit: an assignment or redirection PREFIX still leaves a call" {
    local shape
    for shape in 'FOO=1 qdwin_gone' '>/dev/null qdwin_gone' 'coproc qdwin_gone'; do
        run audit "$shape"
        [ "$status" -eq 1 ] || { echo "MISSED: $shape" >&2; false; }
        [ "$output" = qdwin_gone ]
    done
}

@test "callee audit: a declare -f for a DIFFERENT name does not suppress a call" {
    run audit 'declare -f qdwin_gone_helper >/dev/null; qdwin_gone'
    [ "$status" -eq 1 ]
    [ "$output" = qdwin_gone ]
}

@test "callee audit: an assignment or arithmetic is not a call" {
    run audit 'qdwin_dir=/tmp/x
x=$((qdwin_n + 1))'
    [ "$status" -eq 0 ]
}

@test "callee audit: a definition need not start its line" {
    run audit 'echo "a # b"; qdwin_local() { :; }
qdwin_local'
    [ "$status" -eq 0 ]
}

# Round-6 review: the false-positive filter erased real calls file-wide.
@test "callee audit: an ASSIGNMENT does not suppress a real call to that name" {
    run audit 'qdwin_gone=1
qdwin_gone'
    [ "$status" -eq 1 ]
    [ "$output" = qdwin_gone ]
}

@test "callee audit: ARITHMETIC does not suppress a real call to that name" {
    run audit 'x=$((qdwin_gone + 1))
qdwin_gone'
    [ "$status" -eq 1 ]
    [ "$output" = qdwin_gone ]
}

# Round-7 review: three audit defects, two of them claims that were not wired.
@test "callee audit: a function defined with the KEYWORD form is defined" {
    # DEF_KW was compiled and never referenced, so `function name {` was
    # reported as undefined while the commit message said it was handled
    # (sol and fable, B round 7).
    run audit 'function qdwin_local {
    :
}
qdwin_local'
    [ "$status" -eq 0 ]
}

@test "callee audit: an UNRELATED declare -f does not suppress a real call" {
    # File-wide erasure again, surviving in the guard filter (sol, B round 7).
    run audit 'unused_probe() {
    declare -f qdwin_gone >/dev/null
}
qdwin_gone'
    [ "$status" -eq 1 ]
    [ "$output" = qdwin_gone ]
}

@test "callee audit: a guard does not reach across a newline to the next call" {
    # What this pins is the REGION, not GUARD's whitespace class: a bare guard
    # covers the rest of its own line, so a next-line call is outside it even
    # if the name matched. Widening `[ \t]+` to `\s+` still leaves this green
    # -- verified by mutation, which is why the test below exists (fable, B
    # round 8, who said this test pinned the iteration rather than the class).
    run audit 'declare -f qdwin_a
qdwin_b'
    [ "$status" -eq 1 ]
    [ "$output" = qdwin_b ]
}

@test "callee audit: blanking an arithmetic expression preserves its length" {
    # ARITH matches an OPTIONAL leading `$`, so a fixed four-space delimiter
    # replacement returned `$((x))` one character short and `((x))` exact. The
    # skew never changed a verdict -- guard_regions() and the call scan read
    # the same post-substitution string, so bounds and offsets move together
    # (the test below pins that) -- but the docstring asserts length-exactness,
    # and an assertion a reader will build on has to be true. Reverting to
    # `'  ' + ' ' * len(body) + '  '` turns red exactly those `$` shapes that
    # REACH the blanking -- not all of them: `$(( $(f) ))` takes the
    # leave-entirely-alone branch and is returned unchanged either way, so it
    # covers that branch rather than the skew (sol, C2 round 2).
    run python3 - "$BATS_TEST_DIRNAME/undefined_callees.py" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("uc", sys.argv[1])
uc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(uc)
bad = []
for shape in ("((1 + 2))", "$((1 + 2))", "$((x))", "((a>b))",
              "$(( $(f) ))", "x=$((i+1)) y=$((j+2))"):
    out = uc.ARITH.sub(uc.blank_arith, shape)
    if len(out) != len(shape):
        bad.append(f"{shape!r}: {len(shape)} -> {len(out)}")
print("\n".join(bad))
sys.exit(1 if bad else 0)
PY
    [ "$status" -eq 0 ]
    [ -z "$output" ]
}

@test "callee audit: an arithmetic expansion before a guard does not move it" {
    # The consequence half of the same round-11 finding. THIS TEST IS GREEN
    # UNDER THE REVERTED FORM TOO, and that is the point: it records that the
    # length skew is behaviourally inert, because guard_regions() and the call
    # scan read one and the same post-substitution string. It is a non-vacuity
    # guard: it pins the VERDICT for this shape, nothing more. It does NOT
    # establish that the two scans share a string, and it is not a guaranteed
    # detector of a future change that splits them -- now that blanking is
    # length-exact, two different strings would still agree on offsets unless
    # a skew were reintroduced as well (sol, C2, correcting an overclaim in
    # this comment). It is a semantic regression test. The mutation test for
    # the blanking itself is the one above.
    run audit 'x=$((1 + 2))
if declare -f qdwin_a
then
  qdwin_a
fi
qdwin_b'
    [ "$status" -eq 1 ]
    [ "$output" = qdwin_b ]

    run audit 'x=$((1 + 2)); y=$((3 + 4)); z=$(( 5 + 6 ))
if declare -f qdwin_a
then
  qdwin_a
fi
qdwin_b'
    [ "$status" -eq 1 ]
    [ "$output" = qdwin_b ]
}

@test "callee audit: a guard's NAME LIST stops at the end of its line" {
    # The class matters where the region is wide enough to hide the leak: as
    # the condition of an `if`, the guard covers the whole construct. With
    # `\s+` the name list swallows `qdwin_b` off the next line and the call in
    # the body is silently suppressed -- a false negative, the expensive
    # direction. Mutating `[ \t]+` -> `\s+` in GUARD turns this test red.
    run audit 'if declare -f qdwin_a
qdwin_b
then
  qdwin_b
fi'
    [ "$status" -eq 1 ]
    [ "$output" = qdwin_b ]
}

@test "callee audit: a REAL guard still marks an optional dependency, inline or in an if" {
    run audit 'declare -f qdwin_opt >/dev/null 2>&1 && qdwin_opt'
    [ "$status" -eq 0 ]
    run audit 'if declare -f qdwin_opt >/dev/null 2>&1; then
    qdwin_opt
fi'
    [ "$status" -eq 0 ]
}

@test "callee audit: arithmetic commands and += / array assignments are not calls" {
    run audit '(( qdwin_n = 1 ))
qdwin_count+=1
qdwin_arr[0]=x'
    [ "$status" -eq 0 ]
}

@test "callee audit: a case PATTERN is not a call" {
    # `qdwin_ghost)` is a label, not an invocation. blank_case_patterns() is
    # what makes that true, and until 2026-09-18 nothing pinned it: a mutation
    # battery that neutered every regex in the auditor in turn found this one
    # (and only this one) changing a verdict with no test noticing. Neuter
    # CASE_PATTERN and `qdwin_ghost` is reported as an undefined callee.
    run audit 'f() { :; }
case "$x" in
  qdwin_ghost) f ;;
esac'
    [ "$status" -eq 0 ]
    # ...but the BODY of the arm still is a call.
    run audit 'case "$x" in
  label) qdwin_gone ;;
esac'
    [ "$status" -eq 1 ]
    [ "$output" = qdwin_gone ]
}

@test "callee audit: a call nested inside arithmetic is seen" {
    run audit 'x=$(($(qdwin_gone)))'
    [ "$status" -eq 1 ]
    [ "$output" = qdwin_gone ]
}

# Round-8 review: guard regions were control-flow blind in BOTH directions.
@test "callee audit: the ELSE branch of a guard is the ABSENT branch" {
    # The call runs precisely when the helper is missing, so suppressing it is
    # the worst kind of false negative (sol, B round 8). One-line form first:
    # a line-granular region cannot express it.
    run audit 'if declare -f qdwin_opt; then :; else qdwin_opt; fi'
    [ "$status" -eq 1 ]
    [ "$output" = qdwin_opt ]
    run audit 'if declare -f qdwin_opt; then
  qdwin_opt
else
  qdwin_opt
fi'
    [ "$status" -eq 1 ]
}

@test "callee audit: a NEGATED guard guards nothing in its then-body" {
    run audit 'if ! declare -f qdwin_opt; then qdwin_opt; fi'
    [ "$status" -eq 1 ]
    [ "$output" = qdwin_opt ]
}

@test "callee audit: an until-loop body is not guarded either" {
    run audit 'until declare -f qdwin_opt; do qdwin_opt; done'
    [ "$status" -eq 1 ]
}

@test "callee audit: if/fi are syntax only in COMMAND POSITION" {
    # `echo fi` closed a range early; `echo if` ran it to EOF (sol, B round 8).
    run audit 'if declare -f qdwin_opt; then
  echo fi
  qdwin_opt
fi'
    [ "$status" -eq 0 ]
    run audit 'if declare -f qdwin_opt; then
  echo if
  qdwin_opt
fi
qdwin_opt'
    [ "$status" -eq 1 ]
    [ "$output" = qdwin_opt ]
}

@test "callee audit: elif and while are real guards" {
    run audit 'if false; then :;
elif declare -f qdwin_opt; then
  qdwin_opt
fi'
    [ "$status" -eq 0 ]
    run audit 'while declare -f qdwin_opt; do
  qdwin_opt
done'
    [ "$status" -eq 0 ]
}

# Round-9 review: the line-at-a-time guard scanner was wrong in both directions.
@test "callee audit: a while-guard body may contain if/for/brace blocks" {
    # `_delta` counted the body's `if` but subtracted only `done`, so depth
    # never reached zero and EVERY later call in the file was silenced -- the
    # direction the docstring itself calls the worst (sol and fable, round 9).
    run audit 'while declare -f qdwin_opt; do
  if true; then :; fi
  qdwin_opt
done
qdwin_opt'
    [ "$status" -eq 1 ]
    [ "$output" = qdwin_opt ]
    # ...and the guarded call inside such a body is still suppressed
    run audit 'while declare -f qdwin_opt; do
  for i in 1 2; do :; done
  qdwin_opt
done'
    [ "$status" -eq 0 ]
    run audit 'if declare -f qdwin_opt; then { if true; then :; fi; }
  qdwin_opt
fi'
    [ "$status" -eq 0 ]
}

@test "callee audit: a call after fi is outside the region, same line or not" {
    # The one-liner problem character offsets were introduced for: solved for
    # `else` in round 9 and not for `fi` (fable, B round 9).
    run audit 'if declare -f qdwin_opt; then :; fi; qdwin_opt'
    [ "$status" -eq 1 ]
    run audit 'if declare -f qdwin_opt; then
  :
fi; qdwin_opt'
    [ "$status" -eq 1 ]
}

@test "callee audit: a NESTED one-liner else is not the guard's own" {
    run audit 'if declare -f qdwin_opt; then if true; then :; else :; fi; else qdwin_opt; fi'
    [ "$status" -eq 1 ]
    [ "$output" = qdwin_opt ]
}

@test "callee audit: negation decides WHICH branch is guarded" {
    # `if ! declare -f f; then A; else B; fi` runs A when f is ABSENT, so A is
    # unguarded and B is guarded. Round 9 dropped the negated guard whole, so
    # its `else` -- the PRESENT branch -- was not a region (fable, round 9).
    run audit 'if ! declare -f qdwin_opt; then :; else qdwin_opt; fi'
    [ "$status" -eq 0 ]
    run audit 'if ! declare -f qdwin_opt; then qdwin_opt; fi'
    [ "$status" -eq 1 ]
    # a `!` in front of something else does not negate the guard
    run audit 'if ! [ -e /nonexistent ] && declare -f qdwin_opt; then qdwin_opt; fi'
    [ "$status" -eq 0 ]
}

@test "callee audit: the construct keyword need not start the line" {
    run audit ':; if declare -f qdwin_opt; then
  qdwin_opt
fi'
    [ "$status" -eq 0 ]
}
