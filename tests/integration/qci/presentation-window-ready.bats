#!/usr/bin/env bats
# qci:host-only
# A detached GUI process is not capture-ready until qdwin maps and focuses it.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    export QDISTRO_REPO="$REPO_ROOT"
    source "$REPO_ROOT/tests/integration/presentation-gui/presentation-helpers.sh"
    echo 0 >"$BATS_TEST_TMPDIR/journal-calls"
    pres_window_handle() { printf '4\n'; }
    pres_admin() {
        local journal_calls
        journal_calls=$(cat "$BATS_TEST_TMPDIR/journal-calls")
        journal_calls=$((journal_calls + 1))
        echo "$journal_calls" >"$BATS_TEST_TMPDIR/journal-calls"
        printf '%s\n' 'qdwin: toplevel_added handle=4 uid=1000 pid=1941 app_id=python3'
        if [ "$journal_calls" -ge 2 ]; then
            printf '%s\n' 'qdwin: mapped handle=4 size=1280x800 pos=0,31 (normal)'
        fi
        if [ "$journal_calls" -ge 3 ]; then
            printf '%s\n' 'qdwin: focus handle=4 (was 3) seat=default'
        else
            printf '%s\n' 'qdwin: focus handle=3 (was 2) seat=default'
        fi
    }
}

@test "presentation capture waits for both normal map and latest focus" {
    pres_wait_window_front qdbrowser 4 >"$BATS_TEST_TMPDIR/out"
    [ "$(cat "$BATS_TEST_TMPDIR/journal-calls")" -ge 3 ]
    grep -q 'qdbrowser mapped and focused (handle=4)' "$BATS_TEST_TMPDIR/out"
}

@test "a covered window is never treated as capture-ready" {
    pres_admin() {
        printf '%s\n' 'qdwin: mapped handle=4 size=1280x800 pos=0,31 (normal)'
        printf '%s\n' 'qdwin: focus handle=5 (was 4) seat=default'
    }
    run pres_wait_window_front qdbrowser 1
    [ "$status" -eq 1 ]
    [[ "$output" == *'not mapped and focused'* ]]
}
