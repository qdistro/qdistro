#!/usr/bin/env bats
#
# Host-only regressions for two pieces of tests/integration/vm/tiered-isolation.bats
# that decide a verdict without the VM being able to object:
#
# 1. teardown_file's cleanup verdict. bats calls it as
#    `teardown_file ... || bats_teardown_file_status=$?`, so errexit never
#    stops it and only its final return status counts. A version that ended on
#    a successful broker check returned 0 after a FAILED qdlocker restore, and
#    the file went green (astra round 3, triage-fix-review-r3-astra.md #1). The
#    matrix below runs the REAL teardown_file, extracted from the suite, under
#    the REAL installed bats runner with only vm_run/reap_vm_drivers stubbed,
#    and checks the runner's exit status. Round 4 (triage-fix-review-r4-astra.md
#    #1/#2) added a failing host reaper, and cases that RUN the real qdlocker
#    restore guest string under command shims: stubbing the whole vm_run
#    result could never see a guest script that swallowed its own failures.
#
# 2. run_driver_keep_stderr / split_driver_frame, which cut driver stdout (the
#    assertion input) out of a merged capture. A fixed marker could be forged or
#    go missing (review #2); the frame now uses a per-call nonce and fails
#    closed. The cases run the real functions against a local stand-in for the
#    guest. Round 4 (#3/#4) added a failed/short host nonce and non-canonical
#    end statuses (`08`, `256`, ...).
#
# No VM, no libvirt, no systemd.

bats_require_minimum_version 1.5.0

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    SUITE="$REPO_ROOT/tests/integration/vm/tiered-isolation.bats"
    HELPERS="$REPO_ROOT/tests/integration/vm/helpers.bash"
    WORK="$BATS_TEST_TMPDIR/w"
    mkdir -p "$WORK/bin"
}

# extract_fn <file> <name> — print one top-level function from <file>: the
# shortest range from `<name>() {` to a column-0 `}` that bash parses cleanly.
# (A plain sed range stops early: teardown_file's heredoc defines adm_uctl()
# with its own column-0 `}`.) Fails when no such range exists.
extract_fn() {
    local file=$1 name=$2 start e snippet
    start=$(grep -n "^$name() {\$" "$file" | head -1 | cut -d: -f1)
    [ -n "$start" ] || return 1
    for e in $(awk -v s="$start" 'NR > s && /^}$/ { print NR }' "$file"); do
        snippet=$(sed -n "${start},${e}p" "$file")
        if [ -z "$(bash -n <<<"$snippet" 2>&1)" ]; then
            printf '%s\n' "$snippet"
            return 0
        fi
    done
    return 1
}

# ---------------------------------------------------------------------------
# 1. teardown_file under the real bats runner
# ---------------------------------------------------------------------------

# run_teardown_matrix — build an inner .bats file with one green test, the
# suite's real teardown_file and the real assert/fail_loud helpers, and run it
# with the real bats. MODE_rules / MODE_broker / MODE_locker pick each stubbed
# vm_run's outcome: ok | fail | transport | nopass | quoted-ok, plus `exec`,
# which RUNS the real guest command string locally under /bin/sh with the
# command shims from make_guest_shims (see there; SHIM_FAIL picks failures).
# MODE_reap=fail makes the host reaper return 1.
run_teardown_matrix() {
    local inner="$BATS_TEST_TMPDIR/inner.bats"
    : > "$WORK/calls.log"
    : > "$WORK/shim.log"
    make_guest_shims
    {
        echo "CALLS='$WORK/calls.log'"
        echo "SHIMS='$WORK/shims'"
        cat <<'STUB'
reap_vm_drivers() { echo reap >> "$CALLS"; [ "${MODE_reap:-ok}" = ok ]; }
vm_run() {
    local step mode_var mode
    case "$1" in
        *zz-tier2-isolation-allow.yaml*)             step=rules ;;
        *"systemctl restart qdistro-admin-broker"*)  step=broker ;;
        *qdlocker*)                                  step=locker ;;
        *)                                           step=unknown ;;
    esac
    echo "$step" >> "$CALLS"
    mode_var="MODE_$step"; mode=${!mode_var:-ok}
    case $mode in
        exec)
            output=$(PATH="$SHIMS:$PATH" /bin/sh -c "$1" 2>&1) && status=0 || status=$? ;;
        ok)
            status=0
            case $step in
                # A successful guest prints what the command echoes; the
                # f2015e843 shape folded the restart into this same call.
                rules)  output="PASS: tier-2 broker allow-rules removed"
                        [[ "$1" != *"BROKER-RESTART: ok"* ]] || output+=$'\nBROKER-RESTART: ok' ;;
                locker) output="PASS: qdlocker idle-auto-lock restored" ;;
                *)      output="" ;;
            esac ;;
        fail)      status=1;   output="Job for qdistro-admin-broker.service failed" ;;
        transport) status=255; output="[vm-exec] ERROR: guest agent not responding" ;;
        nopass)    status=0;   output="(no PASS line)" ;;
        # A failed restart whose status dump quotes an old success token.
        quoted-ok) status=3;   output="journal: BROKER-RESTART: ok PASS: tier-2 broker allow-rules removed" ;;
    esac
}
STUB
        extract_fn "$HELPERS" assert_success
        extract_fn "$HELPERS" assert_output_contains
        extract_fn "$HELPERS" fail_loud
        extract_fn "$SUITE" teardown_file
        echo '@test "green body" { true; }'
    } > "$inner"
    # The extraction itself must have worked, or every case below is vacuous.
    grep -q '^teardown_file() {$' "$inner"
    grep -q 'qdlocker' "$inner"
    run bats "$inner"
    CALLS_SEEN=$(tr '\n' ' ' < "$WORK/calls.log")
}

# make_guest_shims — stand-ins for the guest commands the qdlocker restore
# runs (rm, rmdir, systemctl, runuser). Each logs its argv to shim.log and,
# when told to fail, prints a stderr diagnostic and exits 1. SHIM_FAIL is a
# space-separated list: `rm`, `rmdir`, `systemctl:<verb>` (the machined path),
# `runuser:<verb>` (the login-shell fallback); <verb> is daemon-reload or
# restart. Nothing on the host is touched.
make_guest_shims() {
    local c
    mkdir -p "$WORK/shims"
    for c in rm rmdir systemctl runuser; do
        {
            echo '#!/bin/bash'
            printf 'LOG=%q; NAME=%q\n' "$WORK/shim.log" "$c"
            cat <<'SHIM'
echo "$NAME $*" >> "$LOG"
key=$NAME
case $NAME in
    systemctl|runuser)
        if [[ "$*" == *restart* ]]; then key+=:restart; else key+=:daemon-reload; fi ;;
esac
case " ${SHIM_FAIL:-} " in
    *" $key "*) echo "$key-shim-diag" >&2; exit 1 ;;
esac
exit 0
SHIM
        } > "$WORK/shims/$c"
        chmod +x "$WORK/shims/$c"
    done
}

@test "teardown_file: all cleanups succeed -> file passes" {
    MODE_rules=ok MODE_broker=ok MODE_locker=ok run_teardown_matrix
    [ "$status" -eq 0 ]
    [ "$CALLS_SEEN" = "reap rules broker locker " ]
}

@test "teardown_file: broker ok + qdlocker transport failure -> file FAILS (astra r3 #1)" {
    MODE_rules=ok MODE_broker=ok MODE_locker=transport run_teardown_matrix
    [ "$status" -ne 0 ]
    [[ "$output" == *"could not restore qdlocker idle-auto-lock"* ]]
}

@test "teardown_file: broker ok + qdlocker output lacks PASS -> file FAILS" {
    MODE_rules=ok MODE_broker=ok MODE_locker=nopass run_teardown_matrix
    [ "$status" -ne 0 ]
}

@test "teardown_file: broker restart fails -> file FAILS, qdlocker restore still runs" {
    MODE_rules=ok MODE_broker=fail MODE_locker=ok run_teardown_matrix
    [ "$status" -ne 0 ]
    [ "$CALLS_SEEN" = "reap rules broker locker " ]
    [[ "$output" == *"broker restart after removing the tier-2 allow-rules failed"* ]]
}

@test "teardown_file: a failed restart that quotes a success token still FAILS" {
    MODE_rules=ok MODE_broker=quoted-ok MODE_locker=ok run_teardown_matrix
    [ "$status" -ne 0 ]
}

@test "teardown_file: rule removal fails -> file FAILS, broker + qdlocker cleanups still run" {
    MODE_rules=transport MODE_broker=ok MODE_locker=ok run_teardown_matrix
    [ "$status" -ne 0 ]
    [ "$CALLS_SEEN" = "reap rules broker locker " ]
}

@test "teardown_file: everything fails -> file FAILS, every cleanup attempted" {
    MODE_rules=fail MODE_broker=transport MODE_locker=transport run_teardown_matrix
    [ "$status" -ne 0 ]
    [ "$CALLS_SEEN" = "reap rules broker locker " ]
}

@test "teardown_file: host reaper fails -> file FAILS, guest cleanups still run (astra r4 #2)" {
    MODE_reap=fail MODE_rules=ok MODE_broker=ok MODE_locker=ok run_teardown_matrix
    [ "$status" -ne 0 ]
    [ "$CALLS_SEEN" = "reap rules broker locker " ]
    [[ "$output" == *"could not reap the driver-staging http server"* ]]
}

# The qdlocker restore below runs the REAL guest command string (not a stubbed
# vm_run result) under /bin/sh with the command shims.

@test "qdlocker restore (real guest string): all steps succeed -> file passes" {
    MODE_locker=exec SHIM_FAIL="" run_teardown_matrix
    [ "$status" -eq 0 ]
    grep -qx 'rm -f /etc/systemd/user/qdlocker.service.d/99-qci-no-idle-lock.conf' "$WORK/shim.log"
    grep -q '^systemctl .*daemon-reload$' "$WORK/shim.log"
    grep -q '^systemctl .*restart qdlocker.service$' "$WORK/shim.log"
    run grep -q '^runuser' "$WORK/shim.log"
    [ "$status" -eq 1 ]
}

@test "qdlocker restore (real guest string): machined fails, runuser fallback ok -> file passes" {
    MODE_locker=exec SHIM_FAIL="systemctl:daemon-reload systemctl:restart" run_teardown_matrix
    [ "$status" -eq 0 ]
    grep -q '^runuser .*daemon-reload$' "$WORK/shim.log"
    grep -q '^runuser .*restart qdlocker.service$' "$WORK/shim.log"
}

@test "qdlocker restore (real guest string): rmdir of a non-empty dir stays best effort" {
    MODE_locker=exec SHIM_FAIL="rmdir" run_teardown_matrix
    [ "$status" -eq 0 ]
}

@test "qdlocker restore (real guest string): drop-in rm fails -> file FAILS, reload + restart still run (astra r4 #1)" {
    MODE_locker=exec SHIM_FAIL="rm" run_teardown_matrix
    [ "$status" -ne 0 ]
    [[ "$output" == *"could not restore qdlocker idle-auto-lock"* ]]
    [[ "$output" != *"PASS: qdlocker idle-auto-lock restored"* ]]
    grep -q '^systemctl .*daemon-reload$' "$WORK/shim.log"
    grep -q '^systemctl .*restart qdlocker.service$' "$WORK/shim.log"
}

@test "qdlocker restore (real guest string): daemon-reload fails both ways -> file FAILS with both diagnostics, restart still runs" {
    MODE_locker=exec SHIM_FAIL="systemctl:daemon-reload runuser:daemon-reload" run_teardown_matrix
    [ "$status" -ne 0 ]
    [[ "$output" == *"could not restore qdlocker idle-auto-lock"* ]]
    [[ "$output" == *"systemctl:daemon-reload-shim-diag"* ]]
    [[ "$output" == *"runuser:daemon-reload-shim-diag"* ]]
    grep -q '^systemctl .*restart qdlocker.service$' "$WORK/shim.log"
}

@test "qdlocker restore (real guest string): restart fails both ways -> file FAILS with both diagnostics" {
    MODE_locker=exec SHIM_FAIL="systemctl:restart runuser:restart" run_teardown_matrix
    [ "$status" -ne 0 ]
    [[ "$output" == *"could not restore qdlocker idle-auto-lock"* ]]
    [[ "$output" == *"systemctl:restart-shim-diag"* ]]
    [[ "$output" == *"runuser:restart-shim-diag"* ]]
}

# ---------------------------------------------------------------------------
# 2. run_driver_keep_stderr / split_driver_frame
# ---------------------------------------------------------------------------

# load_framing — define the real functions plus a vm_run stand-in that runs
# the guest command locally (curl copies $FAKE_DRIVER; /tmp is redirected into
# the test dir) and wraps it in vm-exec-style noise before and after.
load_framing() {
    eval "$(extract_fn "$SUITE" run_driver_keep_stderr)"
    eval "$(extract_fn "$SUITE" split_driver_frame)"
    declare -F run_driver_keep_stderr >/dev/null
    declare -F split_driver_frame >/dev/null
    cat > "$WORK/bin/curl" <<'EOF'
#!/bin/bash
while [ $# -gt 0 ]; do case $1 in -o) dest=$2; shift 2 ;; *) shift ;; esac; done
[ -n "${CURL_FAIL:-}" ] && exit 22
cp "$FAKE_DRIVER" "$dest"
EOF
    chmod +x "$WORK/bin/curl"
    # shellcheck disable=SC2034 # read by the eval-ed run_driver_keep_stderr
    QDISTRO_BATS_HTTP_PORT=1
    vm_run() {
        local c=${1//\/tmp\//$WORK/}
        output=$(
            echo "[vm-exec] note: PASS: from-the-transport-before SKIP: pre"
            PATH="$WORK/bin:$PATH" sh -c "$c" 2>&1; rc=$?
            echo "[vm-exec] --- stderr --- PASS: from-the-transport-after SKIP: post"
            exit $rc
        ) && status=0 || status=$?
    }
}

# driver <exit> <stdout-body> [stderr-body] — write the fake driver.
driver() {
    export FAKE_DRIVER="$WORK/driver.sh"
    {
        printf 'printf %%s %q\n' "$2"
        printf 'printf %%s %q >&2\n' "${3:-}"
        printf 'exit %s\n' "$1"
    } > "$FAKE_DRIVER"
}

@test "frame: stdout only reaches \$output; driver stderr and transport noise do not" {
    load_framing
    driver 0 $'PASS: first\nPASS: last\n' $'unit status: start-limit-hit\nSKIP: PASS: bogus\n'
    run_driver_keep_stderr s62 s62-x.sh 2>"$WORK/err"
    [ "$status" -eq 0 ]
    [ "$output" = $'PASS: first\nPASS: last' ]
    grep -q 'SKIP: PASS: bogus' "$WORK/err"
    grep -q 'from-the-transport-after' "$WORK/err"
}

@test "frame: a driver printing marker-like lines cannot move the boundary" {
    load_framing
    driver 0 $'PASS: first\n@@qci-driver-stderr@@\n@@qci-driver-stderr-0123456789abcdef0123456789abcdef@@\n@@qci-driver-end-0123456789abcdef0123456789abcdef rc=0@@\nPASS: last\n'
    run_driver_keep_stderr s62 s62-x.sh 2>"$WORK/err"
    [ "$status" -eq 0 ]
    [[ "$output" == *"PASS: last"* ]]
}

@test "frame: driver stdout without a trailing newline is kept whole" {
    load_framing
    driver 0 'PASS: only' 'err-no-newline'
    run_driver_keep_stderr s62 s62-x.sh 2>"$WORK/err"
    [ "$status" -eq 0 ]
    [ "$output" = 'PASS: only' ]
}

@test "frame: driver exit status is preserved (1, 137)" {
    load_framing
    driver 1 $'FAIL: x\n'
    run_driver_keep_stderr s62 s62-x.sh 2>"$WORK/err"
    [ "$status" -eq 1 ]
    driver 137 $'PASS: x\n'
    run_driver_keep_stderr s62 s62-x.sh 2>"$WORK/err"
    [ "$status" -eq 137 ]
}

@test "frame: staging failure keeps the transport status and fails closed" {
    load_framing
    driver 0 $'PASS: x\n'
    CURL_FAIL=1 run_driver_keep_stderr s62 s62-x.sh 2>"$WORK/err"
    [ "$status" -eq 22 ]
    grep -q 'invalid frame' "$WORK/err"
}

@test "frame: a zero-status capture with no frame fails closed with the raw capture" {
    load_framing
    status=0; output=$'PASS: x\n[vm-exec] --- stderr ---\nSKIP: y'
    split_driver_frame s62 0123 2>"$WORK/err"
    [ "$status" -ne 0 ]
    grep -q 'invalid frame' "$WORK/err"
    grep -q 'SKIP: y' "$WORK/err"
}

@test "frame: a duplicated marker (ambiguous boundary) fails closed" {
    load_framing
    local n=abcd
    status=0; output="@@qci-driver-stdout-$n@@
PASS: x
@@qci-driver-stderr-$n@@
@@qci-driver-stderr-$n@@
@@qci-driver-end-$n rc=0@@"
    split_driver_frame s62 "$n" 2>"$WORK/err"
    [ "$status" -ne 0 ]
}

@test "frame: a truncated capture (no end marker) fails closed" {
    load_framing
    local n=abcd
    status=0; output="@@qci-driver-stdout-$n@@
PASS: x
@@qci-driver-stderr-$n@@
partial"
    split_driver_frame s62 "$n" 2>"$WORK/err"
    [ "$status" -ne 0 ]
}

@test "frame: end-marker rc wins over a zero transport status" {
    load_framing
    local n=abcd
    status=0; output="@@qci-driver-stdout-$n@@
PASS: x
@@qci-driver-stderr-$n@@
@@qci-driver-end-$n rc=3@@"
    split_driver_frame s62 "$n" 2>"$WORK/err"
    [ "$status" -eq 3 ]
    [ "$output" = 'PASS: x' ]
}

# od_shim <exit> <text> — an `od` stand-in for the host-side nonce read.
od_shim() {
    mkdir -p "$WORK/odbin"
    printf '#!/bin/bash\nprintf %%s %q\nexit %s\n' "$2" "$1" > "$WORK/odbin/od"
    chmod +x "$WORK/odbin/od"
}

# nonce_case <exit> <text> — run the real run_driver_keep_stderr with od
# shimmed; vm_run only records that it was (wrongly) reached.
nonce_case() {
    load_framing
    # shellcheck disable=SC2329 # called by the real run_driver_keep_stderr
    vm_run() { touch "$WORK/vm_run_called"; status=0; output=""; }
    od_shim "$1" "$2"
    PATH="$WORK/odbin:$PATH" run_driver_keep_stderr s62 s62-x.sh 2>"$WORK/err"
}

@test "nonce: od fails with no output -> status 125, driver never run (astra r4 #3)" {
    nonce_case 1 ""
    [ "$status" -eq 125 ]
    [ ! -e "$WORK/vm_run_called" ]
    grep -q 'could not generate a 32-hex frame nonce' "$WORK/err"
}

@test "nonce: od fails after printing a full-looking nonce -> status 125" {
    nonce_case 1 $' 01 23 45 67 89 ab cd ef 01 23 45 67 89 ab cd ef\n'
    [ "$status" -eq 125 ]
    [ ! -e "$WORK/vm_run_called" ]
}

@test "nonce: short od output -> status 125, driver never run" {
    nonce_case 0 $' 01 23\n'
    [ "$status" -eq 125 ]
    [ ! -e "$WORK/vm_run_called" ]
}

# frame_with_rc <rc-text> — a complete, well-ordered frame ending in rc=<rc-text>.
frame_with_rc() {
    local n=abcd
    output="@@qci-driver-stdout-$n@@
PASS: x
@@qci-driver-stderr-$n@@
@@qci-driver-end-$n rc=$1@@"
}

@test "frame: non-canonical end statuses are invalid frames (astra r4 #4)" {
    load_framing
    local bad
    for bad in 08 010 00 256 1000 -1 ' 3' 3x ''; do
        status=0; frame_with_rc "$bad"
        split_driver_frame s62 abcd 2>"$WORK/err"
        [ "$status" -eq 125 ] || { echo "rc='$bad' gave status=$status"; false; }
        grep -q 'invalid frame' "$WORK/err"
    done
}

@test "frame: an invalid end status keeps a non-zero transport status" {
    load_framing
    status=255; frame_with_rc 08
    split_driver_frame s62 abcd 2>"$WORK/err"
    [ "$status" -eq 255 ]
    grep -q 'invalid frame' "$WORK/err"
}

@test "frame: canonical end statuses 0 and 255 are accepted" {
    load_framing
    status=0; frame_with_rc 0
    split_driver_frame s62 abcd 2>"$WORK/err"
    [ "$status" -eq 0 ]
    [ "$output" = 'PASS: x' ]
    status=0; frame_with_rc 255
    split_driver_frame s62 abcd 2>"$WORK/err"
    [ "$status" -eq 255 ]
}
