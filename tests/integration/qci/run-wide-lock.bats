#!/usr/bin/env bats
# Host-only contention contract: no libvirt or VM is touched.

# Monotonic seconds for readiness deadlines: /proc/uptime, not bash
# SECONDS (wall clock; jumps with NTP/suspend) -- same convention as
# scripts/vm/vm-exec's monotonic_s.
rw_mono_s() {
    local u
    read -r u _ < /proc/uptime 2>/dev/null || return 1
    printf '%s\n' "${u%%.*}"
}

# Poll <command...> until it succeeds or <budget_s> of monotonic time
# passes. These waits are READINESS budgets: they stop scheduling new
# polls at the deadline and return 1, so callers must fail loudly --
# a fixed iteration count that silently falls through once made a
# never-released lock look like a free one.
rw_wait_until() {   # rw_wait_until <budget_s> <interval_s> <command...>
    local budget=$1 interval=$2 start now
    shift 2
    start=$(rw_mono_s) || { echo "cannot read /proc/uptime for a readiness deadline"; return 1; }
    "$@" && return 0
    while :; do
        # Check the deadline before scheduling another poll: sleep can be
        # delayed arbitrarily under load, and a poll started past the
        # deadline would make the budget a lie.
        sleep "$interval"
        now=$(rw_mono_s) || { echo "lost /proc/uptime mid-poll; readiness not established"; return 1; }
        [ $((now - start)) -ge "$budget" ] && return 1
        "$@" && return 0
    done
}

# Full holder readiness: the ready marker is up AND the lock file records a
# numeric pid that is still alive. The two are published by different
# processes (runner vs launcher) so either can lag under load.
rw_ready_pid_live() {
    local pid
    [ -f "$RUN_LOCK_READY" ] || return 1
    pid=$(cat "$QDWIN_IMG_DIR/.qdistro-vm-run.lock" 2>/dev/null) || return 1
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    kill -0 "$pid" 2>/dev/null
}

setup() {
    # A GUI scenario agent's marker would turn the contention checks below into
    # refusals; only the refusal test sets it, explicitly.
    unset QCI_GUI_SCENARIO_AGENT
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
# Outlive the whole bats file: teardown kills this, but a fixed 30s sleep
# could expire mid-file under host load and leave the contention tests
# asserting against a dead holder's stale lock file. 900s still self-cleans
# the lock if the suite itself is SIGKILLed before teardown.
exec sleep 900
SH
    chmod +x "$T/holder"
    export RUN_LOCK_HELPER="$REPO/scripts/vm/run-lock.sh"
    export RUN_LOCK_READY="$T/ready"
    "$T/holder" > "$T/holder.log" 2>&1 &
    HOLDER=$!
    # The launcher publishes `ready` only after acquiring the run lock;
    # under load that took longer than the old nominal 1s poll
    # (selftest qci-bats row, 2026-10-09). And `ready` alone is not full
    # readiness: the holder's runner writes it, while the launcher writes the
    # recorded pid to the lock file separately, so under load `ready` can be
    # visible before the pid lands. Fold the numeric live pid into the same
    # bounded readiness predicate so a race reports "not ready", not
    # `kill -0 ""` (sol impl review r2). If the budget lapses, say why.
    if ! rw_wait_until 15 0.05 rw_ready_pid_live; then
        echo "lock holder not ready+live within 15s; holder log:" >&2
        cat "$T/holder.log" >&2 || true
        echo "lock file contents: $(cat "$QDWIN_IMG_DIR/.qdistro-vm-run.lock" 2>/dev/null || echo '<absent>')" >&2
        return 1
    fi
    # `ready` only proves the holder STARTED; the tests below need it to
    # still own the lock when they run.
    local lockpid
    lockpid=$(cat "$QDWIN_IMG_DIR/.qdistro-vm-run.lock")
    kill -0 "$lockpid"
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

@test "a GUI scenario agent cannot re-enter qci (refused before the lock)" {
    # full-20261001T124446Z apps/13: the luna driver ran `qci gui` on its own
    # VM, hit this lock, then waited on its own codex pid. The refusal must
    # come first (exit 2, not 98) and tell the agent to drive the scenario.
    local command
    for command in full bats gui gui-admin image cleanup; do
        QCI_GUI_SCENARIO_AGENT=apps_13 run "$REPO/ci/bin/qci" "$command"
        [ "$status" -eq 2 ]
        [[ "$output" == *"refused inside GUI scenario agent"* ]]
        [[ "$output" != *"held by pid"* ]]
        [ ! -e "$QCI_RUNS_DIR" ]
    done
    # affected --run reaches the VM gates through gate_affected: refused too,
    # wherever --run sits among the flags.
    local -a args
    for args in "--run" "--vm vmx --run" "--changed-from HEAD --run -- x.md"; do
        # shellcheck disable=SC2086
        QCI_GUI_SCENARIO_AGENT=apps_13 run "$REPO/ci/bin/qci" affected $args
        [ "$status" -eq 2 ]
        [[ "$output" == *"qci affected --run: refused inside GUI scenario agent"* ]]
        [ ! -e "$QCI_RUNS_DIR" ]
    done
    # An operand or a path spelled --run is not the flag: not refused.
    for args in "--vm --run" "--changed-from HEAD -- --run"; do
        # shellcheck disable=SC2086
        QCI_GUI_SCENARIO_AGENT=apps_13 run "$REPO/ci/bin/qci" affected $args
        [[ "$output" != *"refused inside GUI scenario agent"* ]]
    done
}

@test "an inherited scenario-agent marker does not reach the host selftest's bats" {
    grep -Eq 'sf_scrub=\(.*-u QCI_GUI_SCENARIO_AGENT' "$REPO/ci/lib/gates/selftest.sh"
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
    # The flock is released only when the killed holder's fd closes; if the
    # deadline lapses the old loop fell through and the next lines ran
    # against a still-held lock. Fail loudly instead.
    if ! rw_wait_until 15 0.05 flock -n "$lock" true; then
        echo "run lock still held 15s after killing recorded holder pid=$pid"
        false
    fi
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
