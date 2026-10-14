#!/bin/bash
# Tier-5 cleanup: destroy + undefine a libvirt domain spawned by
# qdistro-tier5-spawn --vm, and remove its linked-clone disk. Idempotent.
#
# Runs as the admin user (the domain lives in admin's qemu:///session and
# the overlay in admin's image dir); root execution is refused.
#
# Usage:
#   qdistro-tier5-cleanup.sh <vm_name>
set -uo pipefail

VM_NAME="${1:?usage: $0 <vm_name>}"
# Same shape the spawn enforces: VM_NAME becomes a path component below.
if ! [[ "$VM_NAME" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$ ]]; then
    echo "[tier5-cleanup] FAIL: invalid vm_name '$VM_NAME'" >&2
    exit 2
fi
if [ "$(id -u)" -eq 0 ]; then
    echo "[tier5-cleanup] FAIL: run as the admin user, not root" >&2
    exit 2
fi
# A deferred upgrade leaves the old root-run lifecycle's locks in place:
# its wrappers' holds are invisible to ours, so refuse (see spawn-tier5.sh
# and scripts/install/lib/tier5-legacy.sh).
for _legacy in ${TIER5_LEGACY_STATE_DIRS-/run/qdistro-tier5 /run/qdistro-tier5-boot}; do
    if [ -e "$_legacy" ]; then
        echo "[tier5-cleanup] FAIL: $_legacy exists — the pre-bridge root-run" >&2
        echo "        lifecycle has not been retired; re-run" >&2
        echo "        install-tier5-for-vm.sh once its sessions end, or reboot" >&2
        exit 4
    fi
done
DISK_DIR="${TIER5_DISK_DIR:-$HOME/.local/share/libvirt/images}"
DISK="$DISK_DIR/$VM_NAME.qcow2"
DISK_BASE="${TIER5_BASE_DISK:-/var/lib/libvirt/images/qdistro-tier5-base.qcow2}"
RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export XDG_RUNTIME_DIR="$RUNTIME_DIR"
export LIBVIRT_DEFAULT_URI="qemu:///session"

# qemu_pids_for_domain — same contract as spawn-tier5.sh: 0 pids live,
# 1 confirmed dead, 2 scan unreliable (including a process of ours
# unreadable for any reason but disappearance). domstate can report "shut off" for
# a LIVE qemu under nested virt, so unlinking the overlay is gated on a
# proven-dead scan, never on the destroy/undefine attempts.
qemu_pids_for_domain() {
    local name="${1:-$VM_NAME}" out rc
    out=$(T5NAME="$name" python3 - <<'PY'
import errno, os, sys
needle = b"guest=" + os.environ["T5NAME"].encode() + b","
me = os.getuid()
# Only these prove the process is gone; any other failure (EACCES, EIO,
# ...) on a candidate means it may be a live qemu we could not read.
GONE = (errno.ENOENT, errno.ESRCH)
try:
    pids = [p for p in os.listdir("/proc") if p.isdigit()]
except OSError:
    sys.exit(2)
found = []
unsure = False
for p in pids:
    try:
        if os.stat(f"/proc/{p}").st_uid != me:
            continue
        with open(f"/proc/{p}/comm", "rb") as f:
            comm = f.read().strip()
        if not (comm.startswith(b"qemu-system-") or comm == b"qemu-kvm"):
            continue
        with open(f"/proc/{p}/cmdline", "rb") as f:
            argv = f.read().split(b"\0")
    except OSError as e:
        if e.errno not in GONE:
            unsure = True
        continue
    if any(a == b"-name" and argv[i + 1].startswith(needle)
           for i, a in enumerate(argv[:-1])):
        found.append(p)
if found:
    print(" ".join(found))
    sys.exit(0)
sys.exit(2 if unsure else 1)
PY
)
    rc=$?
    case $rc in
        0) printf '%s' "$out"; return 0 ;;
        1) return 1 ;;
        *) return 2 ;;
    esac
}

# tier5_derived_name <appId> — port of VMApps._vmNameForApp: a binding or
# sidecar must re-derive to the artifact's own name before a t5-namespace
# artifact is treated as that app's.
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

# _read_owner_record <path> — "<appId> <dev> <ino>" into _sapp / _sident.
_read_owner_record() {
    _sapp="" _sident=""
    local line=""
    [ -f "$1" ] && [ ! -L "$1" ] || return 0
    IFS= read -r line <"$1" 2>/dev/null || [ -n "$line" ] || return 0
    case "$line" in
        *" "*) _sapp=${line%% *}; _sident=${line#* } ;;
        *) _sapp=$line ;;
    esac
}

# domain_defined_state — rc 0 defined, 1 CONFIRMED absent, 2 cannot
# determine (an enumeration that succeeds is authoritative).
domain_defined_state() {
    local list
    list=$(virsh list --all --name 2>/dev/null) || return 2
    printf '%s\n' "$list" | grep -Fxq -- "$VM_NAME" && return 0
    return 1
}

# Serialize on the domain's hold file — the spawn wrappers' flock protocol.
# A live wrapper holds it shared for its whole session, so this bounded
# EXCLUSIVE claim either wins (no wrapper owns the domain) or loses (refuse
# rather than destroy a guest mid-session). Holding it also keeps any spawn
# from defining/starting the domain between the checks and the unlink.
HOLD_DIR="$RUNTIME_DIR/qdistro-tier5"
HOLD_FILE="$HOLD_DIR/hold-$VM_NAME"
mkdir -m 0700 "$HOLD_DIR" 2>/dev/null || true
( umask 077; : >>"$HOLD_FILE" ) 2>/dev/null || true
if ! exec {HOLD_FD}>>"$HOLD_FILE"; then
    echo "[tier5-cleanup] FAIL: cannot open $HOLD_FILE" >&2
    exit 2
fi
if ! flock -w 5 -x "$HOLD_FD" 2>/dev/null; then
    echo "[tier5-cleanup] FAIL: a live wrapper session holds $VM_NAME" >&2
    echo "        — stop the wrapper first, or let its own teardown run" >&2
    exit 4
fi

domain_defined_state; _ds=$?
if [ "$_ds" -eq 0 ]; then
    # The hold proves no wrapper uses the name — not that the domain is
    # tier-5's. Require the appId binding the spawn stamps at define time,
    # and for a t5-namespace name that binding must re-derive to it.
    _bound=$(virsh dumpxml "$VM_NAME" 2>/dev/null \
        | tr -d '\n' \
        | sed -n 's|.*<qdistro:appId[^>]*>\([^<]*\)</qdistro:appId>.*|\1|p')
    if [ -z "$_bound" ]; then
        echo "[tier5-cleanup] FAIL: $VM_NAME carries no tier-5 appId" >&2
        echo "        binding — not provably ours; refusing to destroy" >&2
        exit 3
    fi
    if [[ "$VM_NAME" =~ ^t5- ]] \
            && [ "$(tier5_derived_name "$_bound")" != "$VM_NAME" ]; then
        echo "[tier5-cleanup] FAIL: $VM_NAME is bound to '$_bound'," >&2
        echo "        which derives a different guest — refusing to destroy" >&2
        exit 3
    fi
    # destroy can no-op under a lying domstate; there is no pid-signalling
    # fallback — the confirmed-dead gate below then preserves the overlay.
    virsh destroy "$VM_NAME" >/dev/null 2>&1 || true
    if ! virsh undefine "$VM_NAME" >/dev/null 2>&1; then
        echo "[tier5-cleanup] WARN: undefine $VM_NAME failed" >&2
    fi
    echo "[tier5-cleanup] $VM_NAME destroyed + undefined"
elif [ "$_ds" -eq 1 ]; then
    echo "[tier5-cleanup] $VM_NAME not present"
else
    echo "[tier5-cleanup] WARN: cannot query $VM_NAME — libvirt error" >&2
fi

if [ -e "$DISK" ] || [ -L "$DISK" ]; then
    if [ ! -f "$DISK" ] || [ -L "$DISK" ]; then
        echo "[tier5-cleanup] FAIL: $DISK is not a regular file —" >&2
        echo "        keeping it" >&2
        exit 5
    fi
    # The overlay goes only when NO startable definition remains and no
    # backing qemu lives.
    domain_defined_state; _ds=$?
    if [ "$_ds" -eq 0 ]; then
        echo "[tier5-cleanup] FAIL: $VM_NAME still defined — keeping $DISK" >&2
        exit 5
    elif [ "$_ds" -ne 1 ]; then
        echo "[tier5-cleanup] FAIL: cannot verify $VM_NAME is gone" >&2
        echo "        — keeping $DISK" >&2
        exit 5
    fi
    # Provenance: the file must clone OUR base, and its sidecar must bind
    # this inode (and, for a t5 name, re-derive to it). A missing sidecar
    # is unproven ownership unless the operator explicitly opts in.
    _read_owner_record "$DISK.appid"
    _dino=$(stat -c '%d %i' "$DISK" 2>/dev/null)
    if [ -z "$_sapp" ]; then
        if [ -e "$DISK.appid" ] || [ -L "$DISK.appid" ]; then
            echo "[tier5-cleanup] FAIL: $DISK.appid is not a readable" >&2
            echo "        regular-file record — keeping both" >&2
            exit 5
        fi
        if [ "${TIER5_CLEANUP_ALLOW_OWNERLESS:-0}" != "1" ]; then
            echo "[tier5-cleanup] FAIL: $DISK has no appId sidecar —" >&2
            echo "        owner unproven; keeping it. To remove a" >&2
            echo "        deliberate pre-sidecar orphan, re-run with" >&2
            echo "        TIER5_CLEANUP_ALLOW_OWNERLESS=1" >&2
            exit 5
        fi
        _sapp="<unbound>"
        echo "[tier5-cleanup] WARN: $DISK has no appId sidecar —" >&2
        echo "        unowned orphan (explicitly allowed)" >&2
    else
        if [ -z "$_dino" ] || [ "$_sident" != "$_dino" ]; then
            echo "[tier5-cleanup] FAIL: $DISK.appid does not bind this" >&2
            echo "        disk (copied or stale record) — keeping it" >&2
            exit 5
        fi
        if [[ "$VM_NAME" =~ ^t5- ]] \
                && [ "$(tier5_derived_name "$_sapp")" != "$VM_NAME" ]; then
            echo "[tier5-cleanup] FAIL: $DISK is bound to '$_sapp'," >&2
            echo "        which derives a different guest — keeping it" >&2
            exit 5
        fi
    fi
    qemu_pids_for_domain >/dev/null; _lv=$?
    if [ "$_lv" -ne 1 ]; then
        echo "[tier5-cleanup] FAIL: $VM_NAME qemu live or unverifiable" >&2
        echo "        — keeping $DISK" >&2
        exit 5
    fi
    _back=$(qemu-img info -U --output=json "$DISK" 2>/dev/null \
        | sed -n 's/.*"backing-filename": "\([^"]*\)".*/\1/p')
    if [ "$_back" != "$DISK_BASE" ]; then
        echo "[tier5-cleanup] FAIL: $DISK is not a tier-5" >&2
        echo "        overlay (backing '${_back:-<unknown>}')" >&2
        echo "        — keeping it" >&2
        exit 5
    fi
    rm -f -- "$DISK" "$DISK.appid"
    echo "[tier5-cleanup] removed $DISK (bound to '$_sapp')"
fi
