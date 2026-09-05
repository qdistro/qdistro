#!/bin/bash
# build-guard.sh — liveness, kill and cleanup helpers for the in-VM kiwi
# retry loop in image/build-in-vm.sh. Sourced, not executed. Pure shell so
# the VM-free parts can be unit-tested on the host
# (tests/integration/vm/build-guard.bats); the mount/loop cleanup needs root
# and is exercised in the builder VM.
#
# Why these exist (iso/14 Phase A, runs 11-17, 2026-09-04):
#   * kill-by-name matched the driving shell itself (run 11), kill-by-group
#     could include the guest agent's child (i.e. that shell), so killing is
#     by an explicitly collected pid set, collected BEFORE the first signal
#     (a child forked after its parent was enumerated is otherwise missed).
#   * a SIGKILLed kiwi leaves its bind mounts, its /var/tmp/kiwi_* mounts and
#     its loop device behind; rm -rf then fails (run 13) or unlinks a 20 GiB
#     raw whose inode the loop still pins, so the space is never reclaimed.
#   * "no log output == hung" is false: mksquashfs -comp xz is silent for
#     minutes (run 16). Liveness is therefore several signals OR-ed together.
#
# Scope: the loop-device + `partx` topology kiwi uses for this description.
# Device-mapper / kpartx / LUKS / nested loop layouts are not released here;
# a future image type that uses them needs this extended first. The orphan
# sweep scans every loop device, which is right for a single-purpose builder
# VM and wrong for a shared host.
#
# Every function is safe to call with a pid that has already exited, and
# safe under a caller's `set -e`: nothing here exits non-zero except the
# deliberate return codes documented per function.

# All pids in the tree rooted at $1 (root first), collected in one pass.
guard_tree_pids() {
    local p=$1 c
    echo "$p"
    for c in $(pgrep -P "$p" 2>/dev/null || true); do guard_tree_pids "$c"; done
}

# Cumulative CPU ticks (utime+stime+cutime+cstime) of the tree rooted at $1.
# The comm field in /proc/pid/stat is parenthesised and may contain spaces
# and ')' , so fields are counted after the LAST ') ': stat fields 14-17
# become awk fields 12-15. cutime/cstime count a child that was reaped
# between two samples, so its work still registers.
guard_tree_cpu() {
    local total=0 p v
    for p in $(guard_tree_pids "$1"); do
        v=$(awk '{ sub(/^.*\) /, ""); print $12 + $13 + $14 + $15 }' "/proc/$p/stat" 2>/dev/null || true)
        total=$(( total + ${v:-0} ))
    done
    echo "$total"
}

# Number of processes in the tree rooted at $1 that are in uninterruptible
# sleep (state D): a process blocked in kernel I/O (unmounting a 20 GiB
# filesystem, syncing it, a blocking mkfs) burns no ticks of its own and
# prints nothing, yet is plainly not stalled.
guard_tree_dstate() {
    local n=0 p st
    for p in $(guard_tree_pids "$1"); do
        st=$(awk '{ sub(/^.*\) /, ""); print $1 }' "/proc/$p/stat" 2>/dev/null || true)
        if [ "$st" = D ]; then n=$(( n + 1 )); fi
    done
    echo "$n"
}

# Bytes received on every non-loopback interface, summed. A package download
# that is slow but alive (libzypp's speed floor allows ~20 kB/s) moves this
# while touching neither the log nor the CPU.
guard_rx_bytes() {
    # Interface names are right-aligned, so the leading blanks vary per line.
    awk 'NR > 2 { sub(/^ +/, ""); split($0, a, ":"); if (a[1] != "lo") { split(a[2], f, " "); s += f[1] } }
         END { print s + 0 }' /proc/net/dev 2>/dev/null || echo 0
}

# Kill the tree rooted at $1 and nothing else. Collect the set first, stop
# everything so nobody can fork or reparent in the window, then kill. If the
# root leads its own process group (the loop starts it with setsid) the whole
# group is killed too, which catches a descendant that had already reparented
# to PID 1 before we looked. Waits up to ${2:-30}s for the set to vanish and
# returns 1 if any pid survives (a D-state process can outlive SIGKILL until
# its I/O completes; the caller must not reuse the tree's resources then).
guard_kill_tree() {
    local root=$1 wait_s=${2:-30} pids p pgid i alive
    pids=$(guard_tree_pids "$root")
    [ -n "$pids" ] || return 0
    pgid=$(awk '{ sub(/^.*\) /, ""); print $3 }' "/proc/$root/stat" 2>/dev/null || true)
    for p in $pids; do kill -STOP "$p" 2>/dev/null || true; done
    for p in $pids; do kill -KILL "$p" 2>/dev/null || true; done
    if [ -n "$pgid" ] && [ "$pgid" = "$root" ] && [ "$pgid" != "$$" ]; then
        kill -KILL -- "-$pgid" 2>/dev/null || true
    fi
    for i in $(seq 1 "$wait_s"); do
        alive=0
        for p in $pids; do
            if [ -d "/proc/$p" ] && [ "$(awk '{ sub(/^.*\) /, ""); print $1 }' "/proc/$p/stat" 2>/dev/null || true)" != Z ]; then
                alive=1
            fi
        done
        if [ "$alive" = 0 ]; then return 0; fi
        sleep 1
    done
    return 1
}

# Mount points at or below $1, deepest first. /proc/mounts octal-escapes
# whitespace (\040), which is decoded so such a path still unmounts.
guard_mounts_under() {
    local dir=$1
    awk -v d="$dir" '{ m = $2; gsub(/\\040/, " ", m); if (m == d || index(m, d "/") == 1) print m }' /proc/mounts 2>/dev/null \
        | awk '{ print length($0) "\t" $0 }' | sort -rn | cut -f2- || true
}

# Loop devices whose backing file lives under $1.
guard_loops_under() {
    local dir=$1
    losetup -ln -O NAME,BACK-FILE 2>/dev/null | awk -v d="$dir" '$2 == d || index($2, d "/") == 1 { print $1 }' || true
}

# Loop devices with no backing file but with partition nodes still present
# (see guard_cleanup_target). Prints them; with "fix" as $1, deletes them.
guard_sweep_orphan_partitions() {
    local b
    for b in /sys/block/loop*; do
        [ -d "$b" ] || continue
        if [ -e "$b/loop/backing_file" ]; then continue; fi
        if ls "$b"/loop*p* >/dev/null 2>&1; then
            echo "/dev/${b##*/}"
            if [ "${1:-fix}" = fix ]; then partx -d "/dev/${b##*/}" 2>/dev/null || true; fi
        fi
    done
}

# Release everything a killed kiwi attempt left attached to $1 (the
# --target-dir) and remove it, so the next attempt starts on a clean disk.
# Order matters: mounts first (deepest first; a loop device cannot detach
# while a filesystem on it is mounted), then kiwi's own scratch mounts under
# /var/tmp/kiwi_*, then loop devices backed by files under $1, then the
# directory. Every step is VERIFIED afterwards and a leftover is reported and
# returns 1; the caller must treat that as a failed attempt, not recreate the
# directory over a live mount and let kiwi fail later with a misleading
# "target dir not empty".
guard_cleanup_target() {
    local dir=$1 m l rc=0
    for m in $(guard_mounts_under "$dir"; guard_mounts_under /var/tmp | grep '^/var/tmp/kiwi_' || true); do
        umount "$m" 2>/dev/null || umount -l "$m" 2>/dev/null || true
    done
    for l in $(guard_loops_under "$dir"); do
        # kiwi adds the partitions with `partx --add`, and partitions added
        # that way are NOT dropped when the device is detached: loop0p1..p4
        # outlive `losetup -d`, and the next attempt's own `partx --add
        # /dev/loop0` then fails with "error adding partitions 1-4" (run 18,
        # attempts 2 and 3). Delete them first, then detach.
        partx -d "$l" 2>/dev/null || true
        losetup -d "$l" 2>/dev/null || true
    done
    # Sweep: any loop device that is detached (no backing file) but still
    # carries partition nodes is the same trap waiting for the next attempt.
    guard_sweep_orphan_partitions
    sync || true
    rm -rf "$dir" 2>/dev/null || true
    if [ -n "$(guard_mounts_under "$dir")" ]; then
        echo "[guard] mounts still present under $dir:" >&2
        guard_mounts_under "$dir" >&2
        rc=1
    fi
    if [ -n "$(guard_loops_under "$dir")" ]; then
        echo "[guard] loop devices still backed by $dir:" >&2
        guard_loops_under "$dir" >&2
        rc=1
    fi
    if [ -n "$(guard_mounts_under /var/tmp | grep '^/var/tmp/kiwi_')" ]; then
        echo "[guard] kiwi scratch mounts still present under /var/tmp:" >&2
        guard_mounts_under /var/tmp | grep '^/var/tmp/kiwi_' >&2
        rc=1
    fi
    if [ -n "$(guard_sweep_orphan_partitions list)" ]; then
        echo "[guard] detached loop devices still carrying partitions:" >&2
        guard_sweep_orphan_partitions list >&2
        rc=1
    fi
    if [ -e "$dir" ]; then
        echo "[guard] $dir could not be removed" >&2
        rc=1
    fi
    return $rc
}
