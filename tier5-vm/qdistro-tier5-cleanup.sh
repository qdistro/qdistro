#!/bin/bash
# Tier-5 cleanup: destroy + undefine a libvirt domain spawned by
# qdistro-tier5-spawn --vm, and remove its linked-clone disk.
# Idempotent.
#
# Usage:
#   qdistro-tier5-cleanup.sh <vm_name>
set -uo pipefail

VM_NAME="${1:?usage: $0 <vm_name>}"
ADMIN_USER="${TIER5_ADMIN_USER:-admin}"
ADMIN_HOME_DEFAULT="$(getent passwd "$ADMIN_USER" 2>/dev/null | cut -d: -f6)"
DISK_DIR="${TIER5_DISK_DIR:-$ADMIN_HOME_DEFAULT/.local/share/libvirt/images}"
DISK="$DISK_DIR/$VM_NAME.qcow2"
DISK_BASE="${TIER5_BASE_DISK:-/var/lib/libvirt/images/qdistro-tier5-base.qcow2}"

if [ "$(id -u)" -ne 0 ]; then
    echo "[tier5-cleanup] FAIL: must run as root" >&2
    exit 2
fi

ADMIN_UID=$(id -u "$ADMIN_USER")
ADMIN_RUNTIME="/run/user/$ADMIN_UID"
ADMIN_HOME="$(getent passwd "$ADMIN_USER" | cut -d: -f6)"
LIBVIRT_URI="qemu:///session"

run_as_admin() {
    runuser -u "$ADMIN_USER" -- env \
        XDG_RUNTIME_DIR="$ADMIN_RUNTIME" \
        HOME="$ADMIN_HOME" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=$ADMIN_RUNTIME/bus" \
        LIBVIRT_DEFAULT_URI="$LIBVIRT_URI" \
        "$@"
}

# qemu_pids_for_domain — same contract as spawn-tier5.sh: 0 pids live,
# 1 confirmed dead, 2 scan unreliable. domstate can report "shut off"
# for a LIVE qemu under nested virt, so unlinking the overlay is gated
# on a proven-dead scan, never on the destroy/undefine attempts above.
qemu_pids_for_domain() {
    local name="${1:-$VM_NAME}" p c out="" raw unread=""
    run_as_admin true >/dev/null 2>&1 || return 2
    # Literal substring match: pgrep -f treats the name as ERE, so a
    # manual name like `t5.a` would also match `t5xa` — over-matching
    # hands the caller FOREIGN qemu pids it may signal (sol r15). awk
    # index() via ENVIRON needs no escaping at all.
    raw=$(run_as_admin ps -eo pid=,args= 2>/dev/null) || return 2
    raw=$(printf '%s\n' "$raw" \
        | T5N="-name guest=$name," \
            awk 'index($0, ENVIRON["T5N"]) {print $1}')
    [ -n "$raw" ] || return 1
    for p in $raw; do
        # An unreadable /proc/<pid>/comm is ambiguous, never "dead".
        c=$(cat "/proc/$p/comm" 2>/dev/null) || { unread=1; continue; }
        case "$c" in qemu-system-*|qemu-kvm) out="$out $p" ;; esac
    done
    [ -n "$out" ] || { [ -n "$unread" ] && return 2; return 1; }
    printf '%s' "$out"
}

# tier5_derived_name <appId> — port of VMApps._vmNameForApp so a
# recorded binding/sidecar can be checked against the artifact's own
# name: a nonempty tag or a t5-shaped name alone does not prove THIS
# artifact is the requested identity (sol r17). appIds are ASCII by
# the --policy-key charset, so JS charCodeAt == ord() here.
tier5_derived_name() {
    python3 - "$1" <<'PY'
import re, sys
s = sys.argv[1]
fnv, djb = 0x811c9dc5, 0x00001505
for c in (ord(ch) for ch in s):
    fnv = ((fnv ^ c) * 0x01000193) & 0xFFFFFFFF
    djb = ((djb * 33) ^ c) & 0xFFFFFFFF
stub = re.sub(r"[^a-zA-Z0-9_-]", "-", s)
stub = re.sub(r"-{2,}", "-", stub).strip("-")[:43]
print("t5-" + (stub or "vmapp") + f"-{fnv:08x}{djb:08x}")
PY
}

# domain_defined_state — rc 0 defined, 1 CONFIRMED absent, 2 cannot
# determine. Uses `virsh list --all --name`: an enumeration that
# succeeds is authoritative; ANY failure is unverifiable. Dominfo
# absence needs error-text parsing whose shape drifts across libvirt
# versions and can match outage text (sol r11).
domain_defined_state() {
    local list
    if ! list=$(run_as_admin virsh list --all --name 2>/dev/null); then
        return 2
    fi
    printf '%s\n' "$list" | grep -Fxq -- "$VM_NAME" && return 0
    return 1
}

# Serialize on the domain's own hold file — the same flock protocol the
# spawn wrappers use. A live wrapper holds a shared fd9 lock for its
# whole session, so our bounded EXCLUSIVE claim either wins (no wrapper
# owns this domain — we may tear down) or loses (a live session holds
# it — refuse rather than destroy a guest mid-session). It also closes
# the check→unlink window below: while we hold EX no spawn can pass its
# hold and start/define the domain under our feet (sol r8).
HOLD_DIR="${TIER5_HOLD_DIR:-/run/qdistro-tier5}"
HOLD_FILE="$HOLD_DIR/hold-$VM_NAME"
install -d -m 0700 "$HOLD_DIR" 2>/dev/null || true
[ -f "$HOLD_FILE" ] || install -m 0600 /dev/null "$HOLD_FILE" 2>/dev/null
if ! exec 9>>"$HOLD_FILE"; then
    echo "[tier5-cleanup] FAIL: cannot open $HOLD_FILE" >&2
    exit 2
fi
if ! flock -w 5 -x 9 2>/dev/null; then
    echo "[tier5-cleanup] FAIL: a live wrapper session holds $VM_NAME" >&2
    echo "        — stop the wrapper first, or let its own teardown run" >&2
    exit 4
fi

# Teardown itself is best-effort against our own domain; only the FINAL
# gate below (confirmed-absent definition + confirmed-dead qemu) may
# unlink the overlay. Defined-state is read via the enumeration oracle —
# a query failure is ambiguity, never absence (sol r7/r11).
domain_defined_state; _ds=$?
if [ "$_ds" -eq 0 ]; then
    # The hold proves no wrapper uses the name — it does not prove the
    # domain is tier-5's. Require the appId binding the spawn stamps at
    # define time before destroying: an unbound (or unreadable) domain
    # under this name is not provably ours (sol r9).
    _bound=$(run_as_admin virsh dumpxml "$VM_NAME" 2>/dev/null \
        | tr -d '\n' \
        | sed -n 's|.*<qdistro:appId[^>]*>\([^<]*\)</qdistro:appId>.*|\1|p')
    if [ -z "$_bound" ]; then
        echo "[tier5-cleanup] FAIL: $VM_NAME carries no tier-5 appId" >&2
        echo "        binding — not provably ours; refusing to destroy" >&2
        exit 3
    fi
    # For a t5-namespace name the binding must ALSO re-derive to it —
    # any nonempty tag is not proof THIS domain is the requested
    # identity; a foreign-bound guest under a shaped name must be
    # refused, not destroyed (sol r17).
    if [[ "$VM_NAME" =~ ^t5- ]] \
            && [ "$(tier5_derived_name "$_bound")" != "$VM_NAME" ]; then
        echo "[tier5-cleanup] FAIL: $VM_NAME is bound to '$_bound'," >&2
        echo "        which derives a different guest — refusing to destroy" >&2
        exit 3
    fi
    run_as_admin virsh destroy "$VM_NAME" >/dev/null 2>&1 || true
    # destroy can no-op under a lying domstate — signal leftover qemu
    # directly (mirrors reap_domain's fallback), then demand a
    # confirmed-dead scan before touching the overlay.
    _qp=$(qemu_pids_for_domain) || true
    if [ -n "$_qp" ]; then
        echo "[tier5-cleanup] destroy left qemu alive for $VM_NAME (pids:$_qp); killing" >&2
        run_as_admin kill $_qp 2>/dev/null || true
        for _ in 1 2 3 4 5 6; do
            sleep 0.5
            _qp=$(qemu_pids_for_domain) || true
            [ -n "$_qp" ] || break
        done
        [ -n "$_qp" ] && run_as_admin kill -9 $_qp 2>/dev/null || true
    fi
    if ! run_as_admin virsh undefine "$VM_NAME" >/dev/null 2>&1; then
        echo "[tier5-cleanup] WARN: undefine $VM_NAME failed" >&2
    fi
    echo "[tier5-cleanup] $VM_NAME destroyed + undefined"
elif [ "$_ds" -eq 1 ]; then
    echo "[tier5-cleanup] $VM_NAME not present"
else
    echo "[tier5-cleanup] WARN: cannot query $VM_NAME — libvirt error" >&2
fi

if [ -f "$DISK" ]; then
    # Both proofs are required before the overlay goes: NO startable
    # definition remains (a still-defined domain can be started against
    # a disk we are about to delete) AND no backing qemu lives.
    domain_defined_state; _ds=$?
    if [ "$_ds" -eq 0 ]; then
        echo "[tier5-cleanup] FAIL: $VM_NAME still defined — keeping $DISK" >&2
        exit 5
    elif [ "$_ds" -ne 1 ]; then
        echo "[tier5-cleanup] FAIL: cannot verify $VM_NAME is gone" >&2
        echo "        — keeping $DISK" >&2
        exit 5
    fi
    # Orphan disk: no definition carries the appId binding, so
    # provenance is the file itself — it must clone OUR base to be a
    # tier-5 artifact at all (sol r13), and its .appid sidecar is the
    # owner record. A MISSING sidecar means unproven ownership: refuse
    # unless the operator explicitly opts in (a deliberate pre-sidecar
    # orphan cleanup — sol r17). For a t5-namespace name the sidecar
    # must also re-derive to it — an owner record for a different
    # guest is incoherent, not proof. An unreadable backing is
    # ambiguity -> keep the disk.
    _back=$(run_as_admin qemu-img info --output=json "$DISK" \
        2>/dev/null \
        | sed -n 's/.*"backing-filename": "\([^"]*\)".*/\1/p')
    if [ "$_back" != "$DISK_BASE" ]; then
        echo "[tier5-cleanup] FAIL: $DISK is not a tier-5 overlay" >&2
        echo "        (backing '${_back:-<unknown>}') — keeping it" >&2
        exit 5
    fi
    if ! _side=$(cat "$DISK.appid" 2>/dev/null) || [ -z "$_side" ]; then
        if [ "${TIER5_CLEANUP_ALLOW_OWNERLESS:-0}" != "1" ]; then
            echo "[tier5-cleanup] FAIL: $DISK has no appId sidecar —" >&2
            echo "        owner unproven; keeping it. To remove a" >&2
            echo "        deliberate pre-sidecar orphan, re-run with" >&2
            echo "        TIER5_CLEANUP_ALLOW_OWNERLESS=1" >&2
            exit 5
        fi
        _side="<unbound>"
        echo "[tier5-cleanup] WARN: $DISK has no appId sidecar —" >&2
        echo "        unowned orphan (explicitly allowed)" >&2
    elif [[ "$VM_NAME" =~ ^t5- ]] \
            && [ "$(tier5_derived_name "$_side")" != "$VM_NAME" ]; then
        echo "[tier5-cleanup] FAIL: $DISK is bound to '$_side'," >&2
        echo "        which derives a different guest — keeping it" >&2
        exit 5
    fi
    qemu_pids_for_domain; _lv=$?
    if [ "$_lv" -eq 1 ]; then
        rm -f "$DISK" "$DISK.appid"
        echo "[tier5-cleanup] removed $DISK (bound to '$_side')"
    else
        echo "[tier5-cleanup] FAIL: $VM_NAME qemu live or unverifiable" >&2
        echo "        — keeping $DISK" >&2
        exit 5
    fi
fi
