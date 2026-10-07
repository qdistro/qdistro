#!/usr/bin/env bats
# Host-only timing contract; live image boots remain the acceptance gate.

setup() {
    REPO="$(cd "$BATS_TEST_DIRNAME/../../.." && pwd)"
}

@test "stick extra omits only the two timed sleeps; primary retains both" {
    local block
    block="$(sed -n '/^shoot 02-fully-booted$/,/^#-- 10\./p' "$REPO/image/verify.sh" | sed '$d')"
    run bash -c 'shoot() { echo "shot:$1"; }; sleep() { echo "sleep:$1"; }; eval "$1"' _ "$block"
    [ "$status" -eq 0 ]
    [[ "$output" == *"shot:02-fully-booted"* ]]
    [[ "$output" == *"shot:04-after-60s"* ]]
    [ "$(printf '%s\n' "$output" | grep -c '^sleep:30$')" -eq 2 ]
    run env QDISTRO_VERIFY_PARENT=1 bash -c 'shoot() { echo "shot:$1"; }; sleep() { echo "sleep:$1"; }; eval "$1"' _ "$block"
    [ "$status" -eq 0 ]
    [[ "$output" == *"shot:02-fully-booted"* ]]
    [[ "$output" == *"shot:03-stick-extra-checkpoint"* ]]
    [[ "$output" == *"shot:04-stick-extra-final"* ]]
    [[ "$output" != *"sleep:30"* ]]
    # The required boot checklist remains before this screenshot/timing block,
    # and the matrix still propagates a failed extra to the parent result.
    grep -q 'expect "no failed systemd units after first boot"' "$REPO/image/verify.sh"
    grep -q 'extra_fail=$((extra_fail + 1))' "$REPO/image/verify.sh"
    grep -q 'stick extras: $extra_fail failed' "$REPO/image/verify.sh"
    grep -q 'expect "persist marker survived reboot"' "$REPO/image/verify.sh"
}

@test "qci pins the primary verify parent flag empty despite inherited 0" {
    local invocation
    invocation="$(sed -n '/^    QDISTRO_IMAGE="\$verify_src" QDISTRO_VERIFY_PARENT=/,+2p' "$REPO/ci/lib/gates/image.sh")"
    [ -n "$invocation" ]
    run env QDISTRO_VERIFY_PARENT=0 bash -c '
        verify_src=/tmp/primary.raw
        IMAGE_DIR=/tmp
        v_log="$1"
        bash() { printf "parent=%s image=%s login=%s\n" "$QDISTRO_VERIFY_PARENT" "$QDISTRO_IMAGE" "$QDISTRO_VERIFY_LOGIN"; }
        eval "$2"
        cat "$v_log"
    ' _ "$BATS_TEST_TMPDIR/verify.log" "$invocation"
    [ "$status" -eq 0 ]
    [ "$output" = 'parent= image=/tmp/primary.raw login=1' ]
}

stick_matrix() {
    # Execute the real matrix block from verify.sh with the child boot stubbed.
    local block
    block="$(sed -n '/^if \[ "\$STICK" = 1 \] && /,/^fi$/p' "$REPO/image/verify.sh")"
    [ -n "$block" ] || return 1
    bash -c '
        STICK=1 KEEP=0 VM=vm SSH_PORT=2200 HERE=/nonexistent IMG=/img BUILD_DIR=/b
        VERIFY_DIR="$2" PASS=0 FAIL=0 QDISTRO_RESOLVED_XZ=/img.raw.xz
        log() { :; }
        qdistro_pick_free_port() { echo 30123; }
        bash() { printf "child parent=%s login=%s args=%s\n" "$QDISTRO_VERIFY_PARENT" "$QDISTRO_VERIFY_LOGIN" "${*:2}"; }
        eval "$1"
        echo "fail=$FAIL"
    ' _ "$block" "$BATS_TEST_TMPDIR"
}

@test "stick matrix marks every extra boot as a child of the parent" {
    run stick_matrix
    [ "$status" -eq 0 ]
    [ "$(printf '%s\n' "$output" | grep -c '^child ')" -eq 7 ]
    [ "$(printf '%s\n' "$output" | grep -c '^child parent=1 login=0 ')" -eq 7 ]
    [[ "$output" == *"fail=0"* ]]
}

@test "an inherited parent flag other than 1 still runs as a primary" {
    local block
    block="$(sed -n '/^shoot 02-fully-booted$/,/^#-- 10\./p' "$REPO/image/verify.sh" | sed '$d')"
    run env QDISTRO_VERIFY_PARENT=0 bash -c 'shoot() { echo "shot:$1"; }; sleep() { echo "sleep:$1"; }; eval "$1"' _ "$block"
    [ "$status" -eq 0 ]
    [ "$(printf '%s\n' "$output" | grep -c '^sleep:30$')" -eq 2 ]
    QDISTRO_VERIFY_PARENT=0 run stick_matrix
    [ "$(printf '%s\n' "$output" | grep -c '^child parent=1 ')" -eq 7 ]
}
