#!/usr/bin/env bats
#
# Host-only guards for cross-user port/path isolation on a shared test host.
# Several qci users may share one machine with separate libvirt sessions;
# loopback TCP/UDP ports and /tmp are HOST-GLOBAL, so fixed defaults there
# collide across users. These tests pin:
#
#   * mmnet-alloc.sh   — reservation state is host-shared (one namespace for
#                        every uid), stale slots are pruned via /proc (kill -0
#                        EPERMs on foreign pids), and reserve/release works.
#   * host-port.sh     — qdistro_pick_free_port honours a busy port and a
#                        range that leaves nothing free.
#   * fresh-vm-bootstrap.sh — QDISTRO_HTTP_HOST is required (no baked 8765).
#   * verify.sh / install-test.sh — no fixed SSH ports (2299/2300).
#   * build-enforcing-baseweed.sh — kernel-assigned port by default; the
#                        port-reclaim path exists only behind --http-port.
#   * vm-gui           — screenshot default is uid-qualified, not a bare
#                        /tmp/vm-screenshot.png every user would share.
#
# All assertions are static greps or pure-host function calls — no VM boots.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    VM="$REPO_ROOT/scripts/vm"
    IMAGE="$REPO_ROOT/image"
    TDIR="$(mktemp -d)"
}

teardown() {
    rm -rf "$TDIR"
}

# ---------------------------------------------------------------------------
# mmnet-alloc.sh
# ---------------------------------------------------------------------------

@test "mmnet-alloc: default state dir is host-shared and sticky" {
    # The UDP port space is host-global; a per-uid reservation dir let two
    # users reserve the SAME port pair and bridge their "isolated" segments.
    run grep -n 'qdistro-mmnet-$(id -u)' "$VM/mmnet-alloc.sh"
    [ "$status" -ne 0 ]
    run grep -nF 'MMNET_STATE_DIR:-/tmp/qdistro-mmnet' "$VM/mmnet-alloc.sh"
    [ "$status" -eq 0 ]
    # The dir must be sticky (1777): without it, any user could unlink or
    # rename a peer's live reservation — or worse, swap a lockfile for a
    # symlink into a victim's files.
    run grep -n 'chmod 1777' "$VM/mmnet-alloc.sh"
    [ "$status" -eq 0 ]
}

@test "mmnet-alloc: lock is the dir inode, not an unlinkable lockfile" {
    # flock(2) accepts directory fds; locking the dir itself removes the
    # unlink-the-lockfile / symlink-the-lockfile attack class entirely.
    run grep -n 'exec 9<"' "$VM/mmnet-alloc.sh"
    [ "$status" -eq 0 ]
    # The fd we locked must BE the live dir: compare dev:inode (a path-string
    # compare is defeated by a same-path symlink both sides resolve).
    run grep -nF '/proc/$$/fd/9' "$VM/mmnet-alloc.sh"
    [ "$status" -eq 0 ]
    # -L is mandatory: stat without it reports the procfs symlink's own
    # inode, not the target's.
    run grep -nF "stat -Lc '%d:%i'" "$VM/mmnet-alloc.sh"
    [ "$status" -eq 0 ]
    # No separate lockfile path may remain in code.
    run grep -nE '[^#]\.lock' "$VM/mmnet-alloc.sh"
    [ "$status" -ne 0 ]
}

@test "mmnet-alloc: foreign-owned reservations are never pruned or released" {
    # Under hidepid=2 a foreign LIVE pid looks dead — prune must be gated on
    # file ownership, not just pid liveness. release must refuse a foreign
    # reservation rather than silently rm-fail (or worse, succeed as dir
    # owner).
    run grep -nF -- '[ -O "$f" ] || continue' "$VM/mmnet-alloc.sh"
    [ "$status" -eq 0 ]
    run grep -nF -- '[ ! -O "$resv" ]' "$VM/mmnet-alloc.sh"
    [ "$status" -eq 0 ]
    run grep -n 'refusing to release foreign-owned' "$VM/mmnet-alloc.sh"
    [ "$status" -eq 0 ]
}

@test "mmnet-alloc: state dir symlink is refused WITHOUT touching the target" {
    # Historical hazard: chmod 1777 by path follows a planted symlink and
    # silently makes a caller-owned private directory world-writable. The
    # script must refuse the link before any mode change — assert the target
    # keeps its original mode.
    local victim="$TDIR/private-dir"
    mkdir -p "$victim"
    chmod 0700 "$victim"
    export MMNET_STATE_DIR="$TDIR/mmnet-link"
    export MMNET_SEED_SPACE=4
    ln -s "$victim" "$MMNET_STATE_DIR"
    run "$VM/mmnet-alloc.sh" reserve
    [ "$status" -ne 0 ]
    [ "$(stat -c %a "$victim")" = "700" ]
    # The path itself is never chmod'ed — mode is fixed through /proc fd.
    run grep -nF 'chmod 1777 "/proc/$$/fd/9"' "$VM/mmnet-alloc.sh"
    [ "$status" -eq 0 ]
}

@test "mmnet-alloc: an existing reservation file is never overwritten" {
    # The create must be O_EXCL-atomic (python os.open; bash noclobber only
    # refuses REGULAR files — `>` on an existing FIFO opens and blocks).
    # Seed space of 1 makes the target path deterministic: pre-occupy it with
    # a sentinel and assert the script refuses AND leaves the bytes alone.
    export MMNET_STATE_DIR="$TDIR/mmnet-oc"
    export MMNET_SEED_SPACE=1
    . "$VM/mmnet-config.sh"
    pa=$(mmnet_local_port a 0)
    mkdir -p "$MMNET_STATE_DIR"
    printf 'sentinel\n' >"$MMNET_STATE_DIR/port-$pa.reserved"
    run "$VM/mmnet-alloc.sh" reserve
    [ "$status" -ne 0 ]
    [ "$(cat "$MMNET_STATE_DIR/port-$pa.reserved")" = "sentinel" ]
    run grep -n 'O_CREAT | os.O_EXCL' "$VM/mmnet-alloc.sh"
    [ "$status" -eq 0 ]
}

@test "mmnet-alloc: a FIFO at the reservation path fails fast, no flock hang" {
    # Regression for the bash-noclobber hole: `>` under set -C opens an
    # existing FIFO and blocks (holding the shared flock). With real O_EXCL
    # the open fails EEXIST. Seed space 1 → deterministic path; timeout is
    # the assertion (a hang = failure).
    export MMNET_STATE_DIR="$TDIR/mmnet-fifo"
    export MMNET_SEED_SPACE=1
    . "$VM/mmnet-config.sh"
    pa=$(mmnet_local_port a 0)
    mkdir -p "$MMNET_STATE_DIR"
    mkfifo "$MMNET_STATE_DIR/port-$pa.reserved"
    run timeout 15 "$VM/mmnet-alloc.sh" reserve
    [ "$status" -ne 0 ]
    [ "$status" -ne 124 ]   # 124 = timed out = the FIFO was opened and blocked
}

@test "mmnet-alloc: a FIFO AS the state dir fails fast, no blocking open" {
    # `exec 9<fifo` is a blocking read open — a planted FIFO at the state-dir
    # path would hang reserve AND release. The -d pre-check must refuse first.
    # timeout is the assertion: 124 = the open blocked.
    export MMNET_STATE_DIR="$TDIR/mmnet-fifodir"
    export MMNET_SEED_SPACE=4
    mkfifo "$MMNET_STATE_DIR"
    run timeout 15 "$VM/mmnet-alloc.sh" reserve
    [ "$status" -ne 0 ]
    [ "$status" -ne 124 ]
    run timeout 15 "$VM/mmnet-alloc.sh" release 0
    [ "$status" -ne 0 ]
    [ "$status" -ne 124 ]
}

@test "mmnet-alloc: reservations are world-readable (0644) regardless of umask" {
    # A peer's record must be READABLE for staleness checks; write/unlink stays
    # owner-only via the sticky dir. The create passes 0o644 explicitly and the
    # chmod repairs a restrictive umask — assert both, then functionally prove
    # readability under umask 077.
    run grep -n '0o644' "$VM/mmnet-alloc.sh"
    [ "$status" -eq 0 ]
    run grep -n 'chmod 0644' "$VM/mmnet-alloc.sh"
    [ "$status" -eq 0 ]
    export MMNET_STATE_DIR="$TDIR/mmnet-umask"
    s=$(umask 077; "$VM/mmnet-alloc.sh" reserve)
    [ -n "$s" ]
    . "$VM/mmnet-config.sh"
    pa=$(mmnet_local_port a "$s")
    [ "$(stat -c %a "$MMNET_STATE_DIR/port-$pa.reserved")" = "644" ]
    "$VM/mmnet-alloc.sh" release "$s"
}

@test "mmnet-alloc: reserve/release round-trips and hands out distinct seeds" {
    export MMNET_STATE_DIR="$TDIR/mmnet"
    export MMNET_SEED_SPACE=8
    # Pin a LIVE owner pid (as gate_mmnet does): under $() the default $PPID is
    # the ephemeral command-substitution subshell, whose death makes the next
    # reserve prune the still-in-use slot.
    export MMNET_OWNER_PID=$$
    s1=$("$VM/mmnet-alloc.sh" reserve)
    s2=$("$VM/mmnet-alloc.sh" reserve)
    [ -n "$s1" ] && [ -n "$s2" ]
    [ "$s1" != "$s2" ]
    # Reservation files are named by base port and live in the shared dir.
    [ -n "$(ls "$MMNET_STATE_DIR"/port-*.reserved)" ]
    "$VM/mmnet-alloc.sh" release "$s1"
    "$VM/mmnet-alloc.sh" release "$s2"
    run bash -c "ls '$MMNET_STATE_DIR'/port-*.reserved 2>/dev/null"
    [ "$status" -ne 0 ]
}

@test "mmnet-alloc: dead-pid reservations are pruned, live ones kept" {
    export MMNET_STATE_DIR="$TDIR/mmnet-dead"
    export MMNET_SEED_SPACE=8
    mkdir -p "$MMNET_STATE_DIR"
    # Poison EVERY seed with a stale reservation owned by pid one ABOVE
    # /proc/sys/kernel/pid_max — a pid the kernel can never hand out, so the
    # dead-owner check cannot flake on a recycled real pid.
    local dead_pid=$(( $(cat /proc/sys/kernel/pid_max) + 1 ))
    . "$VM/mmnet-config.sh"
    for i in $(seq 0 $((MMNET_SEED_SPACE - 1))); do
        pa=$(mmnet_local_port a "$i"); pb=$(mmnet_local_port b "$i")
        printf 'seed=%s ports=%s/%s pid=%s uid=0 ts=1\n' "$i" "$pa" "$pb" "$dead_pid" \
            >"$MMNET_STATE_DIR/port-$pa.reserved"
    done
    s=$("$VM/mmnet-alloc.sh" reserve)
    [ -n "$s" ]
    "$VM/mmnet-alloc.sh" release "$s"
    # Pruning must NOT use kill -0 in code: a foreign user's live pid returns
    # EPERM, indistinguishable from dead, so kill -0 would reap a live
    # reservation. (The word may appear in comments — match only non-comment
    # lines.)
    run grep -nE '^[[:space:]]*[^#[:space:]].*kill -0' "$VM/mmnet-alloc.sh"
    [ "$status" -ne 0 ]
    run grep -nF '/proc/' "$VM/mmnet-alloc.sh"
    [ "$status" -eq 0 ]
}

@test "mmnet-alloc: reservation records carry uid for cross-user diagnosis" {
    export MMNET_STATE_DIR="$TDIR/mmnet-uid"
    s=$("$VM/mmnet-alloc.sh" reserve)
    . "$VM/mmnet-config.sh"
    pa=$(mmnet_local_port a "$s")
    run grep -q 'uid=' "$MMNET_STATE_DIR/port-$pa.reserved"
    [ "$status" -eq 0 ]
    "$VM/mmnet-alloc.sh" release "$s"
}

# ---------------------------------------------------------------------------
# host-port.sh — qdistro_pick_free_port
# ---------------------------------------------------------------------------

@test "host-port: pick returns a numeric port inside the requested range" {
    p=$(bash -c ". '$VM/lib/host-port.sh'; qdistro_pick_free_port")
    [ -n "$p" ]
    [ "$p" -ge 30000 ] && [ "$p" -le 39999 ]
}

@test "host-port: a bound port is never returned" {
    # Occupy a port, then demand a pick from a range covering ONLY that port —
    # the helper must refuse rather than hand back the busy port.
    cat >"$TDIR/listen.py" <<'EOF'
import socket, sys, time
s = socket.socket()
s.bind(("127.0.0.1", int(sys.argv[1])))
s.listen(1)
open(sys.argv[2], "w").close()
time.sleep(8)
EOF
    run bash -c "
        . '$VM/lib/host-port.sh'
        p=\$(qdistro_pick_free_port) || exit 1
        python3 '$TDIR/listen.py' \"\$p\" '$TDIR/ready' &
        for i in \$(seq 1 50); do [ -e '$TDIR/ready' ] && break; sleep 0.1; done
        [ -e '$TDIR/ready' ] || exit 2   # readiness asserted, not assumed
        qdistro_pick_free_port \"\$p\" \"\$p\"
    "
    [ "$status" -ne 0 ]
    [[ "$output" == *"could not pick a free port"* ]]
}

@test "host-port: explicit one-port range returns that port when free" {
    p=$(bash -c ". '$VM/lib/host-port.sh'; qdistro_pick_free_port")
    got=$(bash -c ". '$VM/lib/host-port.sh'; qdistro_pick_free_port $p $p")
    [ "$got" = "$p" ]
}

# ---------------------------------------------------------------------------
# Fixed-port removal: image verify/install-test, bootstrap staging, vm-gui
# ---------------------------------------------------------------------------

@test "image/verify.sh: no fixed SSH port; QDISTRO_VERIFY_PORT overrides" {
    run grep -n 'SSH_PORT=2299\|SSH_PORT="2299"' "$IMAGE/verify.sh"
    [ "$status" -ne 0 ]
    run grep -n 'QDISTRO_VERIFY_PORT' "$IMAGE/verify.sh"
    [ "$status" -eq 0 ]
    run grep -n 'qdistro_pick_free_port' "$IMAGE/verify.sh"
    [ "$status" -eq 0 ]
}

@test "image/install-test.sh: no fixed SSH port; QDISTRO_INSTALL_PORT overrides" {
    run grep -n 'SSH_PORT=2300\|SSH_PORT="2300"' "$IMAGE/install-test.sh"
    [ "$status" -ne 0 ]
    run grep -n 'QDISTRO_INSTALL_PORT' "$IMAGE/install-test.sh"
    [ "$status" -eq 0 ]
    run grep -n 'qdistro_pick_free_port' "$IMAGE/install-test.sh"
    [ "$status" -eq 0 ]
}

@test "fresh-vm-bootstrap.sh: QDISTRO_HTTP_HOST is required, no baked 8765 default" {
    run grep -nF 'QDISTRO_HTTP_HOST:-' "$VM/fresh-vm-bootstrap.sh"
    [ "$status" -ne 0 ]
    run grep -nF 'QDISTRO_HTTP_HOST:?' "$VM/fresh-vm-bootstrap.sh"
    [ "$status" -eq 0 ]
}

@test "build-enforcing-baseweed.sh: kernel-assigned port; reclaim only on explicit --http-port" {
    run grep -n 'HTTP_PORT=8765' "$VM/build-enforcing-baseweed.sh"
    [ "$status" -ne 0 ]
    # Bind+readback: the server prints the port it ACTUALLY bound (REQ_PORT=0
    # default = kernel-assigned; explicit port proves OUR process bound it —
    # a foreign listener makes python exit and no port line ever appears).
    run grep -n 'REQ_PORT=0' "$VM/build-enforcing-baseweed.sh"
    [ "$status" -eq 0 ]
    run grep -n 'server_address\[1\]' "$VM/build-enforcing-baseweed.sh"
    [ "$status" -eq 0 ]
    # The kill-the-listener reclaim must be gated on an explicit port request.
    run grep -n 'HTTP_PORT_EXPLICIT' "$VM/build-enforcing-baseweed.sh"
    [ "$status" -eq 0 ]
    reclaim_line=$(grep -n 'PIDS=.*ss -tlnp' "$VM/build-enforcing-baseweed.sh" | cut -d: -f1)
    guard_line=$(grep -n 'HTTP_PORT_EXPLICIT" -eq 1' "$VM/build-enforcing-baseweed.sh" | head -1 | cut -d: -f1)
    [ -n "$reclaim_line" ] && [ -n "$guard_line" ]
    [ "$guard_line" -lt "$reclaim_line" ]
}

@test "vm-gui: screenshot default is uid-qualified, not a shared /tmp name" {
    run grep -n '/tmp/vm-screenshot.png' "$VM/vm-gui"
    [ "$status" -ne 0 ]
    run grep -n 'vm-screenshot-$(id -u)' "$VM/vm-gui"
    [ "$status" -eq 0 ]
}

@test "test-bare-metal-on-vm.sh: kernel-assigned staging port, not a bare RANDOM draw" {
    run grep -n 'RANDOM % 1000' "$REPO_ROOT/scripts/install/test-bare-metal-on-vm.sh"
    [ "$status" -ne 0 ]
    run grep -n '("127.0.0.1", 0)' "$REPO_ROOT/scripts/install/test-bare-metal-on-vm.sh"
    [ "$status" -eq 0 ]
}

@test "test-vm-{consumer-check,suites}.sh: no fixed SSH hostfwd ports" {
    run grep -nE '(SSH_)?PORT=.*:-222[34]' "$VM/test-vm-consumer-check.sh" "$VM/test-vm-suites.sh"
    [ "$status" -ne 0 ]
    run grep -n 'QDISTRO_CONSUMER_SSH_PORT' "$VM/test-vm-consumer-check.sh"
    [ "$status" -eq 0 ]
    run grep -n 'QDISTRO_SUITES_SSH_PORT' "$VM/test-vm-suites.sh"
    [ "$status" -eq 0 ]
    run grep -n 'qdistro_pick_free_port' "$VM/test-vm-consumer-check.sh" "$VM/test-vm-suites.sh"
    [ "$status" -eq 0 ]
}
