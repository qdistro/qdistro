#!/bin/bash
# mmnet-alloc.sh — atomically allocate a unique UDP port-pair seed for one
# multi-machine run, recording the reservation so a CONCURRENT sibling run on
# the same host cannot pick the same isolated segment.
#
# Why a lock: the mmnet segment is a QEMU point-to-point UDP tunnel over
# loopback (mmnet-config.sh); its namespace is the (A,B) UDP PORT PAIR. Two runs
# that picked the same port pair would cross-deliver into one segment — a
# correctness and isolation bug. So we serialise allocation under flock(2) taken
# ON THE SHARED STATE DIR's inode — UDP ports are global on the box, not per-UID,
# so the reservation namespace must span every test user (an earlier per-UID dir
# let two users reserve the same pair and bridge their "isolated" segments) — and
# reserve the actual PORT PAIR (not just a raw seed).
#
# Why lock the dir and not a lock FILE: a shared dir's lock file would be
# unlinkable/replaceable by any user, splitting the flock onto two inodes, and a
# planted symlink at that path turns our open+chmod into an attack on a victim's
# file. A directory can't be swapped under sticky /tmp (only its owner may
# unlink it), everyone can open it O_RDONLY, and flock(2) accepts dir fds.
# We still verify the opened fd resolves to this exact path.
#
# The allocatable seed space is the base-port index 0..MMNET_SEED_SPACE-1, which
# maps BIJECTIVELY onto the base ports mmnet_base_port produces (base = 20000 +
# index*2). Reserving a free index therefore guarantees a free, distinct port
# pair — there is no seed->port aliasing (an earlier bug: a 0..65535 seed taken
# mod 5000 let two distinct seeds share one port pair). The reservation file is
# NAMED by the base port so the port-pair invariant is impossible to miss.
#
# Usage:
#   seed=$(mmnet-alloc.sh reserve)   # prints the allocated seed (== base-port
#                                    #   index); creates a reservation file
#   mmnet-alloc.sh release <seed>    # removes the reservation (run cleanup)
#
# State dir: ${MMNET_STATE_DIR:-/tmp/qdistro-mmnet} (mode 1777 sticky, shared by
# all test users on the host). Sticky is deliberate: non-owners cannot unlink
# or rename another user's reservation, so a live segment's slot can never be
# freed out from under it, and fs.protected_symlinks (kernel default) refuses
# foreign symlinks here. Reservations are written 0644 so any user can READ a
# peer's pid for staleness — but only the owner may remove it. A crashed OTHER
# user's slots stay burned until reboot; that is safe (never double-assigned)
# and the 5000-pair space makes it cheap.
#   port-<basePort>.reserved   one line: "seed=<i> ports=<a>/<b> pid=<pid> uid=<u> ts=<epoch>"
#
# Stale reservations (whose recorded pid is gone) are reclaimable BY THEIR
# OWNER: reserve() prunes same-user reservations whose pid no longer exists
# before scanning, so a crashed run cannot permanently burn a port pair.
# Pid liveness is probed via /proc/<pid> rather than kill -0: a foreign user's
# live pid returns EPERM from kill -0, which is indistinguishable from dead and
# would reap a live slot. An unreadable or unparseable record is treated as
# LIVE (skipped, never removed) — conservative, never a double assignment.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=scripts/vm/mmnet-config.sh
. "$SCRIPT_DIR/mmnet-config.sh"

# Size of the base-port index space. mmnet_base_port maps index -> 20000+index*2,
# so this MUST match the modulus mmnet_base_port uses (5000 -> ports 20000..29998).
# Kept here so reserve()'s candidate range and the config's port derivation stay
# in lockstep.
SEED_SPACE="${MMNET_SEED_SPACE:-5000}"

STATE_DIR="${MMNET_STATE_DIR:-/tmp/qdistro-mmnet}"

mkdir -p "$STATE_DIR" 2>/dev/null || true
# A symlinked state dir is refused outright — accepting one would let a
# planted link aim the mode fix and the lock at a caller-chosen directory.
# (fs.protected_symlinks already refuses FOREIGN symlinks in 1777 /tmp;
# refusing own-uid links too keeps the contract uniform.)
if [ -L "$STATE_DIR" ]; then
    echo "mmnet-alloc: $STATE_DIR is a symlink (refusing)" >&2
    exit 1
fi
# Reject non-directories BEFORE opening: `exec 9<fifo` is a blocking read
# open that hangs forever on a planted FIFO. The check-then-open race needs
# the attacker to own the path entry (only the creator can swap it in sticky
# /tmp) — same accepted residual as the dir-owner rename below.
if [ ! -d "$STATE_DIR" ]; then
    echo "mmnet-alloc: $STATE_DIR is not a directory (refusing)" >&2
    exit 1
fi
# Open the directory and operate on the inode through /proc/self/fd:
# chmod-by-path here could be redirected by a swapped-in symlink before the
# check above would ever run again. A dir fd opens O_RDONLY for every user
# of a 1777 dir; there is no lock FILE to unlink or symlink.
if ! { exec 9<"$STATE_DIR"; } 2>/dev/null; then
    echo "mmnet-alloc: cannot open $STATE_DIR" >&2
    exit 1
fi
if [ "$(stat -Lc '%F' "/proc/$$/fd/9" 2>/dev/null)" != "directory" ]; then
    echo "mmnet-alloc: $STATE_DIR is not a directory" >&2
    exit 1
fi
# Sticky + world-writable: sticky stops non-owners unlinking/renaming peers'
# files; world-writable lets every test user create reservation files. The
# mode is fixed through the fd (chmod fails silently for a foreign-owned dir)
# and then VALIDATED — a foreign 1777 dir passes as shared state, a foreign
# non-1777 dir fails loudly rather than running under weakened invariants.
if [ "$(stat -Lc '%a' "/proc/$$/fd/9" 2>/dev/null)" != "1777" ]; then
    chmod 1777 "/proc/$$/fd/9" 2>/dev/null || true
fi
if [ "$(stat -Lc '%a' "/proc/$$/fd/9" 2>/dev/null)" != "1777" ]; then
    echo "mmnet-alloc: $STATE_DIR must be a mode-1777 directory (permitting shared reservations)" >&2
    exit 1
fi

cmd="${1:-}"; shift || true

case "$cmd" in
    reserve)
        # Serialise on the directory inode (fd 9 opened above). Compare
        # dev:inode, not path strings: the check must prove the fd we lock IS
        # the directory at $STATE_DIR right now. Residual the check cannot
        # close: the dir's OWNER can rename it and plant a fresh dir,
        # stranding live reservations — accepted, because reservations are
        # advisory anyway (that same user could bind the UDP ports directly).
        fd_id=$(stat -Lc '%d:%i' "/proc/$$/fd/9" 2>/dev/null)
        dir_id=$(stat -c '%d:%i' "$STATE_DIR" 2>/dev/null)
        if [ -z "$fd_id" ] || [ "$fd_id" != "$dir_id" ]; then
            echo "mmnet-alloc: $STATE_DIR was substituted before lock (fd=$fd_id path=$dir_id)" >&2
            exit 1
        fi
        flock 9
        # Prune OUR stale reservations (owner pid dead = crashed run). -O is
        # the cross-user guard: a reservation owned by another uid is never
        # removed here, whatever its recorded pid — a live peer's slot can
        # only be released by that peer. (Under hidepid=2 a foreign live pid
        # looks dead; the -O test still keeps it.)
        for f in "$STATE_DIR"/port-*.reserved; do
            [ -e "$f" ] || continue
            [ -O "$f" ] || continue
            # Regular files only: sed on a FIFO would block forever while
            # this process holds the flock; non-regular entries are left
            # alone (a burned slot is conservative-safe).
            [ -f "$f" ] || continue
            p=$(sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' "$f" 2>/dev/null) || p=""
            if [ -n "$p" ] && [ ! -d "/proc/$p" ]; then
                rm -f "$f" 2>/dev/null || true
            fi
        done
        # Scan the base-port INDEX space for a free pair. Start from a random
        # offset so two runs that start in the same instant don't both begin at
        # 0 and serialise. The reservation is keyed by the base PORT, so two
        # distinct reserved indices can never share a port pair. An existing
        # file blocks the slot even when its record is stale-but-foreign or
        # unreadable — never a double assignment.
        start=$(( RANDOM % SEED_SPACE ))
        seed=""
        for i in $(seq 0 $(( SEED_SPACE - 1 ))); do
            cand=$(( (start + i) % SEED_SPACE ))
            pa=$(mmnet_local_port a "$cand"); pb=$(mmnet_local_port b "$cand")
            resv="$STATE_DIR/port-$pa.reserved"
            # [ ! -e ] follows symlinks; a dangling foreign symlink at this path
            # would pass it and then have `>` create/write the victim's target.
            if [ ! -e "$resv" ] && [ ! -L "$resv" ]; then
                # Atomic create via real O_EXCL: bash noclobber only refuses
                # existing REGULAR files — `>` on a raced-in FIFO would open
                # it and block forever while this process holds the flock.
                # os.open O_WRONLY|O_CREAT|O_EXCL fails EEXIST on ANY inode
                # type. 0644 at create so peers can READ the record (the
                # chmod below fixes a restrictive umask); a file that fails
                # to be claimed simply burns the slot conservatively.
                if python3 -c '
import os, sys, time
path, cand, pa, pb, pid = sys.argv[1:6]
try:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
except OSError:
    sys.exit(1)
with os.fdopen(fd, "w") as f:
    f.write("seed=%s ports=%s/%s pid=%s uid=%s ts=%s\n"
            % (cand, pa, pb, pid, os.getuid(), int(time.time())))
' "$resv" "$cand" "$pa" "$pb" "${MMNET_OWNER_PID:-$PPID}" 2>/dev/null \
                    && [ -f "$resv" ] && [ ! -L "$resv" ] && [ -s "$resv" ]; then
                    chmod 0644 "$resv" 2>/dev/null || true
                    seed="$cand"
                    break
                fi
            fi
        done
        flock -u 9
        if [ -z "$seed" ]; then
            echo "mmnet-alloc: no free UDP port pair (all $SEED_SPACE reserved?)" >&2
            exit 1
        fi
        printf '%s\n' "$seed"
        ;;
    release)
        seed="${1:-}"
        [ -n "$seed" ] || { echo "mmnet-alloc release: need a seed" >&2; exit 2; }
        # Release by the base port this seed maps to (the reservation key).
        # Same-user only: under the sticky dir, rm on a foreign file fails
        # anyway; refusing explicitly keeps the contract legible in the
        # error rather than silent.
        pa=$(mmnet_local_port a "$seed")
        resv="$STATE_DIR/port-$pa.reserved"
        if [ -e "$resv" ] && [ ! -O "$resv" ]; then
            echo "mmnet-alloc: refusing to release foreign-owned reservation $resv" >&2
            exit 1
        fi
        rm -f "$resv" 2>/dev/null || true
        ;;
    *)
        echo "usage: $0 reserve | release <seed>" >&2
        exit 2
        ;;
esac
