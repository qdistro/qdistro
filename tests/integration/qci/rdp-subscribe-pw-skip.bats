#!/usr/bin/env bats
#
# Host-only contract for qdwin/tests/apps/13-rdp-subscribe-frame.md.
# A non-pipewire bake denies subscribe_view_stream with "(no pw output)" /
# "no free pipewire output". That denial must exit 77 before either
# missing-approval failure. No VM, no libvirt.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    SCENARIO="$REPO_ROOT/qdwin/tests/apps/13-rdp-subscribe-frame.md"
    STEP=$(awk '
        /^### Step 1 / {p=1}
        p && /^```bash$/ {if (!inblock) {inblock=1; next}}
        inblock && /^```$/ {exit}
        inblock {print}
    ' "$SCENARIO")
    FUNC=$(printf '%s\n' "$STEP" | awk '
        /^rdp_skip_if_no_pipewire_output\(\) \{/ {p=1}
        p {print}
        p && /^}$/ {exit}
    ')
    [ -n "$STEP" ]
    [ -n "$FUNC" ]
    # shellcheck disable=SC1090
    eval "$FUNC"
    HANDLE=7
    SUBSCRIBE_CURSOR=cursor-1
}

# Same filter the scenario passes to qdwin_apps_log_since_cursor: extended
# regexp over the journal delta since the subscribe cursor.
qdwin_apps_log_since_cursor() {
    local cursor=$1 pattern=$2
    [ "$cursor" = "$SUBSCRIBE_CURSOR" ] || return 2
    printf '%s\n' "$STUB_JOURNAL" | grep -E "$pattern"
}

@test "step 1 subscribe bash parses" {
    printf '%s\n' "$STEP" | bash -n
}

@test "no-pw skip is exit 77 and precedes both missing-approval fails" {
    local skip_line cred_line pid_line calls
    skip_line=$(printf '%s\n' "$STEP" | grep -n 'exit 77' | head -1 | cut -d: -f1)
    cred_line=$(printf '%s\n' "$STEP" | grep -n 'FAIL: approved credentials did not arrive' | head -1 | cut -d: -f1)
    pid_line=$(printf '%s\n' "$STEP" | grep -n 'FAIL: approved event lacks forward PID' | head -1 | cut -d: -f1)
    [ -n "$skip_line" ]
    [ -n "$cred_line" ]
    [ -n "$pid_line" ]
    [ "$skip_line" -lt "$cred_line" ]
    [ "$skip_line" -lt "$pid_line" ]
    calls=$(printf '%s\n' "$STEP" | grep -c 'rdp_skip_if_no_pipewire_output' || true)
    # definition plus the two missing-approval branches, not the port check
    [ "$calls" -eq 3 ]
    printf '%s\n' "$STEP" | grep -q 'FAIL: approved event returned invalid RDP port'
    ! printf '%s\n' "$STEP" | grep -A2 'invalid RDP port' | grep -q 'rdp_skip_if_no_pipewire_output'
}

@test "journal (no pw output) at end of the compositor record skips" {
    STUB_JOURNAL='Sep 24 12:00:00 host qdwin[1]: qdwin: subscribe_view_stream denied handle=7 peer_label="admin" (no pw output)'
    run rdp_skip_if_no_pipewire_output
    [ "$status" -eq 77 ]
    [[ "$output" == SKIP:*qdwin_apps_log_since_cursor* ]]
    [[ "$output" == *"(no pw output)"* ]]
    [[ "$output" == *"handle=7"* ]]
    [[ "$output" != *"no free pipewire output"* ]]
}

@test "reason text inside peer_label or a quoted copy does not skip" {
    local sample
    for sample in \
        'qdwin: subscribe_view_stream denied handle=7 peer_label="no pw output" (getrandom failed)' \
        'qdwin: subscribe_view_stream denied handle=7 peer_label="admin" (no pw output) trailing' \
        'diagnostic: previous text="subscribe_view_stream denied handle=7 (no pw output)"' \
        'qdwin: subscribe_view_stream denied handle=7 peer_label="admin" no free pipewire output' \
        'qdwin: subscribe_view_stream denied handle=7 (nested-proxy pending admin decision)' \
        'qdwin: subscribe_view_stream denied handle=7 peer_label="admin" (getrandom failed)' \
        'qdwin: subscribe_view_stream denied handle=77 peer_label="admin" (no pw output)' \
        'qdwin: view_stream approved handle=7 pw=pipewire-1 forward_pid=42' \
        ''
    do
        STUB_JOURNAL=$sample
        run rdp_skip_if_no_pipewire_output
        [ "$status" -eq 0 ]
        [ -z "$output" ]
    done
}

_run_step() {
    local exe="$BATS_TEST_TMPDIR/vm-exec"
    printf '%s\n' '#!/bin/bash' 'printf "%s\n" "$STUB_CREDS"' > "$exe"
    chmod +x "$exe"
    export STEP SUBSCRIBE_CURSOR STUB_JOURNAL STUB_CREDS
    export HANDLE=7
    export QDWIN_VM_EXEC="$exe"
    export VMNAME=vm
    run bash -c '
        qdwin_apps_journal_cursor() { printf "%s\n" "$SUBSCRIBE_CURSOR"; }
        qdwin_apps_ctl() { return 0; }
        qdwin_apps_log_since_cursor() {
            local cursor=$1 pattern=$2
            [ "$cursor" = "$SUBSCRIBE_CURSOR" ] || return 2
            printf "%s\n" "$STUB_JOURNAL" | grep -E "$pattern"
        }
        eval "$STEP"
    '
}

@test "missing credentials without the no-pw record still fail" {
    STUB_JOURNAL='qdwin: subscribe_view_stream denied handle=7 peer_label="no pw output" (getrandom failed)'
    STUB_CREDS=''
    _run_step
    [ "$status" -eq 1 ]
    [[ "$output" == *"FAIL: approved credentials did not arrive"* ]]
    [[ "$output" != SKIP:* ]]
}

@test "credentials without an approved forward still fail when the record is absent" {
    STUB_JOURNAL='qdwin: subscribe_view_stream denied handle=7 peer_label="admin" (getrandom failed)'
    STUB_CREDS=$'RDP_PASSWORD=secret\nPIPEWIRE_NODE_NAME=n\nRDP_PORT=3389\nRDP_CERT_PATH=/c\nHANDLE=7'
    _run_step
    [ "$status" -eq 1 ]
    [[ "$output" == *"FAIL: approved event lacks forward PID"* ]]
    [[ "$output" != SKIP:* ]]
}
