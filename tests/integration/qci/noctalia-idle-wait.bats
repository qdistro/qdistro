#!/usr/bin/env bats
# qci:host-only
#
# Host-only tests for scenario 05's guest-timed idle wait
# (tests/integration/qdwin-noctalia/noctalia-helpers.sh: noct_idle_wait_*).
# No VM: QDWIN_VM_EXEC is a fake that runs the guest command locally, and a
# fake systemd-run starts the unit's command detached.
#
# Why: in full-20261006T175536Z-3524705 the driver's 75 s host-side wait was
# cut short by its tool, it read DPMS itself ~50 s after its last input, and
# recorded a product FAIL. The guest now times the wait and records how long
# it waited; the verdict refuses a short wait.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    # shellcheck disable=SC1090
    source "$REPO_ROOT/tests/integration/qdwin-noctalia/noctalia-helpers.sh"
    BIN="$BATS_TEST_TMPDIR/bin"; mkdir -p "$BIN"
    cat > "$BIN/vm-exec" <<'SH'
#!/bin/bash
shift
exec bash -c "$1"
SH
    cat > "$BIN/systemd-run" <<'SH'
#!/bin/bash
while [ $# -gt 0 ]; do case "$1" in --*) shift ;; *) break ;; esac; done
setsid "$@" </dev/null >/dev/null 2>&1 &
SH
    # runuser runs its -c command; journalctl prints the cursor in
    # $JOURNAL_CURSOR (empty = no cursor line, like an unreadable journal).
    cat > "$BIN/runuser" <<'SH'
#!/bin/bash
while [ $# -gt 0 ]; do case "$1" in -c) shift; exec bash -c "$1" ;; *) shift ;; esac; done
SH
    cat > "$BIN/journalctl" <<'SH'
#!/bin/bash
[ -n "${JOURNAL_CURSOR:-}" ] && printf -- '-- cursor: %s\n' "$JOURNAL_CURSOR"
exit 0
SH
    chmod +x "$BIN/vm-exec" "$BIN/systemd-run" "$BIN/runuser" "$BIN/journalctl"
    export JOURNAL_CURSOR='s=abc;i=1f;b=00;m=2a;t=5;x=9'
    export PATH="$BIN:$PATH"
    QDWIN_VM_EXEC="$BIN/vm-exec"; VMNAME=fake
    # The guest contract pins result paths under the per-scenario qci dir.
    # On the host a shared /tmp/qci may be owned by another user's run, so
    # the tests redirect the root via NOCT_QCI_DIR.
    mkdir -p "$BATS_TEST_TMPDIR/qci"
    NOCT_QCI_DIR="$BATS_TEST_TMPDIR/qci"
    GDIR=$(mktemp -d "$NOCT_QCI_DIR/bats-idle-wait.XXXXXX")
    CUR="$GDIR/wake.cur"
    DPMS="$BATS_TEST_TMPDIR/dpms"; echo Off > "$DPMS"
    NOCT_DPMS_SYSFS=$DPMS
    NOCT_IDLE_POLL_S=0.2
    NOCT_IDLE_POLL_MAX_S=20
}

teardown() {
    [ -n "${GDIR:-}" ] && rm -rf -- "$GDIR"
}

@test "verdict: Off after the full wait passes" {
    run noct_idle_wait_verdict "dpms=Off waited_s=75"
    [ "$status" -eq 0 ]
    run noct_idle_wait_verdict "dpms=Off waited_s=90"
    [ "$status" -eq 0 ]
}

@test "verdict: a short wait is refused even when it reads Off" {
    run noct_idle_wait_verdict "dpms=Off waited_s=50"
    [ "$status" -eq 1 ]
    [[ "$output" == *"waited only 50s"* ]]
}

@test "verdict: On after the full wait fails" {
    run noct_idle_wait_verdict "dpms=On waited_s=76"
    [ "$status" -eq 1 ]
    [[ "$output" == *"expected Off"* ]]
}

@test "verdict: malformed or empty records fail" {
    for rec in "" "On" "dpms=Off" "dpms=Off waited_s=" "dpms=Off waited_s=x75" "x dpms=Off waited_s=80"; do
        run noct_idle_wait_verdict "$rec"
        [ "$status" -eq 1 ] || { echo "accepted '$rec'" >&2; return 1; }
    done
}

@test "start refuses a result path outside the per-scenario /tmp/qci dir" {
    run noct_idle_wait_start /tmp/x.txt "$CUR"
    [ "$status" -eq 2 ]
}

@test "the guest script carries no double quote into vm-exec" {
    local cmd
    cmd=$(noct_idle_wait_script "$GDIR/r.txt" | base64 -w0)
    [[ "$cmd" != *'"'* ]]
    cmd=$(noct_idle_wait_start_script "$GDIR/r.txt" "$CUR" | base64 -w0)
    [[ "$cmd" != *'"'* ]]
}

@test "start + poll: the detached wait records the state after the wait" {
    NOCT_IDLE_WAIT_S=1
    local res="$GDIR/r-1.txt"
    run noct_idle_wait_start "$res" "$CUR"
    [ "$status" -eq 0 ]
    [ "$(cat "$CUR")" = "$JOURNAL_CURSOR" ]
    run noct_idle_wait_poll "$res"
    [ "$status" -eq 0 ]
    [[ "$output" =~ ^dpms=Off\ waited_s=[0-9]+$ ]]
    run noct_idle_wait_verdict "$output"
    [ "$status" -eq 0 ]
}

@test "start refuses to reuse an existing result path" {
    NOCT_IDLE_WAIT_S=1
    local res="$GDIR/r-2.txt"
    echo "dpms=Off waited_s=99" > "$res"
    run noct_idle_wait_start "$res" "$CUR"
    [ "$status" -ne 0 ]
    [ ! -e "$CUR" ]
}

@test "poll gives up when no record appears" {
    NOCT_IDLE_POLL_MAX_S=1
    run noct_idle_wait_poll "$GDIR/never.txt"
    [ "$status" -eq 1 ]
    [ -z "$output" ]
}

# full-20261012T183404Z-1725371: the driver started the wait without the
# separate cursor block, so steps 3-4 had no wake cursor (ERROR). The cursor
# is now part of the start and cannot be skipped.
@test "start refuses to run without a wake-cursor file" {
    run noct_idle_wait_start "$GDIR/r-3.txt"
    [ "$status" -eq 2 ]
    [[ "$output" == *"wake-cursor file"* ]]
    run noct_idle_wait_start "$GDIR/r-3.txt" relative.cur
    [ "$status" -eq 2 ]
    [ ! -e "$GDIR/r-3.txt" ]
}

@test "start does not launch the wait when the journal cursor is unreadable" {
    NOCT_IDLE_WAIT_S=1
    JOURNAL_CURSOR='' run noct_idle_wait_start "$GDIR/r-4.txt" "$CUR"
    [ "$status" -eq 3 ]
    [[ "$output" == *"idle wait NOT started"* ]]
    [ ! -e "$CUR" ]
    sleep 2
    [ ! -e "$GDIR/r-4.txt" ]
}

@test "start rejects paths outside the safe charset" {
    run noct_idle_wait_start "$GDIR/r 5.txt" "$CUR"
    [ "$status" -eq 2 ]
    run noct_idle_wait_start "$GDIR/r-5.txt" "/tmp/a'b.cur"
    [ "$status" -eq 2 ]
}
