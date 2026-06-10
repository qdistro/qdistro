#!/bin/bash
# templates-state-snapshot-probe.sh — exercise the fableplan2 task 05 state
# snapshot / rollback primitives against a REAL btrfs filesystem (doc/05;
# doc/filesystem.md §"Rollback semantics"). These are the claims that could
# not run on the headless host (no btrfs): the mechanism selection, the
# read-only btrfs snapshot, the RENAME_EXCHANGE swap of subvolumes, and the
# crash-recovery journal on btrfs. The host unit suites (test_snap_swap.py,
# test_state_snapshot.py) already cover the LOGIC on tmpfs; this probe proves
# the btrfs-specific code (`btrfs subvolume snapshot [-r]`, renameat2 across
# subvolumes, subvolume-aware cleanup) actually does what it claims on btrfs.
#
# Usage: templates-state-snapshot-probe.sh <scenario>
#   setup                  build a btrfs loopback + mount it (idempotent)
#   mechanism              create_state_tree on btrfs -> mechanism=subvolume
#   pre-activation-snapshot  take_pre_activation_snapshot -> RO btrfs snapshot
#   exchange-roundtrip     snapshot genA -> mutate -> restore -> genA is back
#                          via RENAME_EXCHANGE, genB kept aside as rejected
#   crash-recovery         recover() on a btrfs journal (materialized-abort +
#                          moved-complete) — state_path never left missing
#   teardown               unmount + remove the loopback
#   all                    setup, every scenario in order, teardown
#
# Each scenario prints `PASS: <name>` / `FAIL: <name> <reason>` and exits
# nonzero on the first failure. Runs as root (btrfs loop mount + subvolume
# ops are privileged; production takes these snapshots as root/broker too).
set -uo pipefail

BTRFS_IMG="${FP05_BTRFS_IMG:-/var/tmp/fp05-state.img}"
BTRFS_MNT="${FP05_BTRFS_MNT:-/mnt/fp05-btrfs}"

# Resolve the python modules: installed libexec (VM) or the in-tree sources
# (host dev). qdistro_snap_swap lives under snapshots/, the rest under
# templates/; the installer co-locates both in libexec so the lazy sibling
# import in qdistro_state_snapshot resolves either way.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# probes/ -> vm/ -> integration/ -> tests/ -> repo root (qdistro).
REPO_ROOT="$(cd "$SCRIPT_DIR/../../../.." 2>/dev/null && pwd || true)"
PYPATH=""
if [ -f /usr/libexec/qdistro/qdistro_state_snapshot.py ]; then
    PYPATH="/usr/libexec/qdistro"
elif [ -n "$REPO_ROOT" ] && [ -f "$REPO_ROOT/templates/qdistro_state_snapshot.py" ]; then
    PYPATH="$REPO_ROOT/templates:$REPO_ROOT/snapshots"
else
    echo "FAIL: setup cannot locate qdistro_state_snapshot.py (libexec or in-tree)" >&2
    exit 2
fi

# qdistro-snap-swap CLI: installed wrapper, else a thin python shim on PYPATH.
snap_swap() {
    if command -v qdistro-snap-swap >/dev/null 2>&1; then
        qdistro-snap-swap "$@"
    else
        PYTHONPATH="$PYPATH" python3 -m qdistro_snap_swap "$@"
    fi
}

pyrun() { PYTHONPATH="$PYPATH" python3 "$@"; }

fail() { echo "FAIL: $1 ${2:-}" >&2; exit 1; }

require_btrfs_mount() {
    mountpoint -q "$BTRFS_MNT" || fail "${1:-scenario}" \
        "btrfs mount $BTRFS_MNT absent — run the setup scenario first"
    local fst
    fst=$(stat -f -c %T "$BTRFS_MNT" 2>/dev/null || echo "")
    [ "$fst" = "btrfs" ] || fail "${1:-scenario}" \
        "$BTRFS_MNT is $fst, not btrfs"
}

# Two synthetic generation digests (snapshot pins require sha256: digests).
GEN_A="sha256:$(printf 'a%.0s' {1..64})"
GEN_B="sha256:$(printf 'b%.0s' {1..64})"

# --------------------------------------------------------------------------

scenario_setup() {
    [ "$(id -u)" = 0 ] || fail setup "must run as root (btrfs loop mount)"
    command -v mkfs.btrfs >/dev/null 2>&1 || fail setup "mkfs.btrfs not installed"
    mkdir -p "$BTRFS_MNT"
    if mountpoint -q "$BTRFS_MNT"; then
        # Idempotent: a prior setup already mounted it.
        [ "$(stat -f -c %T "$BTRFS_MNT")" = btrfs ] \
            || fail setup "$BTRFS_MNT already mounted but not btrfs"
        echo "PASS: setup (already mounted)"
        return 0
    fi
    rm -f "$BTRFS_IMG"
    truncate -s 1G "$BTRFS_IMG" || fail setup "truncate $BTRFS_IMG failed"
    mkfs.btrfs -q -f "$BTRFS_IMG" || fail setup "mkfs.btrfs failed"
    mount -o loop "$BTRFS_IMG" "$BTRFS_MNT" || fail setup "mount -o loop failed"
    [ "$(stat -f -c %T "$BTRFS_MNT")" = btrfs ] \
        || fail setup "mounted fs is not btrfs"
    echo "PASS: setup"
}

scenario_teardown() {
    if mountpoint -q "$BTRFS_MNT"; then
        umount "$BTRFS_MNT" || umount -l "$BTRFS_MNT" || true
    fi
    rm -f "$BTRFS_IMG"
    echo "PASS: teardown"
}

scenario_mechanism() {
    require_btrfs_mount mechanism
    local silo=fp05mech
    local var="$BTRFS_MNT/$silo-var"
    rm -rf "$var"
    pyrun - "$var" "$silo" <<'PY' || fail mechanism "python harness failed"
import os, sys
import qdistro_templates as qt
var, silo = sys.argv[1], sys.argv[2]
layout = qt.Layout(etc=var + "/etc", var=var)
state = layout.default_state_path(silo)
mech = qt.create_state_tree(state)
assert mech == "subvolume", f"mechanism={mech!r}, expected subvolume on btrfs"
meta = qt.read_state_meta(state)
assert meta and meta.get("mechanism") == "subvolume", f"state-meta={meta!r}"
print("OK", state)
PY
    # Independently confirm it is a real btrfs subvolume (not a plain dir).
    btrfs subvolume show "$var/silos/$silo/state" >/dev/null 2>&1 \
        || fail mechanism "state path is not a btrfs subvolume"
    echo "PASS: mechanism"
}

scenario_pre_activation_snapshot() {
    require_btrfs_mount pre-activation-snapshot
    local silo=fp05snap
    local var="$BTRFS_MNT/$silo-var"
    rm -rf "$var"
    pyrun - "$var" "$silo" "$GEN_A" "$GEN_B" <<'PY' \
        || fail pre-activation-snapshot "python harness failed"
import os, sys
import qdistro_templates as qt
import qdistro_state_snapshot as ss
var, silo, genA, genB = sys.argv[1:5]
layout = qt.Layout(etc=var + "/etc", var=var)
state = layout.default_state_path(silo)
mech = qt.create_state_tree(state)
assert mech == "subvolume", mech
with open(os.path.join(state, "marker"), "w") as fh:
    fh.write("genA-content")
res = ss.take_pre_activation_snapshot(
    layout, silo, incoming_generation=genB, outgoing_generation=genA,
    template="tier2-dev", state_path=state, policy="availability")
assert res.get("taken") is True, f"snapshot not taken: {res!r}"
assert res.get("mechanism") == "subvolume", res
payload = res["path"]
assert os.path.isdir(payload), f"payload missing: {payload}"
# The snapshot must capture the genA content.
with open(os.path.join(payload, "marker")) as fh:
    assert fh.read() == "genA-content", "snapshot content mismatch"
# The pre-migration pin on the outgoing generation must exist.
pin = os.path.join(layout.pins_for("tier2-dev", genA), "pre-migration-snapshot.toml")
assert os.path.isfile(pin), f"snapshot pin missing: {pin}"
print("OK", payload)
PY
    local payload="$var/silos/$silo/state-snapshots"
    payload=$(find "$payload" -maxdepth 2 -name snapshot -type d | head -1)
    [ -n "$payload" ] || fail pre-activation-snapshot "no snapshot payload dir"
    # The payload must be a READ-ONLY btrfs subvolume.
    btrfs subvolume show "$payload" 2>/dev/null | grep -qiE 'readonly|ro flag.*true|Flags.*readonly' \
        || btrfs property get -ts "$payload" ro 2>/dev/null | grep -q 'ro=true' \
        || fail pre-activation-snapshot "snapshot payload is not a RO subvolume"
    # And a write into it must be refused.
    if touch "$payload/should-fail" 2>/dev/null; then
        rm -f "$payload/should-fail"
        fail pre-activation-snapshot "RO snapshot payload accepted a write"
    fi
    echo "PASS: pre-activation-snapshot"
}

scenario_exchange_roundtrip() {
    require_btrfs_mount exchange-roundtrip
    local silo=fp05rt
    local var="$BTRFS_MNT/$silo-var"
    rm -rf "$var"
    pyrun - "$var" "$silo" "$GEN_A" "$GEN_B" <<'PY' \
        || fail exchange-roundtrip "python harness failed"
import os, sys
import qdistro_templates as qt
import qdistro_state_snapshot as ss
var, silo, genA, genB = sys.argv[1:5]
layout = qt.Layout(etc=var + "/etc", var=var)
state = layout.default_state_path(silo)
assert qt.create_state_tree(state) == "subvolume"

def write(name, data):
    with open(os.path.join(state, name), "w") as fh:
        fh.write(data)

# Generation A's state, snapshotted as the pre-activation snapshot.
write("marker", "genA")
res = ss.take_pre_activation_snapshot(
    layout, silo, incoming_generation=genB, outgoing_generation=genA,
    template="tier2-dev", state_path=state, policy="availability")
assert res.get("taken") is True, res

# Generation B then writes: change the marker, add a B-only file.
write("marker", "genB")
write("b-only.txt", "from-genB")

# Roll back to A with --restore-state: find A's snapshot, swap it in.
meta = ss.find_restore_snapshot(layout, silo, genA)
assert meta is not None, "no restore-eligible snapshot for genA"
swap = ss.restore_snapshot(layout, silo, meta, state)
assert swap["method"] == "exchange", f"expected RENAME_EXCHANGE, got {swap!r}"

# state_path now holds genA again, at the SAME path (never moved/missing).
with open(os.path.join(state, "marker")) as fh:
    assert fh.read() == "genA", "restore did not bring back genA marker"
assert not os.path.exists(os.path.join(state, "b-only.txt")), \
    "genB file leaked into the restored state"

# The displaced genB state is kept aside, not deleted.
rej = swap["rejected"]
assert os.path.isdir(rej), f"rejected state not kept: {rej}"
with open(os.path.join(rej, "marker")) as fh:
    assert fh.read() == "genB", "rejected state is not the genB state"
assert os.path.exists(os.path.join(rej, "b-only.txt")), "genB file lost from rejected"
print("OK", swap["method"], rej)
PY
    # Confirm the live state is a btrfs subvolume after the exchange (the
    # writable clone the swap promoted, not a degraded plain dir).
    btrfs subvolume show "$var/silos/$silo/state" >/dev/null 2>&1 \
        || fail exchange-roundtrip "restored state is not a btrfs subvolume"
    echo "PASS: exchange-roundtrip"
}

scenario_crash_recovery() {
    require_btrfs_mount crash-recovery
    local root="$BTRFS_MNT/fp05crash"
    rm -rf "$root"; mkdir -p "$root"

    # --- materialized-abort: clone built but never swapped in. recover() must
    #     drop the orphan clone and leave the OLD state intact at state_path.
    local s1="$root/state1"
    btrfs subvolume create "$s1" >/dev/null || fail crash-recovery "subvol create s1"
    echo old > "$s1/marker"
    local tmp1="$root/.state1.snap-swap-stale"
    btrfs subvolume snapshot "$s1" "$tmp1" >/dev/null \
        || fail crash-recovery "clone snapshot tmp1"
    echo clone > "$tmp1/marker"
    cat > "$s1.swap-pending.toml" <<EOF
phase = 'materialized'
temp = '$tmp1'
rejected = '$s1-rejected-1'
snapshot = '$root/snap'
EOF
    local out1
    out1=$(snap_swap recover "$s1" 2>&1) || fail crash-recovery "recover s1 exited nonzero: $out1"
    [ -d "$s1" ] || fail crash-recovery "materialized-abort left state1 missing"
    [ "$(cat "$s1/marker")" = old ] || fail crash-recovery "materialized-abort lost old state"
    [ ! -e "$tmp1" ] || fail crash-recovery "materialized-abort did not drop the orphan clone"
    [ ! -e "$s1.swap-pending.toml" ] || fail crash-recovery "journal not cleared after abort"

    # --- moved-complete: two-rename crash AFTER state->rejected but BEFORE
    #     temp->state (state_path absent, clone waiting in temp). recover()
    #     must finish the swap so state_path holds the new clone.
    local s2="$root/state2"
    btrfs subvolume create "$s2" >/dev/null || fail crash-recovery "subvol create s2"
    echo newclone > "$s2/marker"
    local tmp2="$root/.state2.snap-swap-pending"
    # Build the clone that should become the live state, then simulate the
    # crash by moving the real state aside and leaving state_path absent.
    btrfs subvolume snapshot "$s2" "$tmp2" >/dev/null \
        || fail crash-recovery "clone snapshot tmp2"
    local rej2="$s2-rejected-2"
    mv "$s2" "$rej2"                     # state -> rejected already happened
    cat > "$s2.swap-pending.toml" <<EOF
phase = 'moved'
temp = '$tmp2'
rejected = '$rej2'
snapshot = '$root/snap'
EOF
    [ ! -e "$s2" ] || fail crash-recovery "precondition: state2 should be absent"
    local out2
    out2=$(snap_swap recover "$s2" 2>&1) || fail crash-recovery "recover s2 exited nonzero: $out2"
    [ -d "$s2" ] || fail crash-recovery "moved-complete left state2 missing"
    [ "$(cat "$s2/marker")" = newclone ] || fail crash-recovery "moved-complete did not install the clone"
    [ ! -e "$s2.swap-pending.toml" ] || fail crash-recovery "journal not cleared after completion"

    echo "PASS: crash-recovery"
}

main() {
    local scenario="${1:-all}"
    case "$scenario" in
        setup)                    scenario_setup ;;
        mechanism)                scenario_mechanism ;;
        pre-activation-snapshot)  scenario_pre_activation_snapshot ;;
        exchange-roundtrip)       scenario_exchange_roundtrip ;;
        crash-recovery)           scenario_crash_recovery ;;
        teardown)                 scenario_teardown ;;
        all)
            scenario_setup
            scenario_mechanism
            scenario_pre_activation_snapshot
            scenario_exchange_roundtrip
            scenario_crash_recovery
            scenario_teardown
            echo "PASS: all"
            ;;
        *) echo "unknown scenario: $scenario" >&2; exit 2 ;;
    esac
}

main "$@"
