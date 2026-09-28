#!/usr/bin/env bats
# Signal lifetime contract for the host-wide VM lock (no libvirt involved).

setup() {
    REPO=$(cd "$BATS_TEST_DIRNAME/../../.." && pwd)
    T=$BATS_TEST_TMPDIR
    export QDWIN_IMG_DIR="$T/images"
    export QCI_RUNS_DIR="$T/runs"
    export RUN_LOCK_HELPER="$REPO/scripts/vm/run-lock.sh"
    export RUN_LOCK_STATE="$T/state"
    export RUN_LOCK_LAUNCHER="$T/launcher-pid"
    export RUN_LOCK_READY="$T/ready"
    # Under qci full/selftest this suite inherits the outer run's guard pid.
    # The runner must be an outermost launcher, or it never records its pid
    # and setup has no process group to signal.
    unset QDISTRO_RUN_LOCK_GUARD_PID
    mkdir -p "$QDWIN_IMG_DIR"
    cat > "$T/runner" <<'SH'
#!/usr/bin/env bash
[ -n "${QDISTRO_RUN_LOCK_GUARD_PID:-}" ] || printf '%s\n' "$$" > "$RUN_LOCK_LAUNCHER"
. "$RUN_LOCK_HELPER"
qdistro_run_lock_reexec "$0" "$@"
trap 'trap - TERM INT HUP; printf "cleanup-start\n" > "$RUN_LOCK_STATE"; sleep 1; printf "cleanup-done\n" > "$RUN_LOCK_STATE"; exit 0' TERM INT HUP
printf '%s\n' ready > "$RUN_LOCK_READY"
while :; do sleep 0.1; done
SH
    chmod +x "$T/runner"
    # A shell background job inherits SIGINT ignored. Reset it before exec so
    # the process-group INT case exercises the runner's trap, not that mask.
    python3 - "$T/runner" > "$T/runner.log" 2>&1 <<'PY' &
import os, signal, sys
os.setsid()
signal.signal(signal.SIGINT, signal.SIG_DFL)
os.execv(sys.argv[1], [sys.argv[1]])
PY
    SESSION=$!
    wait_for_file "$RUN_LOCK_READY"
    LAUNCHER=$(cat "$RUN_LOCK_LAUNCHER")
    PGID=$(ps -o pgid= -p "$LAUNCHER" | tr -d ' ')
    [ -n "$PGID" ]
    [ "$PGID" != "$(ps -o pgid= -p $$ | tr -d ' ')" ]
}

teardown() {
    # SESSION is the setsid leader, so its pid is also the group id. Never
    # wait unbounded: a runner that ignores TERM would hang the whole suite.
    local pg=${PGID:-${SESSION:-}} i
    [ -n "$pg" ] || return 0
    kill -TERM -- "-$pg" 2>/dev/null || true
    for i in $(seq 1 300); do
        kill -0 -- "-$pg" 2>/dev/null || break
        sleep 0.01
    done
    kill -KILL -- "-$pg" 2>/dev/null || true
    [ -n "${SESSION:-}" ] && wait "$SESSION" 2>/dev/null || true
}

wait_for_file() {
    local path=$1 i
    for i in $(seq 1 200); do
        [ -e "$path" ] && return 0
        sleep 0.01
    done
    return 1
}

assert_lock_held_during_cleanup() {
    local lock="$QDWIN_IMG_DIR/.qdistro-vm-run.lock"
    wait_for_file "$RUN_LOCK_STATE"
    [ "$(cat "$RUN_LOCK_STATE")" = cleanup-start ]
    run "$REPO/ci/bin/qci" full
    [ "$status" -eq 98 ]
    [ ! -e "$QCI_RUNS_DIR" ]
    ! flock -n "$lock" true
    for i in $(seq 1 200); do
        [ "$(cat "$RUN_LOCK_STATE")" = cleanup-done ] && break
        sleep 0.01
    done
    [ "$(cat "$RUN_LOCK_STATE")" = cleanup-done ]
    wait "$SESSION"
    flock -n "$lock" true
}

@test "process-group TERM keeps lock through delayed runner cleanup" {
    kill -TERM -- "-$PGID"
    assert_lock_held_during_cleanup
}

@test "process-group INT keeps lock through delayed runner cleanup" {
    kill -INT -- "-$PGID"
    assert_lock_held_during_cleanup
}

@test "process-group HUP keeps lock through delayed runner cleanup" {
    kill -HUP -- "-$PGID"
    assert_lock_held_during_cleanup
}

@test "TERM to original launcher reaches runner and holds lock through cleanup" {
    kill -TERM "$LAUNCHER"
    assert_lock_held_during_cleanup
}
