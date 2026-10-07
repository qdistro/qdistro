#!/usr/bin/env bats
#
# Host-only tests for image/lib/sshd-wait.sh (sourced by image/verify.sh):
# start sshd over the guest agent and wait for SSH auth inside ONE absolute
# deadline. No VM: a file-backed fake clock, a fake agent (qga_root) and a
# fake SSH probe (remote). The clock lives in a file because the library
# calls the agent inside command substitutions (subshells).
#
# Why (sol r145): the first retry chain gave every agent step a fresh 60 s
# budget (a slow agent pushed a 600 s wait to 740 s), and the post-reboot
# SSH wait timed out silently, so agent-backed persistence checks passed on a
# guest whose sshd never came back.

setup() {
    REPO="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    T="$(mktemp -d)"
    VERIFY_DIR="$T/verify"
    CLOCK="$T/clock"; echo 1000 > "$CLOCK"
    OPS="$T/ops"; : > "$OPS"
    REFUSALS="$T/refusals"; echo 0 > "$REFUSALS"
    AGENT_COST=1      # seconds one agent step takes
    SSH_AUTH=1        # 0: sshd may run but auth never succeeds
    QDV_DIAG_TIMEOUT=15
    # shellcheck source=../../../image/lib/sshd-wait.sh
    . "$REPO/image/lib/sshd-wait.sh"
    qdv_now()   { cat "$CLOCK"; }
    qdv_sleep() { advance "$1"; }
    log()  { :; }
    warn() { :; }
}

teardown() { rm -rf "$T"; }

advance() { echo $(( $(cat "$CLOCK") + $1 )) > "$CLOCK"; }

# Fake agent: records "start-time|timeout|cmd", refuses `systemctl start` while
# the refusal counter is positive, and times out when a step costs more than
# its budget.
qga_root() {
    local cmd="$1" limit="${2:-60}" r
    printf '%s|%s|%s\n' "$(cat "$CLOCK")" "$limit" "$cmd" >> "$OPS"
    [ "$limit" -gt 0 ] || return 98
    if [ "$AGENT_COST" -gt "$limit" ]; then advance "$limit"; return 98; fi
    advance "$AGENT_COST"
    case "$cmd" in
        'systemctl start sshd.service')
            r=$(cat "$REFUSALS")
            if [ "$r" -gt 0 ]; then
                echo $((r - 1)) > "$REFUSALS"
                echo "Failed to start sshd.service: Access denied" >&2
                return 4
            fi
            touch "$T/sshd-up"; return 0 ;;
        'systemctl is-active --quiet sshd.service')
            [ -e "$T/sshd-up" ] ;;
        *) echo "diagnostics"; return 0 ;;
    esac
}

remote() {
    advance 1
    [ "$SSH_AUTH" = 1 ] && [ -e "$T/sshd-up" ]
}

# No agent step except the diagnostics may START at or after the deadline,
# and every step's budget must fit before it.
assert_ops_inside() {
    local deadline=$1 t limit cmd
    while IFS='|' read -r t limit cmd; do
        case "$cmd" in 'systemctl start sshd.service'|'systemctl is-active --quiet sshd.service') ;; *) continue ;; esac
        [ "$t" -lt "$deadline" ] || { echo "op started at $t >= deadline $deadline: $cmd" >&2; return 1; }
        [ $(( t + limit )) -le "$deadline" ] || { echo "op budget $limit at $t overruns $deadline: $cmd" >&2; return 1; }
    done < "$OPS"
}

@test "sshd-wait: recovery after one refused start (exit 4) records the reason and returns 0" {
    echo 1 > "$REFUSALS"
    local deadline=$(( $(cat "$CLOCK") + 600 )) rc=0
    wait_for_ssh boot "$deadline" 4 || rc=$?
    [ "$rc" -eq 0 ]
    [ "$SSHD_START_FAILS" -eq 1 ]
    [ "${SSHD_START_LOGS[0]}" = "$VERIFY_DIR/journal/sshd-start-boot-1.log" ]
    grep -qx 'exitcode=4' "${SSHD_START_LOGS[0]}"
    grep -q 'Access denied' "${SSHD_START_LOGS[0]}"
    [[ "$(qdv_ssh_failure_note)" == *"refused 1 time(s)"*"sshd-start-boot-1.log"* ]]
    assert_ops_inside "$deadline"
}

@test "sshd-wait: a permanently refused start fails within the deadline and keeps every diagnostic" {
    echo 100000 > "$REFUSALS"
    local t0 deadline rc=0
    t0=$(cat "$CLOCK"); deadline=$(( t0 + 600 ))
    wait_for_ssh boot "$deadline" 4 || rc=$?
    [ "$rc" -eq 1 ]
    [ "$SSHD_START_FAILS" -ge 2 ]
    [ "${#SSHD_START_LOGS[@]}" -eq "$SSHD_START_FAILS" ]
    for f in "${SSHD_START_LOGS[@]}"; do [ -s "$f" ]; done
    [ "$(cat "$CLOCK")" -le $(( deadline + QDV_DIAG_TIMEOUT )) ]
    assert_ops_inside "$deadline"
}

@test "sshd-wait: a slow agent (55 s per step) near the deadline cannot push a 600 s wait past it" {
    echo 100000 > "$REFUSALS"
    AGENT_COST=55
    local t0 deadline rc=0
    t0=$(cat "$CLOCK"); deadline=$(( t0 + 600 ))
    wait_for_ssh boot "$deadline" 4 || rc=$?
    [ "$rc" -eq 1 ]
    echo "elapsed=$(( $(cat "$CLOCK") - t0 ))" >&2
    # Only the separately bounded diagnostics may run past the deadline
    # (r145 measured elapsed=740 here before the fix).
    [ "$(cat "$CLOCK")" -le $(( deadline + QDV_DIAG_TIMEOUT )) ]
    assert_ops_inside "$deadline"
}

@test "sshd-wait: a slow agent near a short (180 s) post-reboot deadline stays inside it" {
    echo 100000 > "$REFUSALS"
    AGENT_COST=55
    local t0 deadline rc=0
    t0=$(cat "$CLOCK"); deadline=$(( t0 + 180 ))
    wait_for_ssh reboot "$deadline" 4 || rc=$?
    [ "$rc" -eq 1 ]
    [ "$(cat "$CLOCK")" -le $(( deadline + QDV_DIAG_TIMEOUT )) ]
    assert_ops_inside "$deadline"
}

@test "sshd-wait: an expired deadline starts no agent step at all" {
    local rc=0
    start_sshd late $(( $(cat "$CLOCK") - 1 )) || rc=$?
    [ "$rc" -eq 98 ]
    rc=0
    ensure_sshd late "$(cat "$CLOCK")" || rc=$?
    [ "$rc" -eq 98 ]
    [ ! -s "$OPS" ]
}

# The REAL post-reboot block of image/verify.sh, from its SSH wait through
# the agent-backed persistence assertions, run with SSH permanently down
# while the agent (and so every persistence check) is healthy.
@test "verify.sh: SSH that never comes back after reboot is fatal even though agent persistence checks would pass" {
    local block
    block=$(awk '/if ! wait_for_ssh reboot/{f=1} f{print} /swap still active after reboot/{if(f){getline; print; exit}}' \
        "$REPO/image/verify.sh")
    [[ "$block" == *"wait_for_ssh reboot"* ]]
    [[ "$block" == *"persist marker survived reboot"* ]]
    SSH_AUTH=0
    run bash -c '
        set -euo pipefail
        REPO=$1 T=$2 CLOCK=$3 OPS=$4 REFUSALS=$5 VERIFY_DIR=$6 block=$7
        . "$REPO/image/lib/sshd-wait.sh"
        advance() { echo $(( $(cat "$CLOCK") + $1 )) > "$CLOCK"; }
        qdv_now() { cat "$CLOCK"; }; qdv_sleep() { advance "$1"; }
        date() { if [ "${1:-}" = +%s ]; then cat "$CLOCK"; else command date "$@"; fi; }
        log() { :; }; warn() { :; }
        qga_root() { advance 1; [ "$1" = "systemctl start sshd.service" ] && touch "$T/sshd-up"; return 0; }
        remote() { advance 1; return 1; }
        PASS=0
        expect() { local d=$1; shift; "$@" >/dev/null 2>&1 && PASS=$((PASS+1)); echo "PASS: $d"; }
        shoot() { echo "shot $1"; }
        die() { echo "FATAL: $*"; exit 1; }
        eval "$block"
        echo "reached-end PASS=$PASS"
    ' _ "$REPO" "$T" "$CLOCK" "$OPS" "$REFUSALS" "$VERIFY_DIR" "$block"
    echo "$output" >&2
    [ "$status" -ne 0 ]
    [[ "$output" == *"shot 99-ssh-timeout-reboot"* ]]
    [[ "$output" == *"FATAL: SSH never came back within 180s after reboot"* ]]
    [[ "$output" != *"PASS: persist marker survived reboot"* ]]
    [[ "$output" != *"reached-end"* ]]
}
