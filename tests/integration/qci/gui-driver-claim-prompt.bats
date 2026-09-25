#!/usr/bin/env bats
#
# Host-only: the runtime GUI prompt (write_agent_prompt) tells the agent to
# claim its guest driver with qci_claim_driver, and the commands it prints
# really do stop a second driver.
#
# Why: killing the host vm-exec does not kill the guest driver shell. In
# gui-20260924T193011Z-2597819 luna re-issued permissions-gui/08's driver and
# four copies ran at once; one read another's stale rc. qci_claim_driver
# (ci/lib/guest/gui-waiters.sh) only helps if the prompt makes every driver
# take it, on a per-scenario path.
#
# The tests run the EXACT lines the prompt prints, not a copy of them. Only
# the guest paths are rebased onto the test tmpdir.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/core.sh"
    source "$REPO_ROOT/ci/lib/gates/gui.sh"
    SLUG=permissions-gui_08-admin-app-survives-broker-restart
}

_render_prompt() {
    RDIR="$BATS_TEST_TMPDIR/run"
    QDISTRO_REPO="$BATS_TEST_TMPDIR/qdistro"
    mkdir -p "$RDIR" "$QDISTRO_REPO"
    write_agent_prompt \
        qci-vm-1 "$QDISTRO_REPO/tests/integration/permissions-gui/08.md" \
        "$BATS_TEST_TMPDIR/prompt.txt" "$BATS_TEST_TMPDIR/art" \
        "$BATS_TEST_TMPDIR/scratch" "$SLUG"
    cat "$BATS_TEST_TMPDIR/prompt.txt"
}

# The indented command lines of the CLAIM bullet, guest paths rebased.
_claim_snippet() {
    _render_prompt \
        | sed -n '/^- CLAIM THE GUEST DRIVER/,/^  The claim is held/p' \
        | sed -n 's/^      //p' \
        | sed -e "s#/tmp/qci-gui-waiters.sh#$REPO_ROOT/ci/lib/guest/gui-waiters.sh#" \
              -e "s#/tmp/qci/#$BATS_TEST_TMPDIR/qci/#"
}

@test "runtime prompt tells the guest driver to claim a per-scenario lock" {
    local p; p=$(_render_prompt)
    printf '%s\n' "$p" | grep -qx '      source /tmp/qci-gui-waiters.sh'
    printf '%s\n' "$p" | grep -qx "      qci_claim_driver /tmp/qci/$SLUG/driver.lock"
    printf '%s\n' "$p" | grep -q 'ERROR: a second guest driver is already running'
    printf '%s\n' "$p" | grep -q 'do not delete the lock file'
}

@test "the prompt's claim lines stop a second concurrent driver" {
    local snip side ready fifo lock
    snip=$(_claim_snippet)
    [ "$(printf '%s\n' "$snip" | wc -l)" -eq 2 ]
    side="$BATS_TEST_TMPDIR/second-side"
    ready="$BATS_TEST_TMPDIR/ready"
    fifo="$BATS_TEST_TMPDIR/hold"
    lock="$BATS_TEST_TMPDIR/qci/$SLUG/driver.lock"
    mkfifo "$fifo"
    bash -c "$snip"'
        : > "$1"
        exec 3<>"$2"
        read -t 30 -u 3 || true
    ' _ "$ready" "$fifo" &
    local holder=$! i
    for i in $(seq 1 50); do [ -f "$ready" ] && break; sleep 0.1; done
    [ -f "$ready" ] || { kill "$holder"; wait "$holder" || true; return 1; }
    run timeout 5 bash -c "$snip"'
        echo RAN > "$1"
    ' _ "$side"
    kill "$holder" 2>/dev/null || true
    wait "$holder" 2>/dev/null || true
    [ "$status" -eq 1 ]
    [ "$output" = "ERROR: a second guest driver is already running: $lock" ]
    [ ! -e "$side" ]
}

@test "the prompt's claim lines let a driver run once the first has exited" {
    local snip side
    snip=$(_claim_snippet)
    side="$BATS_TEST_TMPDIR/side"
    bash -c "$snip"
    run bash -c "$snip"'
        echo RAN > "$1"
    ' _ "$side"
    [ "$status" -eq 0 ]
    [ "$(cat "$side")" = RAN ]
}

@test "documented prompt template carries the same claim rule" {
    grep -q 'qci_claim_driver /tmp/qci/<slug>/driver.lock' \
        "$REPO_ROOT/ci/prompts/gui-scenario-agent.md"
}
