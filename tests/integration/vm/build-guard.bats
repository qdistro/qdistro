#!/usr/bin/env bats
# Unit tests for image/lib/build-guard.sh — the liveness/kill helpers behind
# the in-VM kiwi retry loop (image/build-in-vm.sh). VM-free and rootless: the
# process-tree helpers are exercised with real setsid'd process trees, the
# mount parser with a synthetic /proc/mounts. The root-only cleanup
# (guard_cleanup_target) is exercised in the builder VM by build-in-vm.sh's
# fault-injection run (QDISTRO_KIWI_FAULT_KILL_AT_S), not here.

setup() {
    REPO="$(cd "$BATS_TEST_DIRNAME/../../.." && pwd)"
    GUARD="$REPO/image/lib/build-guard.sh"
    [ -f "$GUARD" ]
    source "$GUARD"
}

teardown() {
    [ -n "${TREE_ROOT:-}" ] && guard_kill_tree "$TREE_ROOT" 5 >/dev/null 2>&1 || true
}

# Start a detached tree: root bash (own session) -> child sh spinner or
# sleeper -> grandchild. The root writes its own pid to $1.
start_tree() {
    local pidfile=$1 mode=$2
    if [ "$mode" = busy ]; then
        setsid bash -c 'echo $$ >"$0"; sh -c "while :; do :; done" & sleep 300 & wait' "$pidfile" >/dev/null 2>&1 &
    else
        setsid bash -c 'echo $$ >"$0"; sh -c "sleep 300" & sleep 300 & wait' "$pidfile" >/dev/null 2>&1 &
    fi
    for _ in $(seq 1 50); do [ -s "$pidfile" ] && break; sleep 0.1; done
    TREE_ROOT=$(cat "$pidfile")
    sleep 0.3
}

@test "build-guard: guard_tree_pids collects the root and every descendant" {
    start_tree "$BATS_TEST_TMPDIR/pid" idle
    run guard_tree_pids "$TREE_ROOT"
    [ "${lines[0]}" = "$TREE_ROOT" ]
    # root bash and two sleeps at least (sh may exec its single command)
    [ "${#lines[@]}" -ge 3 ]
    local sleeps=0; for p in "${lines[@]}"; do [ "$(cat /proc/$p/comm)" = sleep ] && sleeps=$(( sleeps + 1 )); done
    [ "$sleeps" -eq 2 ]
    for p in "${lines[@]}"; do [ -d "/proc/$p" ]; done
}

@test "build-guard: a busy tree burns ticks, an idle tree burns none" {
    start_tree "$BATS_TEST_TMPDIR/busy" busy
    local a b; a=$(guard_tree_cpu "$TREE_ROOT"); sleep 2; b=$(guard_tree_cpu "$TREE_ROOT")
    # a spinning shell burns ~100 ticks/s; the loop's floor is 100 per 15 s
    [ $(( b - a )) -ge 100 ]
    guard_kill_tree "$TREE_ROOT" 5
    start_tree "$BATS_TEST_TMPDIR/idle" idle
    a=$(guard_tree_cpu "$TREE_ROOT"); sleep 2; b=$(guard_tree_cpu "$TREE_ROOT")
    [ $(( b - a )) -lt 5 ]
}

@test "build-guard: the stat parser survives a comm with ') ' in it" {
    # comm is the executable's basename, so run a sleep binary under an
    # adversarial name; the parser must read the fields after the LAST ') '.
    mkdir -p "$BATS_TEST_TMPDIR/bin"
    cp "$(command -v sleep)" "$BATS_TEST_TMPDIR/bin/x) 1 2"
    local pidfile="$BATS_TEST_TMPDIR/pid"
    setsid bash -c 'echo $$ >"$0"; "$1" 300 & wait' "$pidfile" "$BATS_TEST_TMPDIR/bin/x) 1 2" >/dev/null 2>&1 &
    for _ in $(seq 1 50); do [ -s "$pidfile" ] && break; sleep 0.1; done
    TREE_ROOT=$(cat "$pidfile"); sleep 0.3
    local child; child=$(pgrep -P "$TREE_ROOT" | head -1)
    grep -q 'x) 1 2' "/proc/$child/stat"
    run guard_tree_cpu "$TREE_ROOT"
    [ "$status" -eq 0 ]
    [[ "$output" =~ ^[0-9]+$ ]]
    run guard_tree_dstate "$TREE_ROOT"
    [ "$output" = 0 ]
}

@test "build-guard: guard_kill_tree kills the whole tree and only the tree" {
    start_tree "$BATS_TEST_TMPDIR/pid" busy
    local pids; pids=$(guard_tree_pids "$TREE_ROOT")
    # a bystander that must survive
    sleep 300 &
    local bystander=$!
    run guard_kill_tree "$TREE_ROOT" 10
    [ "$status" -eq 0 ]
    for p in $pids; do
        if [ -d "/proc/$p" ]; then
            [ "$(awk '{ sub(/^.*\) /, ""); print $1 }' /proc/$p/stat)" = Z ]
        fi
    done
    kill -0 "$bystander"
    kill "$bystander"
    TREE_ROOT=""
}

@test "build-guard: a descendant that reparented before the kill still dies" {
    # Double-fork so the grandchild's parent exits and it reparents to init
    # (or a subreaper) — invisible to a pgrep -P walk, but in the same
    # process group as the setsid'd root, so the group kill catches it.
    local pidfile="$BATS_TEST_TMPDIR/pid" orphan="$BATS_TEST_TMPDIR/orphan"
    setsid bash -c 'echo $$ >"$0"; (sh -c "echo \$\$ >\"$1\"; sleep 300" &) ; sleep 300' "$pidfile" "$orphan" >/dev/null 2>&1 &
    for _ in $(seq 1 50); do [ -s "$pidfile" ] && [ -s "$orphan" ] && break; sleep 0.1; done
    TREE_ROOT=$(cat "$pidfile"); local o; o=$(cat "$orphan")
    sleep 0.5
    # Precondition: the orphan is NOT in the walked tree any more.
    run guard_tree_pids "$TREE_ROOT"
    [[ " $output " != *" $o "* ]]
    guard_kill_tree "$TREE_ROOT" 10
    sleep 0.5
    ! kill -0 "$o" 2>/dev/null
    TREE_ROOT=""
}

@test "build-guard: guard_mounts_under is anchored, deepest-first, and decodes \\040" {
    local fake="$BATS_TEST_TMPDIR/mounts"
    printf '%s\n' \
        'a /build/out xfs rw 0 0' \
        'b /build/output-other ext4 rw 0 0' \
        'c /build/out/build/image-root/proc proc rw 0 0' \
        'd /build/out/build/image-root ext4 rw 0 0' \
        'e /build/out/with\040space tmpfs rw 0 0' \
        'f /var/tmp/kiwi_volumes.abc btrfs rw 0 0' > "$fake"
    # shellcheck disable=SC2317
    guard_mounts_under() {
        local dir=$1
        awk -v d="$dir" '{ m = $2; gsub(/\\040/, " ", m); if (m == d || index(m, d "/") == 1) print m }' "$fake" \
            | awk '{ print length($0) "\t" $0 }' | sort -rn | cut -f2-
    }
    run guard_mounts_under /build/out
    [ "${lines[0]}" = "/build/out/build/image-root/proc" ]
    [ "${lines[1]}" = "/build/out/build/image-root" ]
    [ "${lines[2]}" = "/build/out/with space" ]
    [ "${lines[3]}" = "/build/out" ]
    [ "${#lines[@]}" -eq 4 ]
    run guard_mounts_under /var/tmp
    [ "$output" = "/var/tmp/kiwi_volumes.abc" ]
}

@test "build-guard: guard_rx_bytes sums every interface except lo" {
    run guard_rx_bytes
    [[ "$output" =~ ^[0-9]+$ ]]
    local lo; lo=$(awk '/^ *lo:/ { sub(/^ +/, ""); split($0, a, ":"); split(a[2], f, " "); print f[1] }' /proc/net/dev)
    local all; all=$(awk 'NR > 2 { sub(/^ +/, ""); split($0, a, ":"); split(a[2], f, " "); s += f[1] } END { print s + 0 }' /proc/net/dev)
    [ "$output" -le "$all" ]
    [ $(( all - output )) -ge "$lo" ] || [ "$lo" = 0 ]
}
