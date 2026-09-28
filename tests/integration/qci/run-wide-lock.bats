#!/usr/bin/env bats
# Host-only contention contract: no libvirt or VM is touched.

setup() {
    REPO=$(cd "$BATS_TEST_DIRNAME/../../.." && pwd)
    T=$BATS_TEST_TMPDIR
    export QDWIN_IMG_DIR="$T/images"
    export QCI_RUNS_DIR="$T/runs"
    mkdir -p "$QDWIN_IMG_DIR"
    cat > "$T/holder" <<'SH'
#!/usr/bin/env bash
. "$RUN_LOCK_HELPER"
qdistro_run_lock_reexec "$0" "$@"
printf '%s\n' ready > "$RUN_LOCK_READY"
exec sleep 30
SH
    chmod +x "$T/holder"
    export RUN_LOCK_HELPER="$REPO/scripts/vm/run-lock.sh"
    export RUN_LOCK_READY="$T/ready"
    "$T/holder" > "$T/holder.log" 2>&1 &
    HOLDER=$!
    for i in $(seq 1 100); do
        [ -f "$RUN_LOCK_READY" ] && break
        sleep 0.01
    done
    [ -f "$RUN_LOCK_READY" ]
}

teardown() {
    kill "$HOLDER" 2>/dev/null || true
    wait "$HOLDER" 2>/dev/null || true
    # Stop the recorded runner as well if the launcher was interrupted before
    # it could forward the signal, so the fixture leaves no sleeping child.
    local pid
    pid=$(cat "$QDWIN_IMG_DIR/.qdistro-vm-run.lock" 2>/dev/null || true)
    [[ "$pid" =~ ^[0-9]+$ ]] && kill "$pid" 2>/dev/null || true
}

@test "qci full is rejected before init_run and names the live holder pid" {
    local lock="$QDWIN_IMG_DIR/.qdistro-vm-run.lock" pid inode
    pid=$(cat "$lock")
    inode=$(stat -c %i "$lock")
    kill -0 "$pid"
    run "$REPO/ci/bin/qci" full
    [ "$status" -eq 98 ]
    [[ "$output" == *"held by pid $pid"* ]]
    [ ! -e "$QCI_RUNS_DIR" ]
    [ "$(stat -c %i "$lock")" = "$inode" ]
}

@test "all qci VM commands and kiwi teardown share the same lock" {
    local command
    for command in bats gui gui-admin image cleanup; do
        run "$REPO/ci/bin/qci" "$command"
        [ "$status" -eq 98 ]
        [ ! -e "$QCI_RUNS_DIR" ]
    done
    run "$REPO/image/build-in-vm.sh" --teardown qdistro-builder-test
    [ "$status" -eq 98 ]
    [[ "$output" == *"held by pid "* ]]
}

@test "background children cannot retain the whole-run lock after runner exits" {
    local pid bgpid lock="$QDWIN_IMG_DIR/.qdistro-vm-run.lock"
    pid=$(cat "$lock")
    kill "$pid"
    for i in $(seq 1 100); do
        flock -n "$lock" true 2>/dev/null && break
        sleep 0.01
    done
    cat > "$T/forker" <<'SH'
#!/usr/bin/env bash
. "$RUN_LOCK_HELPER"
qdistro_run_lock_reexec "$0" "$@"
sleep 30 >/dev/null 2>&1 &
printf '%s\n' "$!" > "$RUN_LOCK_BG_PID"
SH
    chmod +x "$T/forker"
    export RUN_LOCK_BG_PID="$T/bgpid"
    run "$T/forker"
    [ "$status" -eq 0 ]
    bgpid=$(cat "$RUN_LOCK_BG_PID")
    kill -0 "$bgpid"
    flock -n "$lock" true
    kill "$bgpid"
}

@test "the re-executed runner keeps the launcher's SIGPIPE and SIGXFSZ dispositions" {
    # CPython ignores SIGPIPE and SIGXFSZ at startup and execv keeps SIG_IGN.
    # Leaked through the python re-exec, every qci descendant got EFBIG instead
    # of the SIGXFSZ kill vm-exec's `ulimit -f` capture bound relies on
    # (selftest "file-size limit is inherited by a surviving descendant").
    export QDWIN_IMG_DIR="$T/images-sig"
    mkdir -p "$QDWIN_IMG_DIR"
    cat > "$T/sigrunner" <<'SH'
#!/usr/bin/env bash
. "$RUN_LOCK_HELPER"
qdistro_run_lock_reexec "$0" "$@"
awk '$1 == "SigIgn:" { print $2 }' /proc/$$/status > "$1"
# The kernel must kill an over-limit writer, not fail its write.
( ulimit -f 1; head -c 4096 /dev/zero > "$1.big" ) 2>/dev/null
printf '%s\n' "$?" > "$1.rc"
SH
    chmod +x "$T/sigrunner"
    local ign pipe=$((1 << 12)) xfsz=$((1 << 24))
    # A launcher with both signals at their defaults (as from a terminal).
    python3 -c 'import os, signal, sys
for s in (signal.SIGPIPE, signal.SIGXFSZ): signal.signal(s, signal.SIG_DFL)
os.execv(sys.argv[1], sys.argv[1:])' "$T/sigrunner" "$T/dfl"
    ign=$((16#$(cat "$T/dfl")))
    [ $((ign & pipe)) -eq 0 ]
    [ $((ign & xfsz)) -eq 0 ]
    [ "$(cat "$T/dfl.rc")" -eq $((128 + 25)) ]
    # A launcher that really does ignore them (systemd's IgnoreSIGPIPE, a
    # deliberate caller choice) keeps them ignored: restored, not reset.
    python3 -c 'import os, signal, sys
for s in (signal.SIGPIPE, signal.SIGXFSZ): signal.signal(s, signal.SIG_IGN)
os.execv(sys.argv[1], sys.argv[1:])' "$T/sigrunner" "$T/ign"
    ign=$((16#$(cat "$T/ign")))
    [ $((ign & pipe)) -ne 0 ]
    [ $((ign & xfsz)) -ne 0 ]
    [ "$(cat "$T/ign.rc")" -ne $((128 + 25)) ]
}
