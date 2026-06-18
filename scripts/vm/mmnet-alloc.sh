#!/bin/bash
# mmnet-alloc.sh — atomically allocate a unique UDP port-pair seed for one
# multi-machine run, recording the reservation so a CONCURRENT sibling run on
# the same host cannot pick the same isolated segment.
#
# Why a lock: the mmnet segment is a QEMU point-to-point UDP tunnel over
# loopback (mmnet-config.sh); its namespace is the (A,B) UDP PORT PAIR. Two runs
# that picked the same port pair would cross-deliver into one segment — a
# correctness and isolation bug. So we serialise allocation under flock(1) over a
# per-UID state dir and reserve the actual PORT PAIR (not just a raw seed).
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
# State dir: ${XDG_RUNTIME_DIR:-/tmp}/qdistro-mmnet-$UID/
#   port-<basePort>.reserved   one line: "seed=<i> ports=<a>/<b> pid=<pid> ts=<epoch>"
#   .lock                      flock target
#
# Stale reservations (whose recorded pid is gone) are reclaimable: reserve()
# prunes any reservation whose pid no longer exists before scanning, so a
# crashed run cannot permanently burn a port pair.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=scripts/vm/mmnet-config.sh
. "$SCRIPT_DIR/mmnet-config.sh"

# Size of the base-port index space. mmnet_base_port maps index -> 20000+index*2,
# so this MUST match the modulus mmnet_base_port uses (5000 -> ports 20000..29998).
# Kept here so reserve()'s candidate range and the config's port derivation stay
# in lockstep.
SEED_SPACE="${MMNET_SEED_SPACE:-5000}"

STATE_DIR="${MMNET_STATE_DIR:-${XDG_RUNTIME_DIR:-/tmp}/qdistro-mmnet-$(id -u)}"
LOCK="$STATE_DIR/.lock"

mkdir -p "$STATE_DIR"
chmod 0700 "$STATE_DIR" 2>/dev/null || true

cmd="${1:-}"; shift || true

case "$cmd" in
    reserve)
        exec 9>"$LOCK"
        flock 9
        # Prune reservations whose owning pid is dead (crashed run).
        for f in "$STATE_DIR"/port-*.reserved; do
            [ -e "$f" ] || continue
            p=$(sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' "$f" 2>/dev/null)
            if [ -n "$p" ] && ! kill -0 "$p" 2>/dev/null; then
                rm -f "$f" 2>/dev/null || true
            fi
        done
        # Scan the base-port INDEX space for a free pair. Start from a random
        # offset so two runs that start in the same instant don't both begin at
        # 0 and serialise. The reservation is keyed by the base PORT, so two
        # distinct reserved indices can never share a port pair.
        start=$(( RANDOM % SEED_SPACE ))
        seed=""
        for i in $(seq 0 $(( SEED_SPACE - 1 ))); do
            cand=$(( (start + i) % SEED_SPACE ))
            pa=$(mmnet_local_port a "$cand"); pb=$(mmnet_local_port b "$cand")
            resv="$STATE_DIR/port-$pa.reserved"
            if [ ! -e "$resv" ]; then
                printf 'seed=%s ports=%s/%s pid=%s ts=%s\n' \
                    "$cand" "$pa" "$pb" "${MMNET_OWNER_PID:-$PPID}" "$(date +%s)" >"$resv"
                seed="$cand"
                break
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
        pa=$(mmnet_local_port a "$seed")
        rm -f "$STATE_DIR/port-$pa.reserved" 2>/dev/null || true
        ;;
    *)
        echo "usage: $0 reserve | release <seed>" >&2
        exit 2
        ;;
esac
