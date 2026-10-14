#!/usr/bin/env bats
#
# Host-only tests for the guest-side waiter library core
# (ci/lib/guest/gui-waiters.sh). Exercises the bounded-poll engine and the
# generic file/socket waiters against host state — the systemctl/journal/virsh
# wrappers are thin probes over the same _await core tested here. Asserts the
# masking-critical contract: a waiter returns 0 the instant the condition holds,
# and on TIMEOUT fails LOUD with the last observed state + elapsed seconds.

bats_require_minimum_version 1.5.0

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    # The production root driver uses cgroup.kill; host bats has no delegated
    # cgroup and exercises the process-watcher compatibility path.
    export QCI_DRIVER_CLAIM_TEST_PROC_FALLBACK=1
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/guest/gui-waiters.sh"
}

@test "await_file: returns 0 immediately when the file already exists" {
    local f="$BATS_TEST_TMPDIR/here"
    : > "$f"
    run await_file "$f" 2 1
    [ "$status" -eq 0 ]
}

@test "await_file: succeeds when the file appears during the wait" {
    local f="$BATS_TEST_TMPDIR/later"
    ( sleep 1; : > "$f" ) &
    run await_file "$f" 5 1
    [ "$status" -eq 0 ]
    wait
}

@test "await_file: TIMES OUT loudly when the file never appears" {
    run await_file "$BATS_TEST_TMPDIR/never" 1 1
    [ "$status" -ne 0 ]
    [[ "$output" == *"TIMEOUT"* ]]
    [[ "$output" == *"file to exist"* ]]
    [[ "$output" == *"never"* ]]
}

@test "await_socket: distinguishes a socket from a plain file" {
    local plain="$BATS_TEST_TMPDIR/plain"
    : > "$plain"
    # A regular file is NOT a socket -> must time out, not pass.
    run await_socket "$plain" 1 1
    [ "$status" -ne 0 ]
    [[ "$output" == *"TIMEOUT"* ]]
}

@test "_await: reports the probe's last observed state on timeout" {
    # A probe that always fails but echoes a diagnostic line.
    probe() { echo "state=degraded"; return 1; }
    run _await "the thing to be ready" 1 1 probe
    [ "$status" -ne 0 ]
    [[ "$output" == *"last observed: state=degraded"* ]]
}

@test "_await: returns 0 as soon as the probe succeeds (no full wait)" {
    local n="$BATS_TEST_TMPDIR/n"; echo 0 > "$n"
    # Succeeds on the 2nd probe call.
    probe() {
        local c; c=$(cat "$n"); c=$((c + 1)); echo "$c" > "$n"
        [ "$c" -ge 2 ]
    }
    run _await "second try" 10 1 probe
    [ "$status" -eq 0 ]
}

@test "await_system_unit_active: passes when systemctl reports active" {
    systemctl() { [ "$1" = is-active ] && echo active; }
    export -f systemctl
    run await_system_unit_active some.service 2 1
    [ "$status" -eq 0 ]
}

@test "await_system_unit_active: TIMES OUT loudly on a non-active state" {
    systemctl() { [ "$1" = is-active ] && echo activating; }
    export -f systemctl
    run await_system_unit_active some.service 1 1
    [ "$status" -ne 0 ]
    [[ "$output" == *"TIMEOUT"* ]]
    [[ "$output" == *"system unit active: some.service"* ]]
    [[ "$output" == *"state=activating"* ]]
}

@test "await_system_unit_active: succeeds once the unit flips to active mid-wait" {
    local n="$BATS_TEST_TMPDIR/svc"; echo 0 > "$n"
    systemctl() {
        [ "$1" = is-active ] || return 0
        local c; c=$(cat "$n"); c=$((c + 1)); echo "$c" > "$n"
        if [ "$c" -ge 2 ]; then echo active; else echo activating; fi
    }
    export -f systemctl
    run await_system_unit_active some.service 5 1
    [ "$status" -eq 0 ]
}

@test "await_broker_pending_action: requires the exact pending action" {
    dbus-send() {
        printf 'string "app.send-to:3000:org.qdistro.Qnotebook.uid3000"\n'
    }
    export -f dbus-send
    run await_broker_pending_action \
        app.send-to:3000:org.qdistro.Qnotebook.uid3000 2 1
    [ "$status" -eq 0 ]

    run await_broker_pending_action \
        app.send-to:2000:org.qdistro.Qnotebook.uid2000 1 1
    [ "$status" -ne 0 ]
    [[ "$output" == *"TIMEOUT"* ]]
    [[ "$output" == *"app.send-to:3000:org.qdistro.Qnotebook.uid3000"* ]]
}

# ---- qdwin lane window helpers (journal of the running compositor) ---------
# Fixture journal: what qdwin logs for the admin app (handle 3, pid 111), a
# window that renamed itself, one that was removed, and focus moves.
_qdwin_fake_journal() {
    export QDWIN_FAKE_JOURNAL="$BATS_TEST_TMPDIR/journal"
    cat > "$QDWIN_FAKE_JOURNAL" <<'EOF'
[13:00:00.000] qdwin: toplevel_added handle=3 uid=1000 pid=111 app_id=python3 title="admin approvals"
[13:00:00.010] qdwin: mapped handle=3 size=900x600 pos=190,115 (normal)
[13:00:00.020] qdwin: focus handle=3 (was 4294967295) seat=default
[13:00:01.000] qdwin: toplevel_added handle=4 uid=1000 pid=222 app_id=foo title="say "hi" pid=999 x"
[13:00:01.010] qdwin: mapped handle=4 size=100x100 pos=0,0 (normal)
[13:00:02.000] qdwin: toplevel_title handle=3 title="admin approvals (1 pending)"
[13:00:03.000] qdwin: toplevel_added handle=5 uid=1000 pid=333 app_id=old title="admin approvals"
[13:00:04.000] qdwin: toplevel_removed handle=5
[13:00:05.000] qdwin: focus handle=4 (was 3) seat=default
EOF
    qdwin_compositor_pid() { echo 4242; }
    journalctl() { [ "$1" = "_PID=4242" ] || return 1; cat "$QDWIN_FAKE_JOURNAL"; }
    export -f qdwin_compositor_pid journalctl
}

@test "qdwin_windows: live toplevels with their CURRENT titles; removed ones gone" {
    _qdwin_fake_journal
    run qdwin_windows
    [ "$status" -eq 0 ]
    [ "${lines[0]}" = "$(printf '3\t111\t1000\tpython3\tadmin approvals (1 pending)')" ]
    [ "${lines[1]}" = "$(printf '4\t222\t1000\tfoo\tsay "hi" pid=999 x')" ]
    [ "${#lines[@]}" -eq 2 ]
}

@test "qdwin_windows: journal read failure is an error, not an empty window list" {
    _qdwin_fake_journal
    journalctl() { return 1; }
    export -f journalctl
    run qdwin_windows
    [ "$status" -ne 0 ]
    [[ "$output" == *"cannot read compositor journal for pid 4242"* ]]
}

@test "qdwin_window_handle: whole-title match, optional pid filter" {
    _qdwin_fake_journal
    run qdwin_window_handle 'admin approvals \(1 pending\)'
    [ "$status" -eq 0 ]
    [ "$output" = 3 ]
    # The removed handle 5 still has the bare title in the journal; it must
    # not satisfy a live-window lookup, and the prefix must not match either.
    run qdwin_window_handle 'admin approvals'
    [ "$status" -ne 0 ]
    [ -z "$output" ]
    run qdwin_window_handle 'admin approvals.*' 111
    [ "$status" -eq 0 ]
    [ "$output" = 3 ]
    run qdwin_window_handle 'admin approvals.*' 222
    [ "$status" -ne 0 ]
}

@test "await_qdwin_window_title: OK on the state, TIMEOUT names every live title" {
    _qdwin_fake_journal
    run await_qdwin_window_title 'admin approvals \(1 pending\)' 2 1
    [ "$status" -eq 0 ]
    run await_qdwin_window_title 'admin approvals' 1 1
    [ "$status" -ne 0 ]
    [[ "$output" == *"TIMEOUT"* ]]
    [[ "$output" == *"3=admin approvals (1 pending)"* ]]
    run await_qdwin_window_title '' 1 1
    [ "$status" -eq 2 ]
}

@test "await_qdwin_window_mapped_pid: needs a mapped line for THAT pid's handle" {
    _qdwin_fake_journal
    run await_qdwin_window_mapped_pid 111 2 1
    [ "$status" -eq 0 ]
    [[ "$output" == *"mapped handle=3 pid=111"* ]]
    # Handle 4's TITLE contains "pid=999": its pid is still 222.
    run await_qdwin_window_mapped_pid 222 2 1
    [ "$status" -eq 0 ]
    [[ "$output" == *"mapped handle=4 pid=222"* ]]
    run await_qdwin_window_mapped_pid 999 1 1
    [ "$status" -ne 0 ]
    # pid 333 was added (handle 5) but never mapped.
    run await_qdwin_window_mapped_pid 333 1 1
    [ "$status" -ne 0 ]
    [[ "$output" == *"no mapped toplevel for pid 333"* ]]
}

@test "qdwin_focused_handle: the last focus line wins" {
    _qdwin_fake_journal
    run qdwin_focused_handle
    [ "$status" -eq 0 ]
    [ "$output" = 4 ]
}

@test "qdwin_focus_window: asks qdshell, returns only once focus is on the window" {
    _qdwin_fake_journal
    # Focus starts on handle 4; the fake qs moves it to the requested handle.
    runuser() {
        local a; for a in "$@"; do :; done
        printf '[13:00:09.000] qdwin: focus handle=%s (was 4) seat=default\n' "$a" >> "$QDWIN_FAKE_JOURNAL"
        echo "$*" >> "$BATS_TEST_TMPDIR/qs-calls"
    }
    export -f runuser
    run qdwin_focus_window 'admin approvals.*' 5
    [ "$status" -eq 0 ]
    [ "${lines[-1]}" = 3 ]
    grep -q 'qs ipc -p /usr/share/quickshell/qdshell call qdwin focusWindow 3' "$BATS_TEST_TMPDIR/qs-calls"
}

@test "qdwin_focus_window: fails loudly when focus never lands" {
    _qdwin_fake_journal
    runuser() { :; }
    export -f runuser
    run qdwin_focus_window 'admin approvals.*' 1
    [ "$status" -ne 0 ]
    [[ "$output" == *"focus stayed on handle 4, not 3"* ]]
    run qdwin_focus_window 'no such window' 1
    [ "$status" -ne 0 ]
    [[ "$output" == *"no live window titled"* ]]
}

@test "await_broker_pending_action: rejects an empty action" {
    run await_broker_pending_action "" 1 1
    [ "$status" -eq 2 ]
    [[ "$output" == *"must be non-empty"* ]]
}

@test "broker_pending_count: prints exactly the element count of GetPending" {
    busctl() {
        [ "$*" = "--system call org.qdistro.AdminBroker1 /org/qdistro/AdminBroker1 org.qdistro.AdminBroker1 GetPending" ] || return 9
        printf '%s\n' "$BUSCTL_REPLY"
    }
    export -f busctl
    BUSCTL_REPLY='aa{sv} 0' run broker_pending_count
    [ "$status" -eq 0 ]
    [ "$output" = 0 ]
    BUSCTL_REPLY='aa{sv} 2 1 "id" i 7 1 "id" i 8' run broker_pending_count
    [ "$status" -eq 0 ]
    [ "$output" = 2 ]
}

@test "broker_pending_count: never prints a count for an unexpected reply" {
    busctl() { printf '%s\n' "$BUSCTL_REPLY"; return "${BUSCTL_RC:-0}"; }
    export -f busctl
    local bad
    for bad in 'as 0' 'aa{sv}' 'aa{sv} x' '' 'Call failed: Access denied'; do
        BUSCTL_REPLY=$bad run --separate-stderr broker_pending_count
        [ "$status" -eq 1 ]
        [ -z "$output" ]
        [[ "$stderr" == *"unexpected GetPending reply"* ]]
    done
    BUSCTL_REPLY='' BUSCTL_RC=1 run --separate-stderr broker_pending_count
    [ "$status" -eq 1 ]
    [ -z "$output" ]
    [[ "$stderr" == *"GetPending failed"* ]]
}

@test "qci_sqlite: passes SQL containing % to sqlite3 as one verbatim argv word" {
    sqlite3() { printf '<%s>\n' "$@"; }
    export -f sqlite3
    local sql="SELECT decision, scope FROM audit WHERE action LIKE 'app.send-to:%' ORDER BY id DESC LIMIT 1;"
    run qci_sqlite audit "$sql"
    [ "$status" -eq 0 ]
    [ "$output" = "$(printf '<%s>\n' -batch /var/lib/qdistro/audit/audit.sqlite "$sql")" ]
    run qci_sqlite approvals "DELETE FROM approvals WHERE action LIKE 'qsu.exec:%';"
    [ "$status" -eq 0 ]
    [[ "$output" == *"<-batch>"*"</var/lib/qdistro/approvals/approvals.sqlite>"*"<DELETE FROM approvals WHERE action LIKE 'qsu.exec:%';>" ]]
}

@test "qci_sqlite: runs a real LIKE '%' query when sqlite3 is present" {
    command -v sqlite3 >/dev/null || skip "sqlite3 not installed"
    local db="$BATS_TEST_TMPDIR/audit.sqlite"
    sqlite3 "$db" "CREATE TABLE audit(id INTEGER PRIMARY KEY, action TEXT, decision INT, scope TEXT);
INSERT INTO audit(action,decision,scope) VALUES ('app.send-to:3000:x',0,'1h'),('other',1,'forever'),('app.send-to:3000:x',1,'once');"
    run qci_sqlite "$db" "SELECT decision, scope FROM audit WHERE action LIKE 'app.send-to:%' ORDER BY id DESC LIMIT 1;"
    [ "$status" -eq 0 ]
    [ "$output" = "1|once" ]
}

@test "qci_sqlite: rejects an unknown database alias and a missing SQL argument" {
    sqlite3() { echo called; }
    export -f sqlite3
    run --separate-stderr qci_sqlite audits "SELECT 1;"
    [ "$status" -eq 2 ]
    [ -z "$output" ]
    run --separate-stderr qci_sqlite audit
    [ "$status" -eq 2 ]
    [ -z "$output" ]
    run --separate-stderr qci_sqlite audit "SELECT 1;" extra
    [ "$status" -eq 2 ]
    [ -z "$output" ]
}

@test "qci_claim_driver: a lock under the scratch root must be an existing <slug>/driver.lock" {
    local root="$BATS_TEST_TMPDIR/scratch"
    mkdir -p "$root/the_slug.md"
    export QCI_GUEST_SCRATCH_ROOT=$root
    run _qci_claim_scenario_path "$root/the_slug.md/driver.lock"
    [ "$status" -eq 0 ]
    run _qci_claim_scenario_path "$BATS_TEST_TMPDIR/elsewhere/driver.lock"
    [ "$status" -eq 0 ]
    local bad
    for bad in "$root/qdistro/the_slug.md/driver.lock" "$root/other.md/driver.lock" \
        "$root/the_slug.md/other.lock" "$root/driver.lock" "$root/../driver.lock" \
        "$root/./driver.lock" "$BATS_TEST_TMPDIR//scratch/qdistro/the_slug.md/driver.lock" \
        "$BATS_TEST_TMPDIR/./scratch/qdistro/the_slug.md/driver.lock" \
        "$BATS_TEST_TMPDIR//scratch/the_slug.md/driver.lock"; do
        run _qci_claim_scenario_path "$bad"
        [ "$status" -eq 1 ]
        [[ "$output" == *"is not a scenario lock"* ]]
        [[ "$output" == *"existing: the_slug.md"* ]]
    done
    # The whole claim stops with exit 2 and creates nothing.
    run bash -c '
        source "$1"
        qci_claim_driver "$2"
        echo RAN
    ' _ "$REPO_ROOT/ci/lib/guest/gui-waiters.sh" "$root/qdistro/the_slug.md/driver.lock"
    [ "$status" -eq 2 ]
    [[ "$output" != *RAN* ]]
    [ ! -e "$root/qdistro" ]
}

@test "await_domain_gone: passes when the domain is absent (virsh errors)" {
    virsh() { return 1; }   # domstate on an undefined domain errors, empty stdout
    export -f virsh
    run await_domain_gone gone-dom 2 1
    [ "$status" -eq 0 ]
}

@test "await_domain_gone: passes on a terminal non-running state" {
    virsh() { [ "$1" = domstate ] && echo "shut off"; }
    export -f virsh
    run await_domain_gone dom 2 1
    [ "$status" -eq 0 ]
}

@test "await_domain_gone: accepts a crashed domain as reaped (terminal)" {
    virsh() { [ "$1" = domstate ] && echo crashed; }
    export -f virsh
    run await_domain_gone dom 2 1
    [ "$status" -eq 0 ]
}

@test "await_domain_gone: TIMES OUT loudly while the domain is still running" {
    virsh() { [ "$1" = domstate ] && echo running; }
    export -f virsh
    run await_domain_gone dom 1 1
    [ "$status" -ne 0 ]
    [[ "$output" == *"TIMEOUT"* ]]
    [[ "$output" == *"domain reaped (absent or terminally stopped): dom"* ]]
    [[ "$output" == *"domstate=running"* ]]
}

@test "await_domain_gone: does NOT accept a live/transitional state (paused) as reaped" {
    # paused/pmsuspended/blocked are non-running but the domain still exists —
    # they must NOT satisfy the reap check.
    virsh() { [ "$1" = domstate ] && echo paused; }
    export -f virsh
    run await_domain_gone dom 1 1
    [ "$status" -ne 0 ]
    [[ "$output" == *"TIMEOUT"* ]]
    [[ "$output" == *"domstate=paused"* ]]
}

@test "await_domain_gone: succeeds once the domain stops running mid-wait" {
    local n="$BATS_TEST_TMPDIR/dom"; echo 0 > "$n"
    virsh() {
        [ "$1" = domstate ] || return 0
        local c; c=$(cat "$n"); c=$((c + 1)); echo "$c" > "$n"
        if [ "$c" -ge 2 ]; then echo "shut off"; else echo running; fi
    }
    export -f virsh
    run await_domain_gone dom 5 1
    [ "$status" -eq 0 ]
}

# --- success must be OBSERVABLE -------------------------------------------
# Regression for permissions-gui/59 S2 (run full-20260909T224527Z): the broker
# HAD logged `lineage_enforce=True` 1s after the restart, but the waiter
# returned 0 in silence, the step was graded by grepping the command's stdout,
# and an empty stdout was read as a product failure. A passing gate must say so.

@test "_await: announces SUCCESS on stdout with the probe's observation" {
    probe() { echo "matched: [broker] lineage_enforce=True"; return 0; }
    run _await "the posture line" 2 1 probe
    [ "$status" -eq 0 ]
    [[ "$output" == *"[await] OK"* ]]
    [[ "$output" == *"the posture line"* ]]
    [[ "$output" == *"matched: [broker] lineage_enforce=True"* ]]
}

@test "_await: success line goes to STDOUT (not stderr)" {
    probe() { echo "ready=yes"; return 0; }
    local out err
    out=$(_await "the thing" 2 1 probe 2>"$BATS_TEST_TMPDIR/err")
    err=$(cat "$BATS_TEST_TMPDIR/err")
    [[ "$out" == *"[await] OK"* ]]
    [[ "$out" == *"ready=yes"* ]]
    [ -z "$err" ]
}

@test "await_journal_line_after_cursor: prints the matched journal line on success" {
    journalctl() { echo "[broker] lineage_enforce=True (False=shadow/audit-only)"; }
    export -f journalctl
    run await_journal_line_after_cursor "s=cur;i=1" "lineage_enforce=True" 2 1 \
        -u qdistro-admin-broker.service
    [ "$status" -eq 0 ]
    [[ "$output" == *"lineage_enforce=True"* ]]
}

@test "await_journal_line_after_cursor: a shadow-mode line does NOT satisfy the enforce pattern" {
    journalctl() { echo "[broker] lineage_enforce=False (False=shadow/audit-only)"; }
    export -f journalctl
    run await_journal_line_after_cursor "s=cur;i=1" "lineage_enforce=True" 1 1 \
        -u qdistro-admin-broker.service
    [ "$status" -ne 0 ]
    [[ "$output" == *"TIMEOUT"* ]]
}

@test "_await: QCI_AWAIT_QUIET=1 suppresses the success announcement only" {
    probe() { echo "ready=yes"; return 0; }
    QCI_AWAIT_QUIET=1 run _await "the thing" 2 1 probe
    [ "$status" -eq 0 ]
    [ -z "$output" ]
    # A TIMEOUT is still loud even under quiet.
    probe() { echo "state=degraded"; return 1; }
    QCI_AWAIT_QUIET=1 run _await "the thing" 1 1 probe
    [ "$status" -ne 0 ]
    [[ "$output" == *"TIMEOUT"* ]]
    [[ "$output" == *"state=degraded"* ]]
}

@test "_await: a chatty probe's observation is capped with an ANNOUNCED truncation" {
    probe() { seq 1 50; return 0; }
    QCI_AWAIT_OBSERVED_MAX_LINES=5 run _await "the chatty thing" 2 1 probe
    [ "$status" -eq 0 ]
    [[ "$output" == *"[await] OK"* ]]
    [[ "$output" == *"truncated, 45 more line(s)"* ]]
    [[ "$output" != *"50"* ]]
}

# --- Regressions for the observation-cap defects found in codex sol review ---
# (todo/reviews/out-59-qga.md). The pre-existing cap test above uses 50 SHORT
# lines, which fit in the pipe buffer, so it could not catch either bug.

@test "_await: a large success under set -euo pipefail does NOT become a failure" {
    # The original cap used `printf | head`, which closes the read end early.
    # printf then takes SIGPIPE (141) and, under pipefail, that 141 propagated
    # out of a SUCCESSFUL wait. Output must exceed the pipe buffer (64 KiB) for
    # the race to bite, hence 200k lines rather than 50.
    run bash -c '
        set -euo pipefail
        . '"$REPO_ROOT/ci/lib/guest/gui-waiters.sh"'
        QCI_AWAIT_OBSERVED_MAX_LINES=5
        big() { seq 1 200000; }
        _await "huge" 5 1 big
        echo "SURVIVED rc=$?"
    '
    [ "$status" -eq 0 ]
    [[ "$output" == *"SURVIVED rc=0"* ]]
    [[ "$output" == *"[await] OK after"* ]]
    [[ "$output" == *"truncated, 199995 more line(s)"* ]]
}

@test "_await: a single oversized line is byte-capped, not emitted whole" {
    # The line cap alone is defeated by one long line (e.g. a whole dbus reply),
    # which is the case that can reach qga's per-stream capture cap.
    run bash -c '
        set -euo pipefail
        . '"$REPO_ROOT/ci/lib/guest/gui-waiters.sh"'
        QCI_AWAIT_OBSERVED_MAX_BYTES=200
        oneline() { printf "x%.0s" $(seq 1 200000); echo; }
        _await "oneline" 5 1 oneline
    '
    [ "$status" -eq 0 ]
    [[ "$output" == *"more byte(s)"* ]]
    # Whole-line emission was 200048 bytes; the cap must hold it far below that.
    [ "${#output}" -lt 1000 ]
}

@test "_await: a malformed cap falls back instead of aborting a passing wait" {
    run bash -c '
        set -euo pipefail
        . '"$REPO_ROOT/ci/lib/guest/gui-waiters.sh"'
        QCI_AWAIT_OBSERVED_MAX_LINES=notanumber
        QCI_AWAIT_OBSERVED_MAX_BYTES=-5
        _await "smallprobe" 5 1 echo hello
    '
    [ "$status" -eq 0 ]
    [[ "$output" == *"[await] observed: hello"* ]]
}

@test "_await: a digit-only but malformed cap cannot abort a passing wait" {
    # Round-2 review: digit-only is not enough. "08" is octal in $(( )), "000"
    # is zero, and an overlong digit string overflows a 64-bit shell integer.
    # Each of these previously either aborted a SUCCESSFUL waiter under
    # `set -euo pipefail` or silently applied a cap of zero.
    local v
    for v in 08 000 0000000000000000000000000000 ; do
        run bash -c '
            set -euo pipefail
            . '"$REPO_ROOT/ci/lib/guest/gui-waiters.sh"'
            QCI_AWAIT_OBSERVED_MAX_LINES='"$v"'
            multi() { printf "l1\nl2\nl3\n"; }
            _await "capped" 5 1 multi
            echo "SURVIVED"
        '
        [ "$status" -eq 0 ]
        [[ "$output" == *"SURVIVED"* ]]
        [[ "$output" == *"[await] observed: l1"* ]]
    done
}

@test "_await_positive_int: canonicalizes and rejects per contract" {
    source "$REPO_ROOT/ci/lib/guest/gui-waiters.sh"
    [ "$(_await_positive_int 08 20)" = "8" ]      # octal-looking -> base 10
    [ "$(_await_positive_int 000 20)" = "20" ]    # zero -> fallback
    [ "$(_await_positive_int 0 20)" = "20" ]
    [ "$(_await_positive_int "" 20)" = "20" ]
    [ "$(_await_positive_int abc 20)" = "20" ]
    [ "$(_await_positive_int 5 20)" = "5" ]
    [ "$(_await_positive_int 999999999999999999999999 20)" = "20" ]  # overflow
}

# --- bg_start / bg_wait / bg_rc / bg_log -------------------------------------
#
# These pin the replacement for `wait $(cat X.pid)`, which does not wait at all
# when the producer and the consumer are separate guest shells. The first test
# below reproduces that defect directly, so the reason this API exists is
# itself under test rather than only asserted in a comment.

bg_setup() {
    export QCI_BG_DIR="$BATS_TEST_TMPDIR/bg"
    mkdir -p "$QCI_BG_DIR"
}

@test "bg: the OLD pid-wait idiom reads an empty log — the defect being replaced" {
    bg_setup
    # Producer in one shell, consumer in another, exactly as two vm-exec calls.
    bash -c "{ sleep 3; echo PAYLOAD; } >'$QCI_BG_DIR/old.log' 2>&1 & echo \$! >'$QCI_BG_DIR/old.pid'"
    run bash -c "wait \$(cat '$QCI_BG_DIR/old.pid') 2>/dev/null; cat '$QCI_BG_DIR/old.log'"
    # `wait` returns instantly on a non-child, so the log is still empty.
    [ "$output" = "" ]
    wait
}

@test "bg: bg_wait does block for a job the old idiom would have raced" {
    bg_setup
    bg_start slow - 'sleep 3; echo PAYLOAD'
    # Same timing as the test above, where the pid-wait read nothing.
    [ "$(cat "$QCI_BG_DIR/slow.log" 2>/dev/null)" = "" ]
    QCI_AWAIT_QUIET=1 run bg_wait slow 30 1
    [ "$status" -eq 0 ]
    [ "$(bg_log slow)" = "PAYLOAD" ]
}

@test "bg: bg_start does NOT hold the caller's stdout/stderr open (vm-exec returns)" {
    bg_setup
    # vm-exec runs each command through qga guest-exec with capture-output, and
    # qga reports the command finished only once EVERY holder of its stdout and
    # stderr pipes has closed them. A background job that inherits those fds
    # therefore pins the launching vm-exec until the JOB exits: `bg_start` of a
    # qsu request that waits for an approval hung its own vm-exec until the
    # approval that could only come after it (permissions-gui/44 and /46,
    # full-20260922T193137Z-881799). A command substitution reads to EOF the
    # same way, so it measures exactly that.
    local start=$SECONDS out
    out=$(bash -c "source '$REPO_ROOT/ci/lib/guest/gui-waiters.sh'; QCI_BG_DIR='$QCI_BG_DIR' bg_start det - 'sleep 20; echo late'; echo launched" 2>&1)
    [ "$out" = "launched" ]
    [ $((SECONDS - start)) -lt 10 ]
    # The job itself is still running and still reports through its own files.
    [ ! -e "$QCI_BG_DIR/det.rc" ]
    [ -s "$QCI_BG_DIR/det.pid" ]
    kill "$(cat "$QCI_BG_DIR/det.pid")" 2>/dev/null || true
}

@test "bg: the job's exit status is recorded and readable" {
    bg_setup
    bg_start rc7 - 'exit 7'
    QCI_AWAIT_QUIET=1 bg_wait rc7 20 1
    [ "$(bg_rc rc7)" = "7" ]
    # bg_rc's OWN status says only whether a record could be read. Returning
    # the job's status here instead would make a job that exited 1
    # indistinguishable from a job that never ran, and would abort a caller
    # running under `set -e` on a legitimately failing command.
    run bg_rc rc7
    [ "$status" -eq 0 ]
    [ "$output" = "7" ]
}

@test "bg: QCI_BG_STDERR keeps stderr OUT of the stdout log" {
    bg_setup
    local err="$BATS_TEST_TMPDIR/split.err"
    QCI_BG_STDERR="$err" bg_start split - 'echo wanted; echo loader-warning >&2'
    QCI_AWAIT_QUIET=1 bg_wait split 20 1
    [ "$(bg_log split)" = "wanted" ]
    [[ "$(cat "$err")" == *"loader-warning"* ]]
    [ "$(bg_rc split)" = "0" ]
}

@test "bg: bg_log <tag> <lines> truncates without a pipeline" {
    bg_setup
    bg_start many - 'for i in 1 2 3 4 5; do echo "line$i"; done'
    QCI_AWAIT_QUIET=1 bg_wait many 20 1
    [ "$(bg_log many 2)" = "line1
line2" ]
    # The same read under pipefail must not become a failure, which is what a
    # `bg_log many | head -2` call site would risk.
    run bash -c "set -euo pipefail; source '$REPO_ROOT/ci/lib/guest/gui-waiters.sh'; QCI_BG_DIR='$QCI_BG_DIR' bg_log many 2"
    [ "$status" -eq 0 ]
}

@test "bg: an exit inside the command still records a status" {
    bg_setup
    # A bare `exit` would leave the whole background shell if the command were
    # not run in its own subshell, so no .rc would ever be written and bg_wait
    # would hang until its deadline.
    bg_start ex - 'echo before; exit 3; echo after'
    QCI_AWAIT_QUIET=1 run bg_wait ex 20 1
    [ "$status" -eq 0 ]
    [ "$(bg_rc ex)" = "3" ]
    [ "$(bg_log ex)" = "before" ]
}

@test "bg: a multi-command string sends ALL of its output to the log" {
    bg_setup
    # `cmd > log` binds the redirect to the LAST command of a list; the whole
    # list must be grouped or earlier output escapes to the caller's stdout.
    bg_start multi - 'echo one; echo two; echo three'
    QCI_AWAIT_QUIET=1 bg_wait multi 20 1
    [ "$(bg_log multi)" = "one
two
three" ]
}

@test "bg: stderr is captured in the SAME log as stdout" {
    bg_setup
    bg_start err - 'echo out; echo problem >&2'
    QCI_AWAIT_QUIET=1 bg_wait err 20 1
    # Both halves, or "same log" is not what is being shown.
    [[ "$(bg_log err)" == *"out"* ]]
    [[ "$(bg_log err)" == *"problem"* ]]
}

@test "bg: a command ending in a COMMENT does not corrupt the wrapper" {
    bg_setup
    # Interpolating the command into the script source would let this comment
    # swallow the closing paren and the exit-status record with it; bg_wait
    # would then block to its deadline for a job that had already finished.
    bg_start cmt - 'echo done  # why this command exists'
    QCI_AWAIT_QUIET=1 run bg_wait cmt 20 1
    [ "$status" -eq 0 ]
    [ "$(bg_log cmt)" = "done" ]
    [ "$(bg_rc cmt)" = "0" ]
}

@test "bg: a job directory with spaces and metacharacters still works" {
    # The generated script %q-quotes every path it names; an unquoted one
    # would split this directory into several words and write nowhere useful.
    export QCI_BG_DIR="$BATS_TEST_TMPDIR/od d & \$x"
    mkdir -p "$QCI_BG_DIR"
    bg_start q - 'echo hello; exit 2'
    QCI_AWAIT_QUIET=1 run bg_wait q 20 1
    [ "$status" -eq 0 ]
    [ "$(bg_log q)" = "hello" ]
    [ "$(bg_rc q)" = "2" ]
}

@test "bg: bg_wait TIMES OUT loudly when the job never finishes" {
    bg_setup
    bg_start hang - 'sleep 30'
    run bg_wait hang 1 1
    [ "$status" -ne 0 ]
    [[ "$output" == *"TIMEOUT"* ]]
    [[ "$output" == *"hang"* ]]
}

@test "bg: bg_rc reports ABSENCE separately from a job that exited nonzero" {
    bg_setup
    bg_start none - 'sleep 30'
    run bg_rc none
    [ "$status" -eq 1 ]
    [ "$output" != "" ]
    [[ "$output" == *"no exit status recorded"* ]]
}

@test "bg: a stale record from an earlier job with the same tag cannot satisfy bg_wait" {
    bg_setup
    bg_start reuse - 'echo first'
    QCI_AWAIT_QUIET=1 bg_wait reuse 20 1
    [ "$(bg_rc reuse)" = "0" ]
    bg_start reuse - 'sleep 2; echo second; exit 5'
    # The previous run's .rc must be gone the moment the new job starts,
    # or bg_wait returns instantly with the OLD job's status.
    [ ! -e "$QCI_BG_DIR/reuse.rc" ]
    QCI_AWAIT_QUIET=1 bg_wait reuse 20 1
    [ "$(bg_rc reuse)" = "5" ]
    [ "$(bg_log reuse)" = "second" ]
}

@test "bg: a tag that would escape the job directory is refused" {
    bg_setup
    run bg_start "../escape" - 'true'
    [ "$status" -eq 2 ]
    [[ "$output" == *"invalid tag"* ]]
    run bg_start "" - 'true'
    [ "$status" -eq 2 ]
    run bg_wait "a b" 1 1
    [ "$status" -eq 2 ]
}

@test "bg: bg_log reports a missing log loudly instead of as empty output" {
    bg_setup
    run bg_log never-started
    [ "$status" -eq 1 ]
    [[ "$output" == *"no log"* ]]
}

# --- qci_claim_driver --------------------------------------------------------
#
# The lock has to live in the calling shell. These tests use separate bash
# processes (the way two vm-execs are separate guest shells), not `run` of
# the function inside the bats process: `run` is a subshell, and a subshell
# that exits drops the flock.

_claim_hold() {
    # Block in this shell (builtin read), so the holder has no child that
    # could inherit the lock fd and keep it after the shell is killed.
    local lib=$1 lock=$2 ready=$3 fifo=$4
    bash -c '
        set -euo pipefail
        # shellcheck disable=SC1090
        source "$1"
        qci_claim_driver "$2"
        : > "$3"
        exec 3<>"$4"
        read -t 30 -u 3 || true
    ' _ "$lib" "$lock" "$ready" "$fifo" &
    CLAIM_PID=$!
    local i
    for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
        [ -f "$ready" ] && return 0
        sleep 0.1
    done
    echo "holder did not become ready" >&2
    return 1
}

@test "qci_claim_driver: a second concurrent shell fails and does no side effect" {
    local lock="$BATS_TEST_TMPDIR/qci/permissions-gui/driver.lock"
    local side="$BATS_TEST_TMPDIR/second-side"
    local ready="$BATS_TEST_TMPDIR/ready"
    local fifo="$BATS_TEST_TMPDIR/hold"
    mkfifo "$fifo"
    [ ! -e "$lock" ]
    # No RETURN trap: under bats that trap also fires when this helper
    # returns, which would drop the lock before the contender runs.
    _claim_hold "$REPO_ROOT/ci/lib/guest/gui-waiters.sh" "$lock" "$ready" "$fifo" \
        || { kill "$CLAIM_PID" 2>/dev/null || true; wait "$CLAIM_PID" 2>/dev/null || true; return 1; }
    # No set -e in the contender: the claim itself must stop that shell
    # before the side effect. timeout(1) bounds a blocking flock; 124 would
    # mean we killed it for hanging rather than it exiting on its own (the
    # contender retries for QCI_DRIVER_CLAIM_GRACE, 2s, before refusing).
    run timeout 10 bash -c '
        # shellcheck disable=SC1090
        source "$1"
        qci_claim_driver "$2"
        echo RAN > "$3"
    ' _ "$REPO_ROOT/ci/lib/guest/gui-waiters.sh" "$lock" "$side"
    kill "$CLAIM_PID" 2>/dev/null || true
    wait "$CLAIM_PID" 2>/dev/null || true
    [ "$status" -eq 1 ]
    [ "${lines[0]}" = "ERROR: a second guest driver is already running: $lock" ]
    [ ! -e "$side" ]
    [ -f "$lock" ]
}

@test "qci_claim_driver: after the holder shell exits a later claim succeeds" {
    local lock="$BATS_TEST_TMPDIR/qci/permissions-gui/driver.lock"
    local side="$BATS_TEST_TMPDIR/side"
    bash -c '
        # shellcheck disable=SC1090
        source "$1"
        qci_claim_driver "$2"
    ' _ "$REPO_ROOT/ci/lib/guest/gui-waiters.sh" "$lock"
    run bash -c '
        # shellcheck disable=SC1090
        source "$1"
        qci_claim_driver "$2"
        echo RAN > "$3"
    ' _ "$REPO_ROOT/ci/lib/guest/gui-waiters.sh" "$lock" "$side"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
    [ "$(cat "$side")" = "RAN" ]
}

@test "qci_claim_driver: sourcing the library does not hold the lock" {
    local lock="$BATS_TEST_TMPDIR/qci/slug/driver.lock"
    local side="$BATS_TEST_TMPDIR/side"
    local ready="$BATS_TEST_TMPDIR/ready"
    local fifo="$BATS_TEST_TMPDIR/hold"
    local default_lock=/tmp/qci-driver.lock
    local before after
    mkfifo "$fifo"
    if [ -e "$default_lock" ]; then
        before=$(stat -c %Y "$default_lock")
    else
        before=absent
    fi
    bash -c '
        # shellcheck disable=SC1090
        source "$1"
        : > "$2"
        exec 3<>"$3"
        read -t 30 -u 3 || true
    ' _ "$REPO_ROOT/ci/lib/guest/gui-waiters.sh" "$ready" "$fifo" &
    local pid=$!
    local i
    for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
        [ -f "$ready" ] && break
        sleep 0.1
    done
    if [ ! -f "$ready" ]; then
        kill "$pid" 2>/dev/null || true
        wait "$pid" 2>/dev/null || true
        echo "sourcer did not become ready" >&2
        return 1
    fi
    run bash -c '
        # shellcheck disable=SC1090
        source "$1"
        qci_claim_driver "$2"
        echo RAN > "$3"
    ' _ "$REPO_ROOT/ci/lib/guest/gui-waiters.sh" "$lock" "$side"
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
    [ "$status" -eq 0 ]
    [ "$(cat "$side")" = "RAN" ]
    if [ -e "$default_lock" ]; then
        after=$(stat -c %Y "$default_lock")
    else
        after=absent
    fi
    [ "$before" = "$after" ]
}

@test "qci_claim_driver: a second claim of the same path in the same shell succeeds and keeps the lock" {
    local lock="$BATS_TEST_TMPDIR/qci/slug/driver.lock"
    local side="$BATS_TEST_TMPDIR/second-side"
    local ready="$BATS_TEST_TMPDIR/ready"
    local fifo="$BATS_TEST_TMPDIR/hold"
    mkfifo "$fifo"
    # The holder claims twice and re-sources under set -e before signalling.
    # Ready means both same-shell claims returned 0 and the re-source did not
    # abort the shell. The contender must still lose: the second claim did
    # not drop or replace the lock.
    bash -c '
        set -euo pipefail
        # shellcheck disable=SC1090
        source "$1"
        qci_claim_driver "$2"
        qci_claim_driver "$2"
        # shellcheck disable=SC1090
        source "$1"
        : > "$3"
        exec 3<>"$4"
        read -t 30 -u 3 || true
    ' _ "$REPO_ROOT/ci/lib/guest/gui-waiters.sh" "$lock" "$ready" "$fifo" &
    local pid=$!
    local i
    for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
        [ -f "$ready" ] && break
        sleep 0.1
    done
    if [ ! -f "$ready" ]; then
        kill "$pid" 2>/dev/null || true
        wait "$pid" 2>/dev/null || true
        echo "holder did not become ready" >&2
        return 1
    fi
    run timeout 10 bash -c '
        # shellcheck disable=SC1090
        source "$1"
        qci_claim_driver "$2"
        echo RAN > "$3"
    ' _ "$REPO_ROOT/ci/lib/guest/gui-waiters.sh" "$lock" "$side"
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
    [ "$status" -eq 1 ]
    [ "${lines[0]}" = "ERROR: a second guest driver is already running: $lock" ]
    [ ! -e "$side" ]
}

@test "qci_claim_driver: a subshell that exits is not the lock holder" {
    local lock="$BATS_TEST_TMPDIR/qci/slug/driver.lock"
    local side="$BATS_TEST_TMPDIR/side"
    bash -c '
        # shellcheck disable=SC1090
        source "$1"
        ( qci_claim_driver "$2" )
        echo PARENT_CONTINUED > "$3"
    ' _ "$REPO_ROOT/ci/lib/guest/gui-waiters.sh" "$lock" "$side"
    [ "$(cat "$side")" = "PARENT_CONTINUED" ]
    run bash -c '
        # shellcheck disable=SC1090
        source "$1"
        qci_claim_driver "$2"
        echo RAN
    ' _ "$REPO_ROOT/ci/lib/guest/gui-waiters.sh" "$lock"
    [ "$status" -eq 0 ]
    [ "$output" = "RAN" ]
}

# --- qci_claim_driver: the claim belongs to the driver SHELL -----------------
#
# full-20260926T153217Z-3807077: permissions-gui/44 and qdwin gui/16 each had a
# driver exit (a waiter timed out) while an app it had launched lived on
# (qdistro-start-admin-app's admin app, reparented to init; setsid'd
# qdistro-test-window, which does not die on SIGTERM while it idles in
# wl_display_dispatch). The claim used to be an fd of the driver shell, and
# every child inherited it, so those apps held driver.lock with no driver
# alive and every retry was refused "a second guest driver is already running".

@test "qci_claim_driver: a non-root caller without a delegated scope fails closed" {
    local lock="$BATS_TEST_TMPDIR/qci/slug/driver.lock"
    run env -u QCI_DRIVER_CLAIM_TEST_PROC_FALLBACK bash -c '
        source "$1"
        qci_claim_driver "$2"
        echo RAN
    ' _ "$REPO_ROOT/ci/lib/guest/gui-waiters.sh" "$lock"
    [ "$status" -eq 2 ]
    [[ "$output" == *"cannot create a killable cgroup"* ]]
    [[ "$output" != *RAN* ]]
}

# _claim_child_driver <lib> <lock> <ready> <fifo>
# A driver that claims, then leaves a DETACHED child behind (the shape of
# `setsid -f runuser ... app` and of a launcher that daemonizes) and exits.
# The child blocks on <fifo> until the test releases it.
_claim_child_driver() {
    bash -c '
        # shellcheck disable=SC1090
        source "$1"
        qci_claim_driver "$2"
        setsid -f bash -c '"'"': > "$1"; exec 3<>"$2"; read -t 30 -u 3 || true'"'"' _ "$3" "$4" \
            </dev/null >/dev/null 2>&1
    ' _ "$1" "$2" "$3" "$4"
    local i
    for i in $(seq 1 50); do [ -f "$3" ] && return 0; sleep 0.1; done
    echo "child did not become ready" >&2
    return 1
}

@test "qci_claim_driver: an app the driver left running does not keep the claim" {
    local lock="$BATS_TEST_TMPDIR/qci/slug/driver.lock"
    local side="$BATS_TEST_TMPDIR/side"
    local ready="$BATS_TEST_TMPDIR/child-ready"
    local fifo="$BATS_TEST_TMPDIR/child-hold"
    mkfifo "$fifo"
    _claim_child_driver "$REPO_ROOT/ci/lib/guest/gui-waiters.sh" "$lock" "$ready" "$fifo"
    # The driver shell has exited; its child is still alive.
    run timeout 10 bash -c '
        # shellcheck disable=SC1090
        source "$1"
        qci_claim_driver "$2"
        echo RAN > "$3"
    ' _ "$REPO_ROOT/ci/lib/guest/gui-waiters.sh" "$lock" "$side"
    echo go > "$fifo"
    [ "$status" -eq 0 ]
    [ "$(cat "$side")" = RAN ]
}

@test "qci_claim_driver: no child of the driver holds the lock file open" {
    local lock="$BATS_TEST_TMPDIR/qci/slug/driver.lock"
    local out="$BATS_TEST_TMPDIR/child-fds"
    bash -c '
        # shellcheck disable=SC1090
        source "$1"
        qci_claim_driver "$2"
        # A plain child and a $( ) child: list the files each has open.
        ls -l /proc/self/fd/ > "$3.plain" 2>/dev/null
        x=$(ls -l /proc/self/fd/ 2>/dev/null); printf "%s\n" "$x" > "$3.subst"
    ' _ "$REPO_ROOT/ci/lib/guest/gui-waiters.sh" "$lock" "$out"
    [ -s "$out.plain" ]
    [ -s "$out.subst" ]
    # `run` + status, not a bare `! grep`: bats does not fail a test on a
    # negated command that is not the last one.
    run grep -F "$lock" "$out.plain"
    [ "$status" -eq 1 ]
    run grep -F "$lock" "$out.subst"
    [ "$status" -eq 1 ]
}

@test "qci_claim_driver: a live driver keeps the claim while it runs" {
    # The holder is a plain driver that SLEEPS in a child (a waiter's poll):
    # the claim must hold for the shell's whole life, not only until its
    # first command.
    local lock="$BATS_TEST_TMPDIR/qci/slug/driver.lock"
    local side="$BATS_TEST_TMPDIR/second-side"
    local ready="$BATS_TEST_TMPDIR/ready"
    bash -c '
        # shellcheck disable=SC1090
        source "$1"
        qci_claim_driver "$2"
        : > "$3"
        sleep 4
    ' _ "$REPO_ROOT/ci/lib/guest/gui-waiters.sh" "$lock" "$ready" &
    local holder=$! i
    for i in $(seq 1 50); do [ -f "$ready" ] && break; sleep 0.1; done
    [ -f "$ready" ] || { kill "$holder"; wait "$holder" || true; return 1; }
    run timeout 10 bash -c '
        # shellcheck disable=SC1090
        source "$1"
        qci_claim_driver "$2"
        echo RAN > "$3"
    ' _ "$REPO_ROOT/ci/lib/guest/gui-waiters.sh" "$lock" "$side"
    wait "$holder" || true
    [ "$status" -eq 1 ]
    [ "${lines[0]}" = "ERROR: a second guest driver is already running: $lock" ]
    [ ! -e "$side" ]
}

@test "qci_claim_driver: a SIGKILLed driver that is never reaped releases the claim" {
    # A zombie is dead: the old in-shell fd was released at exit, not when the
    # parent reaped it, and a pid-existence watcher (tail --pid) would keep the
    # lock through the zombie.
    local lock="$BATS_TEST_TMPDIR/qci/slug/driver.lock"
    local side="$BATS_TEST_TMPDIR/side"
    local ready="$BATS_TEST_TMPDIR/ready"
    local pidf="$BATS_TEST_TMPDIR/driver.pid"
    local fifo="$BATS_TEST_TMPDIR/parent-hold"
    mkfifo "$fifo"
    # The parent starts the driver, then blocks without ever calling wait.
    bash -c '
        bash -c "source \"\$1\"; qci_claim_driver \"\$2\"; : > \"\$3\"; sleep 30" _ "$1" "$2" "$3" &
        echo $! > "$4"
        exec 3<>"$5"; read -t 30 -u 3 || true
    ' _ "$REPO_ROOT/ci/lib/guest/gui-waiters.sh" "$lock" "$ready" "$pidf" "$fifo" &
    local parent=$! i
    for i in $(seq 1 50); do [ -f "$ready" ] && break; sleep 0.1; done
    [ -f "$ready" ] || { kill "$parent"; return 1; }
    kill -KILL "$(cat "$pidf")"
    sleep 0.2
    # The driver is a zombie now: still in the process table, state Z.
    [[ "$(cat "/proc/$(cat "$pidf")/stat")" == *") Z "* ]]
    run timeout 10 bash -c '
        # shellcheck disable=SC1090
        source "$1"
        qci_claim_driver "$2"
        echo RAN > "$3"
    ' _ "$REPO_ROOT/ci/lib/guest/gui-waiters.sh" "$lock" "$side"
    echo go > "$fifo"
    wait "$parent" || true
    [ "$status" -eq 0 ]
    [ "$(cat "$side")" = RAN ]
}

@test "qci_claim_driver: a subshell's claim ends with the subshell while its parent runs on" {
    # The owner is the process that called the function ($BASHPID). With $$
    # the claim taken in `( ... )` would belong to the still-running parent.
    local lock="$BATS_TEST_TMPDIR/qci/slug/driver.lock"
    local side="$BATS_TEST_TMPDIR/side"
    local ready="$BATS_TEST_TMPDIR/ready"
    local fifo="$BATS_TEST_TMPDIR/hold"
    mkfifo "$fifo"
    bash -c '
        # shellcheck disable=SC1090
        source "$1"
        ( qci_claim_driver "$2" )
        : > "$3"
        exec 3<>"$4"; read -t 30 -u 3 || true
    ' _ "$REPO_ROOT/ci/lib/guest/gui-waiters.sh" "$lock" "$ready" "$fifo" &
    local parent=$! i
    for i in $(seq 1 50); do [ -f "$ready" ] && break; sleep 0.1; done
    [ -f "$ready" ] || { kill "$parent"; return 1; }
    run timeout 10 bash -c '
        # shellcheck disable=SC1090
        source "$1"
        qci_claim_driver "$2"
        echo RAN > "$3"
    ' _ "$REPO_ROOT/ci/lib/guest/gui-waiters.sh" "$lock" "$side"
    echo go > "$fifo"
    wait "$parent" || true
    [ "$status" -eq 0 ]
    [ "$(cat "$side")" = RAN ]
}

@test "qci_claim_driver: the lock holder has none of the driver's stdio open" {
    # Scan every process for an fd on the driver's own output FILE: only the
    # driver may have it. A holder with the caller's stdout/stderr would keep
    # qga's capture pipe open after the driver exits.
    local lock="$BATS_TEST_TMPDIR/qci/slug/driver.lock"
    local out="$BATS_TEST_TMPDIR/driver.out"
    local ready="$BATS_TEST_TMPDIR/ready"
    local fifo="$BATS_TEST_TMPDIR/hold"
    mkfifo "$fifo"
    bash -c '
        # shellcheck disable=SC1090
        source "$1"
        qci_claim_driver "$2"
        echo $BASHPID > "$3"
        exec 3<>"$4"; read -t 30 -u 3 || true
    ' _ "$REPO_ROOT/ci/lib/guest/gui-waiters.sh" "$lock" "$ready" "$fifo" \
        < /dev/null > "$out" 2>&1 &
    local drv=$! i p fd holders=""
    for i in $(seq 1 50); do [ -s "$ready" ] && break; sleep 0.1; done
    [ -s "$ready" ] || { kill "$drv"; return 1; }
    for p in /proc/[0-9]*; do
        [ "${p#/proc/}" = "$(cat "$ready")" ] && continue
        for fd in "$p"/fd/*; do
            [ "$(readlink "$fd" 2>/dev/null)" = "$out" ] && holders="$holders ${p#/proc/}"
        done
    done
    echo go > "$fifo"
    wait "$drv" || true
    echo "holders:$holders"
    [ -z "$holders" ]
}

@test "qci_claim_driver: the claim does not hold the caller's stdout open" {
    # qga guest-exec reports the command finished only when every holder of
    # its output pipe has closed it: a lock holder that outlived the driver
    # while still holding the pipe would hang the driver's own vm-exec.
    local lock="$BATS_TEST_TMPDIR/qci/slug/driver.lock"
    run timeout 10 bash -c '
        bash -c "source \"\$1\"; qci_claim_driver \"\$2\"; echo claimed" _ "$1" "$2" 2>&1 | cat
    ' _ "$REPO_ROOT/ci/lib/guest/gui-waiters.sh" "$lock"
    [ "$status" -eq 0 ]
    [ "$output" = claimed ]
}

@test "qci_claim_driver: a background subshell of a finished driver cannot re-claim by inheritance" {
    # The subshell inherits QCI_DRIVER_CLAIM_* from its parent. Once the
    # parent is gone and the lock is free, its "same path" call must make a
    # real attempt, not return 0 while another driver takes the lock.
    local lock="$BATS_TEST_TMPDIR/qci/slug/driver.lock"
    local ready="$BATS_TEST_TMPDIR/ready"
    local fifo="$BATS_TEST_TMPDIR/hold"
    local out="$BATS_TEST_TMPDIR/sub.out"
    mkfifo "$fifo"
    bash -c '
        # shellcheck disable=SC1090
        source "$1"
        qci_claim_driver "$2"
        (
            exec 3<>"$4"; read -t 30 -u 3 || true
            qci_claim_driver "$2"
            echo SUB-CLAIMED
        ) </dev/null >"$3" 2>&1 &
    ' _ "$REPO_ROOT/ci/lib/guest/gui-waiters.sh" "$lock" "$out" "$fifo"
    # The parent has exited; take the lock as a new driver and hold it.
    bash -c '
        # shellcheck disable=SC1090
        source "$1"
        qci_claim_driver "$2"
        : > "$3"
        sleep 5
    ' _ "$REPO_ROOT/ci/lib/guest/gui-waiters.sh" "$lock" "$ready" &
    local holder=$! i
    for i in $(seq 1 50); do [ -f "$ready" ] && break; sleep 0.1; done
    [ -f "$ready" ] || { kill "$holder"; echo go > "$fifo"; return 1; }
    echo go > "$fifo"
    for i in $(seq 1 60); do grep -q 'ERROR\|SUB-CLAIMED' "$out" 2>/dev/null && break; sleep 0.1; done
    kill "$holder" 2>/dev/null || true
    wait "$holder" 2>/dev/null || true
    cat "$out"
    run grep -q SUB-CLAIMED "$out"
    [ "$status" -eq 1 ]
    grep -q 'ERROR: a second guest driver is already running' "$out"
}

# _claim_kill_one_holder <which: 1|2> [child] — a ticking driver claims;
# SIGKILL one of its two holder processes; a contender then claims. The
# survivor must kill the driver BEFORE the lock can be taken: no tick after
# the contender's CLAIMED line. With `child`, the ticks come from a
# FOREGROUND child the driver is waiting on, which must die with it.
_claim_kill_one_holder() {
    local which=$1 shape=${2:-shell}
    local lock="$BATS_TEST_TMPDIR/qci/slug/driver.lock"
    local log="$BATS_TEST_TMPDIR/ticks"
    local ready="$BATS_TEST_TMPDIR/ready"
    bash -c '
        # shellcheck disable=SC1090
        source "$1"
        qci_claim_driver "$2"
        trap "echo TEARDOWN >> \"$3\"" EXIT
        echo "$BASHPID $QCI_DRIVER_CLAIM_HOLDER" > "$4"
        if [ "$5" = child ]; then
            bash -c "for i in \$(seq 1 100); do echo child-tick >> \"\$1\"; sleep 0.05; done" _ "$3"
        else
            for i in $(seq 1 100); do echo tick >> "$3"; sleep 0.05; done
        fi
    ' _ "$REPO_ROOT/ci/lib/guest/gui-waiters.sh" "$lock" "$log" "$ready" "$shape" &
    local drv=$! i owner hp gp victim
    for i in $(seq 1 50); do [ -s "$ready" ] && break; sleep 0.1; done
    [ -s "$ready" ] || { kill "$drv"; return 1; }
    read -r owner hp gp < "$ready"
    if [ "$which" = 1 ]; then victim=$hp; else victim=$gp; fi
    kill -KILL "$victim"
    run timeout 10 bash -c '
        # shellcheck disable=SC1090
        source "$1"
        qci_claim_driver "$2"
        echo CLAIMED >> "$3"
        sleep 0.5
    ' _ "$REPO_ROOT/ci/lib/guest/gui-waiters.sh" "$lock" "$log"
    local rc=0
    wait "$drv" || rc=$?
    [ "$status" -eq 0 ]
    # The driver was SIGKILLed (137), not allowed to finish its 5s of ticks,
    # and its EXIT teardown never ran.
    [ "$rc" -eq 137 ]
    run grep -c TEARDOWN "$log"
    [ "$output" = 0 ]
    # Nothing the old driver did came after the new claim.
    [ "$(tail -n 1 "$log")" = CLAIMED ]
}

@test "qci_claim_driver: killing the flock-holding parent kills the driver before the lock can be taken" {
    _claim_kill_one_holder 1
}

@test "qci_claim_driver: killing the guard kills the driver before the lock can be taken" {
    _claim_kill_one_holder 2
}

@test "qci_claim_driver: killing a holder also kills the driver's foreground command" {
    _claim_kill_one_holder 2 child
    grep -q child-tick "$BATS_TEST_TMPDIR/ticks"
}

@test "qci_claim_driver: killing the driver directly drains its foreground command before unlock" {
    local lock="$BATS_TEST_TMPDIR/qci/slug/driver.lock"
    local log="$BATS_TEST_TMPDIR/direct-ticks"
    local ready="$BATS_TEST_TMPDIR/direct-ready"
    bash -c '
        # shellcheck disable=SC1090
        source "$1"
        qci_claim_driver "$2"
        echo "$BASHPID" > "$4"
        # The trailing command prevents bash from exec-optimising the child.
        bash -c "for i in \$(seq 1 200); do echo child-\$i >> \"\$1\"; sleep 0.05; done" _ "$3"
        echo DRIVER-AFTER-CHILD >> "$3"
    ' _ "$REPO_ROOT/ci/lib/guest/gui-waiters.sh" "$lock" "$log" "$ready" &
    local drv=$! i rc=0
    for i in $(seq 1 50); do [ -s "$ready" ] && [ -s "$log" ] && break; sleep 0.1; done
    [ -s "$ready" ] && [ -s "$log" ] || { kill "$drv"; return 1; }
    # Allow both guardians to observe and identity-pin the foreground child.
    sleep 0.4
    kill -KILL "$(cat "$ready")"
    run timeout 10 bash -c '
        # shellcheck disable=SC1090
        source "$1"
        qci_claim_driver "$2"
        echo CLAIMED >> "$3"
        sleep 0.5
    ' _ "$REPO_ROOT/ci/lib/guest/gui-waiters.sh" "$lock" "$log"
    wait "$drv" || rc=$?
    [ "$status" -eq 0 ]
    [ "$rc" -eq 137 ]
    [ "$(tail -n 1 "$log")" = CLAIMED ]
    run grep -q DRIVER-AFTER-CHILD "$log"
    [ "$status" -eq 1 ]
}

@test "qci_claim_driver: teardown closes a foreground fork storm before unlock" {
    local lock="$BATS_TEST_TMPDIR/qci/slug/driver.lock"
    local log="$BATS_TEST_TMPDIR/fork-ticks"
    local ready="$BATS_TEST_TMPDIR/fork-ready"
    bash -c '
        # shellcheck disable=SC1090
        source "$1"
        qci_claim_driver "$2"
        echo "$BASHPID $QCI_DRIVER_CLAIM_HOLDER" > "$4"
        # Each short-lived forker leaves a child running after reparenting.
        # The guardian must finish draining before the next claim can act.
        bash -c "for i in \$(seq 1 150); do ( (sleep 0.03; echo fork-\$i >> \"\$1\") & ); sleep 0.01; done; wait" _ "$3"
        echo DRIVER-AFTER-CHILD >> "$3"
    ' _ "$REPO_ROOT/ci/lib/guest/gui-waiters.sh" "$lock" "$log" "$ready" &
    local drv=$! i owner hp gp rc=0
    for i in $(seq 1 50); do [ -s "$ready" ] && [ -s "$log" ] && break; sleep 0.1; done
    [ -s "$ready" ] && [ -s "$log" ] || { kill "$drv"; return 1; }
    read -r owner hp gp < "$ready"
    kill -KILL "$gp"
    run timeout 10 bash -c '
        # shellcheck disable=SC1090
        source "$1"
        qci_claim_driver "$2"
        echo CLAIMED >> "$3"
        sleep 0.5
    ' _ "$REPO_ROOT/ci/lib/guest/gui-waiters.sh" "$lock" "$log"
    wait "$drv" || rc=$?
    [ "$status" -eq 0 ]
    [ "$rc" -eq 137 ]
    [ "$(tail -n 1 "$log")" = CLAIMED ]
}

@test "qci_claim_driver: a driver whose holders were both killed stops at its next bg_start, without teardown" {
    # Killing both holder processes at once is the one way past the guards;
    # the driver still refuses to start more work.
    local lock="$BATS_TEST_TMPDIR/qci/slug/driver.lock"
    local out="$BATS_TEST_TMPDIR/drv.out"
    local ready="$BATS_TEST_TMPDIR/ready"
    local fifo="$BATS_TEST_TMPDIR/hold"
    mkfifo "$fifo"
    bash -c '
        # shellcheck disable=SC1090
        source "$1"
        QCI_BG_DIR=$5
        qci_claim_driver "$2"
        trap "echo TEARDOWN" EXIT
        echo "$QCI_DRIVER_CLAIM_HOLDER" > "$3"
        exec 3<>"$4"; read -t 30 -u 3 || true
        bg_start j1 - "echo job-ran"
        echo AFTER-BG
    ' _ "$REPO_ROOT/ci/lib/guest/gui-waiters.sh" "$lock" "$ready" "$fifo" "$BATS_TEST_TMPDIR" \
        >"$out" 2>&1 &
    local drv=$! i
    for i in $(seq 1 50); do [ -s "$ready" ] && break; sleep 0.1; done
    [ -s "$ready" ] || { kill "$drv"; return 1; }
    # shellcheck disable=SC2046
    kill -KILL $(cat "$ready")
    sleep 0.3
    echo go > "$fifo"
    local rc=0
    wait "$drv" || rc=$?
    cat "$out"
    [ "$rc" -eq 1 ]
    grep -q 'the claim on .* was lost' "$out"
    run grep -q 'AFTER-BG\|TEARDOWN' "$out"
    [ "$status" -eq 1 ]
    [ ! -e "$BATS_TEST_TMPDIR/j1.log" ]
}

@test "bg_start under a claim: the job is on record before its command runs" {
    local lock="$BATS_TEST_TMPDIR/qci/slug/driver.lock"
    run timeout 20 bash -c '
        # shellcheck disable=SC1090
        source "$1"
        QCI_BG_DIR=$3
        qci_claim_driver "$2"
        bg_start rec - "grep -q \" rec\$\" $2.jobs && echo ON-RECORD"
        bg_wait rec 10 1 >/dev/null
        bg_log rec
    ' _ "$REPO_ROOT/ci/lib/guest/gui-waiters.sh" "$lock" "$BATS_TEST_TMPDIR"
    [ "$status" -eq 0 ]
    [ "$output" = ON-RECORD ]
}

@test "bg_start under a claim: a job that cannot be recorded never runs, and the driver stops without teardown" {
    local lock="$BATS_TEST_TMPDIR/qci/slug/driver.lock"
    run timeout 20 bash -c '
        # shellcheck disable=SC1090
        source "$1"
        QCI_BG_DIR=$3
        qci_claim_driver "$2"
        trap "echo TEARDOWN" EXIT
        chmod 0400 "$2.jobs"
        bg_start rec - "echo RAN > $3/side"
        echo AFTER-BG
    ' _ "$REPO_ROOT/ci/lib/guest/gui-waiters.sh" "$lock" "$BATS_TEST_TMPDIR"
    [ "$status" -eq 1 ]
    [[ "$output" == *"cannot record job rec"* ]]
    [[ "$output" != *AFTER-BG* ]]
    [[ "$output" != *TEARDOWN* ]]
    sleep 0.5
    [ ! -e "$BATS_TEST_TMPDIR/side" ]
}

@test "bg_start without a claim is unchanged: no gate, no record" {
    bg_setup
    bg_start plain - 'echo hi'
    QCI_AWAIT_QUIET=1 bg_wait plain 10 1
    [ "$(bg_log plain)" = hi ]
    [ "$(bg_rc plain)" = 0 ]
}

@test "qci_claim_driver: a bg_start job keeps the claim after its driver exits, and is named" {
    local lock="$BATS_TEST_TMPDIR/qci/slug/driver.lock"
    local side="$BATS_TEST_TMPDIR/side"
    local fifo="$BATS_TEST_TMPDIR/job-hold"
    mkfifo "$fifo"
    bash -c '
        # shellcheck disable=SC1090
        source "$1"
        QCI_BG_DIR=$3
        qci_claim_driver "$2"
        bg_start job-a - "exec 3<>$4; read -t 30 -u 3 || true"
    ' _ "$REPO_ROOT/ci/lib/guest/gui-waiters.sh" "$lock" "$BATS_TEST_TMPDIR" "$fifo"
    # The driver has exited; its bg_start job is still running.
    run timeout 10 bash -c '
        # shellcheck disable=SC1090
        source "$1"
        qci_claim_driver "$2"
        echo RAN > "$3"
    ' _ "$REPO_ROOT/ci/lib/guest/gui-waiters.sh" "$lock" "$side"
    echo go > "$fifo"
    [ "$status" -eq 1 ]
    [ "${lines[0]}" = "ERROR: a second guest driver is already running: $lock" ]
    [[ "$output" == *"held for: bg_start job job-a, pid "* ]]
    [ ! -e "$side" ]
    # Once the job has finished, a new driver claims.
    local i
    for i in $(seq 1 50); do [ -f "$BATS_TEST_TMPDIR/job-a.rc" ] && break; sleep 0.1; done
    run timeout 10 bash -c '
        # shellcheck disable=SC1090
        source "$1"
        qci_claim_driver "$2"
        echo RAN > "$3"
    ' _ "$REPO_ROOT/ci/lib/guest/gui-waiters.sh" "$lock" "$side"
    [ "$status" -eq 0 ]
    [ "$(cat "$side")" = RAN ]
}

@test "qci_claim_driver: an app a bg_start launcher daemonized does not keep the claim" {
    local lock="$BATS_TEST_TMPDIR/qci/slug/driver.lock"
    local side="$BATS_TEST_TMPDIR/side"
    local ready="$BATS_TEST_TMPDIR/app-ready"
    local fifo="$BATS_TEST_TMPDIR/app-hold"
    mkfifo "$fifo"
    # The shape of `bg_start admin admin qdistro-start-admin-app`: the job is
    # the launcher, which returns after it detached the app.
    bash -c '
        # shellcheck disable=SC1090
        source "$1"
        QCI_BG_DIR=$3
        qci_claim_driver "$2"
        bg_start launch - "setsid -f bash -c '"'"': > $4; exec 3<>$5; read -t 30 -u 3 || true'"'"' </dev/null >/dev/null 2>&1"
        bg_wait launch 10 1 >/dev/null
    ' _ "$REPO_ROOT/ci/lib/guest/gui-waiters.sh" "$lock" "$BATS_TEST_TMPDIR" "$ready" "$fifo"
    local i
    for i in $(seq 1 50); do [ -f "$ready" ] && break; sleep 0.1; done
    [ -f "$ready" ]
    run timeout 10 bash -c '
        # shellcheck disable=SC1090
        source "$1"
        qci_claim_driver "$2"
        echo RAN > "$3"
    ' _ "$REPO_ROOT/ci/lib/guest/gui-waiters.sh" "$lock" "$side"
    echo go > "$fifo"
    [ "$status" -eq 0 ]
    [ "$(cat "$side")" = RAN ]
}

@test "qci_claim_driver: refuses a lock path that is a symlink, and does not follow it" {
    local d="$BATS_TEST_TMPDIR/qci/slug" victim="$BATS_TEST_TMPDIR/victim"
    mkdir -p "$d"
    printf 'keep\n' > "$victim"
    ln -s "$victim" "$d/driver.lock"
    run bash -c '
        # shellcheck disable=SC1090
        source "$1"
        qci_claim_driver "$2"
        echo RAN
    ' _ "$REPO_ROOT/ci/lib/guest/gui-waiters.sh" "$d/driver.lock"
    [ "$status" -eq 2 ]
    [[ "$output" == *"unsafe lock path"* ]]
    [ "$(cat "$victim")" = keep ]
    [ -L "$d/driver.lock" ]
}

@test "qci_claim_driver: refuses a lock in a directory anyone can swap it in" {
    local d="$BATS_TEST_TMPDIR/qci/open"
    mkdir -p "$d"
    chmod 0777 "$d"
    run bash -c '
        # shellcheck disable=SC1090
        source "$1"
        qci_claim_driver "$2"
        echo RAN
    ' _ "$REPO_ROOT/ci/lib/guest/gui-waiters.sh" "$d/driver.lock"
    [ "$status" -eq 2 ]
    [[ "$output" == *"unsafe lock path"* ]]
    # Group-writable is no better: any group member can swap the lock.
    chmod 0770 "$d"
    run bash -c '
        # shellcheck disable=SC1090
        source "$1"
        qci_claim_driver "$2"
        echo RAN
    ' _ "$REPO_ROOT/ci/lib/guest/gui-waiters.sh" "$d/driver.lock"
    [ "$status" -eq 2 ]
    [[ "$output" == *"unsafe lock path"* ]]
    # The same directory with the sticky bit (the /tmp/qci/<slug> shape) is fine.
    chmod 1777 "$d"
    run bash -c '
        # shellcheck disable=SC1090
        source "$1"
        qci_claim_driver "$2"
        echo RAN
    ' _ "$REPO_ROOT/ci/lib/guest/gui-waiters.sh" "$d/driver.lock"
    [ "$status" -eq 0 ]
    [ "$output" = RAN ]
}

# --- qci_host_step ------------------------------------------------------------

@test "qci_host_step: a killed driver's step is withdrawn, and a retry never shows it" {
    local lock="$BATS_TEST_TMPDIR/qci/slug/driver.lock" d="$BATS_TEST_TMPDIR/qci/slug"
    local ready="$BATS_TEST_TMPDIR/ready"
    bash -c '
        # shellcheck disable=SC1090
        source "$1"
        qci_claim_driver "$2"
        echo $BASHPID > "$3"
        qci_host_step s1 30
    ' _ "$REPO_ROOT/ci/lib/guest/gui-waiters.sh" "$lock" "$ready" >/dev/null 2>&1 &
    local drv=$! i
    for i in $(seq 1 50); do [ -s "$d/waiting" ] && break; sleep 0.1; done
    [ -s "$d/waiting" ] || { kill "$drv"; return 1; }
    [[ "$(cat "$d/waiting")" == s1."$(cat "$ready")".* ]]
    kill -KILL "$(cat "$ready")"
    wait "$drv" || true
    # The holder withdraws it once the driver is dead.
    for i in $(seq 1 30); do [ -e "$d/waiting" ] || break; sleep 0.1; done
    [ ! -e "$d/waiting" ]
    # Even a stale file that survived (planted here) is gone once a retry has
    # claimed, before its Setup runs.
    printf 's1.1.2\n' > "$d/waiting"
    run timeout 10 bash -c '
        # shellcheck disable=SC1090
        source "$1"
        qci_claim_driver "$2"
        [ -e "${2%/*}/waiting" ] && echo STALE-VISIBLE
        echo CLAIMED
    ' _ "$REPO_ROOT/ci/lib/guest/gui-waiters.sh" "$lock"
    [ "$status" -eq 0 ]
    [ "$output" = CLAIMED ]
}

# _host_go <dir> <step-name> — act as the host: wait until `waiting` names
# <step-name>, then mkdir that token's go. Prints the token.
_host_go() {
    local d=$1 name=$2 tok="" i
    for i in $(seq 1 100); do
        tok=$(cat "$d/waiting" 2>/dev/null) || tok=""
        [ "${tok%%.*}" = "$name" ] && break
        sleep 0.1
    done
    [ "${tok%%.*}" = "$name" ] || return 1
    mkdir "$d/$tok.go"
    printf '%s\n' "$tok"
}

@test "qci_host_step: names the step it waits for and returns once the host says go" {
    local d="$BATS_TEST_TMPDIR/qci/slug"
    mkdir -p "$d"
    _host_go "$d" s1 > "$BATS_TEST_TMPDIR/tok" &
    QCI_HOST_STEP_DIR=$d run qci_host_step s1 10
    wait
    [ "$status" -eq 0 ]
    [[ "$(cat "$BATS_TEST_TMPDIR/tok")" == s1.* ]]
    [[ "$output" == *"s1: go"* ]]
    [ ! -e "$d/waiting" ]
}

@test "qci_host_step: defaults to the claim's directory and a long deadline" {
    local lock="$BATS_TEST_TMPDIR/qci/slug/driver.lock"
    export -f _host_go
    run timeout 10 bash -c '
        # shellcheck disable=SC1090
        source "$1"
        qci_claim_driver "$2"
        _host_go "${2%/*}" s1 >/dev/null &
        qci_host_step s1
        wait
    ' _ "$REPO_ROOT/ci/lib/guest/gui-waiters.sh" "$lock"
    [ "$status" -eq 0 ] || { echo "$output"; false; }
    [[ "$output" == *"s1: waiting up to 900s"* ]]
}

@test "qci_host_step: a late go meant for an earlier attempt does not release this one" {
    local d="$BATS_TEST_TMPDIR/qci/slug"
    mkdir -p "$d"
    # Attempt A published a token; the host read it and acts late.
    (
        tok=""
        for i in $(seq 1 50); do tok=$(cat "$d/waiting" 2>/dev/null) && [ -n "$tok" ] && break; sleep 0.1; done
        printf '%s\n' "$tok" > "$d/../tok-a"
    ) &
    QCI_HOST_STEP_DIR=$d run qci_host_step s1 1
    wait
    [ "$status" -eq 1 ]
    local tok_a; tok_a=$(cat "$d/../tok-a")
    [ -f "$d/$tok_a.timeout" ]
    # Attempt B waits for the same step; A's go arrives now.
    (
        for i in $(seq 1 50); do
            t=$(cat "$d/waiting" 2>/dev/null) && [ -n "$t" ] && break; sleep 0.1
        done
        mkdir "$d/$tok_a.go"
    ) &
    QCI_HOST_STEP_DIR=$d run qci_host_step s1 2
    wait
    [ "$status" -eq 1 ]
    [[ "$output" == *"never created"* ]]
}

@test "qci_host_step: only a real directory owned by the driver's uid is a go" {
    local d="$BATS_TEST_TMPDIR/qci/slug" target="$BATS_TEST_TMPDIR/elsewhere"
    mkdir -p "$d" "$target"
    (
        for i in $(seq 1 50); do
            t=$(cat "$d/waiting" 2>/dev/null) && [ -n "$t" ] && break; sleep 0.1
        done
        # A file and a symlink to a directory: neither is the host's mkdir.
        : > "$d/$t.go"
    ) &
    QCI_HOST_STEP_DIR=$d run qci_host_step s1 2
    wait
    [ "$status" -eq 1 ]
    (
        for i in $(seq 1 50); do
            t=$(cat "$d/waiting" 2>/dev/null) && [ -n "$t" ] && break; sleep 0.1
        done
        ln -s "$target" "$d/$t.go"
    ) &
    QCI_HOST_STEP_DIR=$d run qci_host_step s1 2
    wait
    [ "$status" -eq 1 ]
}

@test "qci_host_step: a timeout stops the driver without running its EXIT teardown" {
    local d="$BATS_TEST_TMPDIR/qci/slug"
    mkdir -p "$d"
    run bash -c '
        set -euo pipefail
        # shellcheck disable=SC1090
        source "$1"
        trap "echo TEARDOWN" EXIT
        QCI_HOST_STEP_DIR=$2 qci_host_step s1 1
        echo AFTER
    ' _ "$REPO_ROOT/ci/lib/guest/gui-waiters.sh" "$d"
    [ "$status" -eq 1 ]
    [[ "$output" == *"never created"* ]]
    [[ "$output" != *TEARDOWN* ]]
    [[ "$output" != *AFTER* ]]
    ls "$d"/s1.*.timeout
    [ ! -e "$d/waiting" ]
}

@test "qci_host_step: does not write through a symlink planted in the step directory" {
    local d="$BATS_TEST_TMPDIR/qci/slug" victim="$BATS_TEST_TMPDIR/victim"
    mkdir -p "$d"
    printf 'keep\n' > "$victim"
    ln -s "$victim" "$d/waiting"
    QCI_HOST_STEP_DIR=$d run qci_host_step s1 1
    [ "$status" -eq 1 ]
    [ "$(cat "$victim")" = keep ]
}

@test "qci_host_step: refuses a bad name, a bad timeout, and a missing directory" {
    local d="$BATS_TEST_TMPDIR/qci/slug"
    mkdir -p "$d"
    QCI_HOST_STEP_DIR=$d run qci_host_step ../x 1
    [ "$status" -eq 2 ]
    QCI_HOST_STEP_DIR=$d run qci_host_step s1 1.5
    [ "$status" -eq 2 ]
    QCI_HOST_STEP_DIR='' QCI_DRIVER_CLAIM_PATH='' run qci_host_step s1 1
    [ "$status" -eq 2 ]
}

# ---- admin TUI in foot: install check, process identity, bus evidence ------
# The TUI scenarios start the INSTALLED TUI and grade the broker's view of it;
# these pin the helpers' verdicts against fake installs, a fake /proc and a
# synthetic dbus-monitor trace. The identity check reads its allowlist from
# the REAL broker module, so a drift between the two shows up here.

_tui_fake_install() {
    T="$BATS_TEST_TMPDIR/tui"
    rm -rf "$T"
    mkdir -p "$T/lib" "$T/bin" "$T/src" "$T/path"
    chmod 0755 "$T/lib"
    printf '#!/usr/bin/python3 -I\n"""tui"""\ndef _reexec_for_authority():\n    pass\n' > "$T/lib/qdistro_admin_tui.py"
    chmod 0755 "$T/lib/qdistro_admin_tui.py"
    cp "$T/lib/qdistro_admin_tui.py" "$T/src/qdistro_admin_tui.py"
    ln -s "$T/lib/qdistro_admin_tui.py" "$T/bin/qdistro-admin-tui"
    printf '#!/bin/sh\nexit 0\n' > "$T/py-ok"; chmod +x "$T/py-ok"
    printf '#!/bin/sh\nexit 1\n' > "$T/py-no-textual"; chmod +x "$T/py-no-textual"
    printf '#!/bin/sh\n' > "$T/path/foot"; chmod +x "$T/path/foot"
    export QCI_ADMIN_TUI_ENTRY="$T/bin/qdistro-admin-tui" \
        QCI_ADMIN_TUI_LIB="$T/lib/qdistro_admin_tui.py" \
        QCI_ADMIN_TUI_SRC="$T/src/qdistro_admin_tui.py" \
        QCI_ADMIN_TUI_PYTHON="$T/py-ok" \
        QCI_ADMIN_TUI_OWNER_UID="$(id -u)"
    PATH="$T/path:$PATH"
}

@test "admin TUI install check: the installer's symlink to an intact module passes" {
    _tui_fake_install
    run qdwin_admin_tui_check_install
    [ "$status" -eq 0 ]
    [[ "$output" == *"admin TUI install OK"* ]]
}

@test "admin TUI install check: a wrapper written THROUGH the symlink is refused" {
    _tui_fake_install
    # What the retired golden step 4 did: `cat > /usr/local/bin/qdistro-admin-tui`
    # follows the symlink and replaces the installed module with a dev wrapper.
    printf '#!/bin/bash\nexec python3 /home/admin/qdistro/tui/qdistro_admin_tui.py "$@"\n' \
        > "$QCI_ADMIN_TUI_ENTRY"
    [ -L "$QCI_ADMIN_TUI_ENTRY" ]
    run qdwin_admin_tui_check_install
    [ "$status" -ne 0 ]
    [[ "$output" == *"a wrapper replaced it"* ]]
}

@test "admin TUI install check: no symlink, writable module, source drift are refused" {
    _tui_fake_install
    rm "$QCI_ADMIN_TUI_ENTRY"
    cp "$QCI_ADMIN_TUI_LIB" "$QCI_ADMIN_TUI_ENTRY"
    run qdwin_admin_tui_check_install
    [ "$status" -ne 0 ]
    [[ "$output" == *"is not the installer's symlink"* ]]

    _tui_fake_install
    chmod 0775 "$QCI_ADMIN_TUI_LIB"
    run qdwin_admin_tui_check_install
    [ "$status" -ne 0 ]
    [[ "$output" == *"group/other writable"* ]]

    chmod 0755 "$QCI_ADMIN_TUI_LIB"
    printf '# drift\n' >> "$QCI_ADMIN_TUI_SRC"
    run qdwin_admin_tui_check_install
    [ "$status" -ne 0 ]
    [[ "$output" == *"differs from the source tree"* ]]
}

@test "admin TUI install check: missing Textual/Rich or foot is an ERROR, never a skip" {
    _tui_fake_install
    QCI_ADMIN_TUI_PYTHON="$T/py-no-textual"
    run qdwin_admin_tui_check_install
    [ "$status" -ne 0 ]
    [[ "$output" == *"Textual/Rich do not import"*"a bake regression, not a skip"* ]]

    _tui_fake_install
    QCI_ADMIN_TUI_FOOT=qci-no-such-foot
    run qdwin_admin_tui_check_install
    [ "$status" -ne 0 ]
    [[ "$output" == *"foot is missing"* ]]
}

# A fake /proc entry: pid 4321, child of foot <ppid>.
# Args: [exe] [argv words] [env words] [uid] [ppid]
_tui_fake_proc() {
    local exe=${1:-/usr/bin/python3.14} argv=${2:-/usr/bin/python3 -I /usr/local/bin/qdistro-admin-tui}
    local env=${3:-PATH=/usr/bin:/bin HOME=/home/admin TERM=foot COLORTERM=truecolor LANG=C.UTF-8 LC_ALL=C.UTF-8 XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1}
    local uid=${4:-1000} ppid=${5:-77}
    P="$BATS_TEST_TMPDIR/proc"
    rm -rf "$P"
    mkdir -p "$P/4321"
    ln -s "$exe" "$P/4321/exe"
    # shellcheck disable=SC2086 # word lists on purpose
    printf '%s\0' $argv > "$P/4321/cmdline"
    # shellcheck disable=SC2086
    printf '%s\0' $env > "$P/4321/environ"
    printf 'Name:\tpython3\nUid:\t%s\t%s\t%s\t%s\n' "$uid" "$uid" "$uid" "$uid" > "$P/4321/status"
    printf '4321 (python3 (x)) S %s 4321 4321 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 99887766 0 0\n' "$ppid" > "$P/4321/stat"
    export QCI_PROC_ROOT="$P" QCI_ADMIN_TUI_ENTRY=/usr/local/bin/qdistro-admin-tui \
        QCI_ADMIN_TUI_BROKER_MODULE="$REPO_ROOT/broker/qdistro_admin_broker.py"
}

@test "admin TUI identity: the re-exec'd installed TUI under its foot passes" {
    _tui_fake_proc
    run qdwin_admin_tui_identity 4321 77
    [ "$status" -eq 0 ]
    [[ "$output" == *"identity: OK pid=4321"*"TERM=foot"* ]]
    run _qci_proc_starttime 4321
    [ "$output" = 99887766 ]
}

@test "admin TUI identity: each shape the broker refuses for authority is refused" {
    _tui_fake_proc /usr/bin/python3.14 "/usr/bin/python3 -I /usr/local/bin/qdistro-admin-tui --dev"
    run qdwin_admin_tui_identity 4321 77
    [ "$status" -ne 0 ]; [[ "$output" == *"no TUI arguments"* ]]

    _tui_fake_proc /usr/bin/python3.14 "/usr/bin/python3 /usr/local/bin/qdistro-admin-tui"
    run qdwin_admin_tui_identity 4321 77
    [ "$status" -ne 0 ]; [[ "$output" == *"isolated"* ]]

    _tui_fake_proc /usr/bin/python3.14 "/usr/bin/python3 -I /home/admin/qdistro/tui/qdistro_admin_tui.py"
    run qdwin_admin_tui_identity 4321 77
    [ "$status" -ne 0 ]

    _tui_fake_proc /home/admin/bin/python3
    run qdwin_admin_tui_identity 4321 77
    [ "$status" -ne 0 ]; [[ "$output" == *"not the root-owned system python3"* ]]

    # The pre-re-exec process: the same argv the shebang gives, but the
    # launcher's environment -- the broker would refuse it.
    _tui_fake_proc "" "" "PATH=/usr/bin TERM=foot PYTHONPATH=/home/admin/x"
    run qdwin_admin_tui_identity 4321 77
    [ "$status" -ne 0 ]; [[ "$output" == *"outside the broker allowlist"*"PYTHONPATH"* ]]

    _tui_fake_proc "" "" "PATH=/usr/bin QT_QPA_PLATFORM=xcb TERM=foot"
    run qdwin_admin_tui_identity 4321 77
    [ "$status" -ne 0 ]; [[ "$output" == *"QT_QPA_PLATFORM"* ]]

    _tui_fake_proc "" "" "PATH=/usr/bin HOME=/home/admin"
    run qdwin_admin_tui_identity 4321 77
    [ "$status" -ne 0 ]; [[ "$output" == *"TERM=''"* ]]

    _tui_fake_proc "" "" "" 0
    run qdwin_admin_tui_identity 4321 77
    [ "$status" -ne 0 ]; [[ "$output" == *"uid"* ]]

    _tui_fake_proc
    run qdwin_admin_tui_identity 4321 78
    [ "$status" -ne 0 ]; [[ "$output" == *"is not the foot terminal 78"* ]]

    QCI_ADMIN_TUI_BROKER_MODULE="$BATS_TEST_TMPDIR/none.py"
    run qdwin_admin_tui_identity 4321 77
    [ "$status" -ne 0 ]; [[ "$output" == *"cannot read the broker's allowlist"* ]]
}

# A synthetic dbus-monitor log (body on stdin): the TUI (:1.40) mounts at
# t=1000 (its first GetPending); the broker is :1.9, the admin app :1.50.
_bus_fake_trace() {
    export QCI_BUS_TRACE_DIR="$BATS_TEST_TMPDIR/trace" QCI_ADMIN_TUI_STATE="$BATS_TEST_TMPDIR/tuistate"
    mkdir -p "$QCI_BUS_TRACE_DIR" "$QCI_ADMIN_TUI_STATE"
    printf ':1.40\n' > "$QCI_ADMIN_TUI_STATE/tui.bus"
    {
        printf 'signal time=999.000000 sender=:1.3 -> destination=(null destination) serial=2 path=/org/qdistro/QciProbe; interface=org.qdistro.QciProbe; member=Ready\n   string "qci-trace"\n'
        printf 'method call time=1000.000000 sender=:1.40 -> destination=:1.9 serial=5 path=/org/qdistro/AdminBroker1; interface=org.qdistro.AdminBroker1; member=GetPending\n'
        cat
    } > "$QCI_BUS_TRACE_DIR/admin-tui.log"
}

_sig() {  # _sig <time> <member> <args...>
    local t=$1 m=$2 a
    shift 2
    printf 'signal time=%s sender=:1.9 -> destination=(null destination) serial=9 path=/org/qdistro/AdminBroker1; interface=org.qdistro.AdminBroker1; member=%s\n' "$t" "$m"
    for a in "$@"; do printf '   %s\n' "$a"; done
}
_call() {  # _call <time> <sender> <member> <args...>
    local t=$1 s=$2 m=$3 a
    shift 3
    printf 'method call time=%s sender=%s -> destination=:1.9 serial=6 path=/org/qdistro/AdminBroker1; interface=org.qdistro.AdminBroker1; member=%s\n' "$t" "$s" "$m"
    for a in "$@"; do printf '   %s\n' "$a"; done
}

@test "bus trace: events parse calls, signals and their arguments" {
    _bus_fake_trace < <(_sig 1005.250000 RequestDecided 'int32 3' 'string "deny"')
    run qci_bus_trace_events admin-tui
    [ "$status" -eq 0 ]
    [ "${lines[1]}" = "$(printf '1000.000000\tcall\t:1.40\tGetPending\t')" ]
    [ "${lines[2]}" = "$(printf '1005.250000\tsignal\t:1.9\tRequestDecided\t3,deny')" ]
    qci_bus_names_of_pid() { echo ":1.40"; }
    run qci_bus_trace_count admin-tui call GetPending 4321
    [ "$output" = 1 ]
    run qci_bus_trace_count admin-tui signal RequestDecided - 1006
    [ "$output" = 0 ]
    run qci_bus_trace_count admin-tui signal RequestDecided - 1005
    [ "$output" = 1 ]
}

@test "TUI signal refresh: a GetPending right after RequestPending, off the poll grid, counts" {
    _bus_fake_trace < <(_sig 1012.000000 RequestPending 'int32 1'; _call 1012.300000 :1.40 GetPending)
    run qdwin_admin_tui_await_signal_refresh 1010 2
    [ "$status" -eq 0 ]
    [[ "$output" == *"SIGNAL-DRIVEN: RequestPending(1)"* ]]
}

@test "TUI signal refresh: a refresh on a safety-poll tick is INCONCLUSIVE, not proof" {
    _bus_fake_trace < <(_sig 1029.500000 RequestPending 'int32 1'; _call 1030.010000 :1.40 GetPending)
    run qdwin_admin_tui_await_signal_refresh 1010 1
    [ "$status" -ne 0 ]
    [[ "$output" == *"INCONCLUSIVE"* ]]
}

@test "TUI signal refresh: no refresh after the signal times out naming it" {
    _bus_fake_trace < <(_sig 1012.000000 RequestPending 'int32 1')
    run qdwin_admin_tui_await_signal_refresh 1010 1
    [ "$status" -ne 0 ]
    [[ "$output" == *"but no TUI GetPending within"* ]]
    # Another client's refresh is not the TUI's.
    _bus_fake_trace < <(_sig 1012.000000 RequestPending 'int32 1'; _call 1012.300000 :1.50 GetPending)
    run qdwin_admin_tui_await_signal_refresh 1010 1
    [ "$status" -ne 0 ]
}

@test "TUI removed by signal: the cheap path (no GetPending) after RequestDecided passes" {
    _bus_fake_trace < <(_call 1014.000000 :1.50 DecideRequest 'int32 1' 'string "allow"'
      _sig 1014.100000 RequestDecided 'int32 1' 'string "allow"')
    run qdwin_admin_tui_removed_by_signal 1013
    [ "$status" -eq 0 ]
    [[ "$output" == *"OK: RequestDecided(1,allow)"*"TUI made no DecideRequest"* ]]
}

@test "TUI removed by signal: a poll is INCONCLUSIVE; a stray refresh, a TUI decision, or no signal FAIL" {
    _bus_fake_trace < <(_sig 1014.100000 RequestDecided 'int32 1' 'string "allow"'
      _call 1030.000000 :1.40 GetPending)
    run qdwin_admin_tui_removed_by_signal 1013
    [ "$status" -eq 3 ]; [[ "$output" == *"INCONCLUSIVE: the TUI safety poll ran"* ]]

    _bus_fake_trace < <(_sig 1014.100000 RequestDecided 'int32 1' 'string "allow"'
      _call 1020.000000 :1.40 GetPending)
    run qdwin_admin_tui_removed_by_signal 1013
    [ "$status" -eq 1 ]; [[ "$output" == *"explained by neither a broker signal nor the safety poll"* ]]

    _bus_fake_trace < <(_sig 1014.100000 RequestDecided 'int32 1' 'string "allow"'
      _call 1014.500000 :1.40 DecideRequest 'int32 1' 'string "deny"')
    run qdwin_admin_tui_removed_by_signal 1013
    [ "$status" -ne 0 ]; [[ "$output" == *"the TUI itself called DecideRequest"* ]]

    _bus_fake_trace </dev/null
    run qdwin_admin_tui_removed_by_signal 1013
    [ "$status" -ne 0 ]; [[ "$output" == *"no RequestDecided signal"* ]]
}

@test "admin TUI state: helpers fail loudly when no TUI was started" {
    export QCI_ADMIN_TUI_STATE="$BATS_TEST_TMPDIR/none"
    run qdwin_admin_tui_alive
    [ "$status" -ne 0 ]
    [[ "$output" == *"no admin TUI recorded"* ]]
    run qdwin_focus_admin_tui 1
    [ "$status" -ne 0 ]
    run qdwin_start_admin_tui 'bad tag'
    [ "$status" -eq 2 ]
}

@test "TUI decision evidence: the TUI's own DecideRequest, the broker signal and the audit row agree" {
    _bus_fake_trace < <(_sig 1012.000000 RequestPending 'int32 7'
        _call 1020.000000 :1.40 DecideRequest 'int32 7' 'string "deny"' 'string "once"'
        _sig 1020.100000 RequestDecided 'int32 7' 'string "deny"')
    printf '900\n' > "$QCI_BUS_TRACE_DIR/admin-tui.t0"
    qci_sqlite() { [[ "$2" == *"request_id=7 AND action='test.action' AND ts >= 900"* ]] && echo '0|once|prompt|1000|2000'; }
    run qdwin_admin_tui_decision_evidence test.action deny once
    [ "$status" -eq 0 ]
    [[ "$output" == *"TUI DecideRequest(7,deny,once)"*"OK: the TUI decided request 7"* ]]
    # The same DENIED from a timeout has no TUI call behind it.
    _bus_fake_trace < <(_sig 1012.000000 RequestPending 'int32 7')
    run qdwin_admin_tui_decision_evidence test.action deny once
    [ "$status" -ne 0 ]
    [[ "$output" == *"want exactly one request id"* ]]
    # Another client's decision is not the TUI's.
    _bus_fake_trace < <(_call 1020.000000 :1.50 DecideRequest 'int32 7' 'string "deny"' 'string "once"'
        _sig 1020.100000 RequestDecided 'int32 7' 'string "deny"')
    run qdwin_admin_tui_decision_evidence test.action deny once
    [ "$status" -ne 0 ]
    # An audit row that is not the admin's prompt decision fails.
    _bus_fake_trace < <(_call 1020.000000 :1.40 DecideRequest 'int32 7' 'string "deny"' 'string "once"'
        _sig 1020.100000 RequestDecided 'int32 7' 'string "deny"')
    qci_sqlite() { echo '0||timeout||2000'; }
    run qdwin_admin_tui_decision_evidence test.action deny once
    [ "$status" -ne 0 ]
    [[ "$output" == *"expected audit row 0|once|prompt|1000|2000"* ]]
}

@test "TUI refresh readiness: any GetPending after the RequestPending counts, a poll too" {
    _bus_fake_trace < <(_sig 1029.500000 RequestPending 'int32 1'; _call 1030.010000 :1.40 GetPending)
    run qdwin_admin_tui_await_refresh 1010 2
    [ "$status" -eq 0 ]
    _bus_fake_trace < <(_call 1011.000000 :1.40 GetPending; _sig 1012.000000 RequestPending 'int32 1')
    run qdwin_admin_tui_await_refresh 1010 1
    [ "$status" -ne 0 ]
    [[ "$output" == *"no TUI GetPending after it yet"* ]]
}

@test "TUI clear of poll: waits out the edges of the 30 s safety-poll grid" {
    _bus_fake_trace < /dev/null
    date() { [ "$1" = +%s.%N ] && echo 1031.000000; }   # phase 1.0 s
    run qdwin_admin_tui_clear_of_poll
    [ "$status" -eq 0 ]
    [[ "$output" == *"poll phase 2.0s after a tick, 28.0s before the next (waited 1.0s)"* ]]
    date() { [ "$1" = +%s.%N ] && echo 1010.000000; }   # phase 10 s
    run qdwin_admin_tui_clear_of_poll
    [[ "$output" == *"waited 0.0s"* ]]
    # A 25 s budget only fits at phases 2-5 s; at 29.5 s wait for tick + 2 s.
    date() { [ "$1" = +%s.%N ] && echo 1029.500000; }
    run qdwin_admin_tui_clear_of_poll 25
    [[ "$output" == *"poll phase 2.0s after a tick, 28.0s before the next (waited 2.5s)"* ]]
    unset -f date
}
