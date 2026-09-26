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
    export QCI_DRIVER_CLAIM_TEST_PROC_FALLBACK=1
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
    local lib=${1:-$REPO_ROOT/ci/lib/guest/gui-waiters.sh}
    _render_prompt \
        | sed -n '/^- CLAIM THE GUEST DRIVER/,/^  Piping the decoded script/p' \
        | sed -n 's/^      //p' \
        | sed -e "s#/tmp/qci-gui-waiters.sh#$lib#" \
              -e "s#/tmp/qci/#$BATS_TEST_TMPDIR/qci/#"
}

@test "runtime prompt tells the guest driver to claim a per-scenario lock" {
    local p; p=$(_render_prompt)
    printf '%s\n' "$p" | grep -qx '      source /tmp/qci-gui-waiters.sh || exit 2'
    printf '%s\n' "$p" | grep -qx "      qci_claim_driver /tmp/qci/$SLUG/driver.lock || exit 2"
    printf '%s\n' "$p" | grep -q 'ERROR: a second guest driver is already running'
    printf '%s\n' "$p" | grep -q 'do not delete the lock'
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
    [ "${lines[0]}" = "ERROR: a second guest driver is already running: $lock" ]
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
    grep -q 'qci_claim_driver /tmp/qci/<slug>/driver.lock || exit 2' \
        "$REPO_ROOT/ci/prompts/gui-scenario-agent.md"
}

@test "the prompt's claim lines fail closed when the library is missing" {
    local snip side
    snip=$(_claim_snippet "$BATS_TEST_TMPDIR/no-such-waiters.sh")
    side="$BATS_TEST_TMPDIR/side"
    run bash -c "$snip"'
        echo RAN > "$1"
    ' _ "$side"
    [ "$status" -eq 2 ]
    [ ! -e "$side" ]
}

@test "the prompt's claim lines fail closed on a stale library without the claim" {
    local snip side stale
    stale="$BATS_TEST_TMPDIR/old-waiters.sh"
    sed '/^qci_claim_driver()/,$d' "$REPO_ROOT/ci/lib/guest/gui-waiters.sh" > "$stale"
    ! grep -q '^qci_claim_driver()' "$stale"
    bash -n "$stale"
    snip=$(_claim_snippet "$stale")
    side="$BATS_TEST_TMPDIR/side"
    run bash -c "$snip"'
        echo RAN > "$1"
    ' _ "$side"
    [ "$status" -eq 2 ]
    [ ! -e "$side" ]
}

@test "an app the driver left running does not keep the claim after the driver exits" {
    # full-20260926T153217Z-3807077: permissions-gui/44's first driver
    # launched the admin app (qdistro-start-admin-app daemonizes it) and then
    # exited on a waiter timeout; the app inherited the old in-shell lock fd,
    # and both retries were refused "a second guest driver is already
    # running" with no driver alive. The claim is the driver SHELL's.
    local snip side ready fifo
    snip=$(_claim_snippet)
    side="$BATS_TEST_TMPDIR/second-side"
    ready="$BATS_TEST_TMPDIR/child-ready"
    fifo="$BATS_TEST_TMPDIR/child-hold"
    mkfifo "$fifo"
    bash -c "$snip"'
        setsid -f bash -c '"'"': > "$1"; exec 3<>"$2"; read -t 30 -u 3 || true'"'"' _ "$1" "$2" \
            </dev/null >/dev/null 2>&1
    ' _ "$ready" "$fifo"
    local i
    for i in $(seq 1 50); do [ -f "$ready" ] && break; sleep 0.1; done
    [ -f "$ready" ]
    run timeout 10 bash -c "$snip"'
        echo RAN > "$1"
    ' _ "$side"
    # Release the child: a writer on the fifo ends its read.
    echo go > "$fifo"
    [ "$status" -eq 0 ]
    [ "$(cat "$side")" = RAN ]
}

@test "runtime prompt gates host steps with qci_host_step and says not to wait on the driver first" {
    local p; p=$(_render_prompt)
    printf '%s\n' "$p" | grep -q 'qci_host_step <name>'
    printf '%s\n' "$p" | grep -q 'qci_claim_done'
    printf '%s\n' "$p" | grep -q "/tmp/qci/$SLUG/waiting"
    printf '%s\n' "$p" | grep -q "mkdir /tmp/qci/$SLUG/<token>.go"
    printf '%s\n' "$p" | grep -q 'THE DRIVER DOES NOT RETURN UNTIL YOU HAVE DONE ITS HOST STEPS'
    # The old advice, a hand-rolled await_file on a touched path, is gone.
    run grep -q 'await_file. on a path you' <<<"$p"
    [ "$status" -eq 1 ]
    # The prompt no longer says launched apps keep the claim.
    run grep -q 'AND every' <<<"$p"
    [ "$status" -eq 1 ]
    grep -q 'qci_host_step <name>' "$REPO_ROOT/ci/prompts/gui-scenario-agent.md"
    grep -q 'qci_claim_done' "$REPO_ROOT/ci/prompts/gui-scenario-agent.md"
    grep -q 'mkdir. that exact token' "$REPO_ROOT/ci/prompts/gui-scenario-agent.md"
}

@test "the prompt's claim lines and qci_host_step: the host reads the token and releases the step" {
    local snip out tok="" i
    snip=$(_claim_snippet)
    out="$BATS_TEST_TMPDIR/driver.out"
    local d="$BATS_TEST_TMPDIR/qci/$SLUG"
    bash -c "$snip"'
        qci_host_step s1 20 > "$1" 2>&1
        echo AFTER-S1 >> "$1"
    ' _ "$out" &
    local drv=$!
    for i in $(seq 1 100); do
        tok=$(cat "$d/waiting" 2>/dev/null) || tok=""
        [ "${tok%%.*}" = s1 ] && break
        sleep 0.1
    done
    [ "${tok%%.*}" = s1 ]
    # Exactly what the prompt tells the host to run.
    mkdir "$d/$tok.go"
    wait "$drv"
    grep -q AFTER-S1 "$out"
}

@test "two different scenarios claim concurrently without contending" {
    local snip other side ready fifo
    snip=$(_claim_snippet)
    # Render the second scenario's prompt on its own; do not rewrite the first.
    other=$(SLUG=qdlocker_09-capture-indicators _claim_snippet)
    [[ "$other" == *"/qci/qdlocker_09-capture-indicators/driver.lock"* ]]
    side="$BATS_TEST_TMPDIR/other-side"
    ready="$BATS_TEST_TMPDIR/ready"
    fifo="$BATS_TEST_TMPDIR/hold"
    mkfifo "$fifo"
    bash -c "$snip"'
        : > "$1"; exec 3<>"$2"; read -t 30 -u 3 || true
    ' _ "$ready" "$fifo" &
    local holder=$! i
    for i in $(seq 1 50); do [ -f "$ready" ] && break; sleep 0.1; done
    [ -f "$ready" ] || { kill "$holder"; wait "$holder" || true; return 1; }
    run timeout 5 bash -c "$other"'
        echo RAN > "$1"
    ' _ "$side"
    kill "$holder" 2>/dev/null || true
    wait "$holder" 2>/dev/null || true
    [ "$status" -eq 0 ]
    [ "$(cat "$side")" = RAN ]
}
