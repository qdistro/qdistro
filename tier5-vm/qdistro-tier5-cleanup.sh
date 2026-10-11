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
# The spawn boots the overlay through a bind mount under a root-owned
# dir — drop it here so a deleted overlay's inode is actually freed
# and no stale mount serves it later (sol r26).
BOOT_DIR="${TIER5_BOOT_DIR:-/run/qdistro-tier5-boot}"
BOOT_PATH="$BOOT_DIR/$VM_NAME.qcow2"
# Root-side ownership registry — a planted disk+sidecar pair can
# satisfy the admin-writable record but never this one (sol r33).
OWNED_DIR="${TIER5_OWNED_DIR:-/var/lib/qdistro/tier5-owned}"

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
        case "$c" in qemu-system-*|qemu-kvm) ;; *) continue ;; esac
        # And the candidate's OWN argv must carry guest=<name>, as the
        # argument right after `-name` — the ps-line substring alone
        # also matches a foreign qemu carrying it inside another
        # argument (file=/tmp/-name guest=t5-x,backup.qcow2) (sol r18).
        # Parse on NUL — the only delimiter an argv entry cannot
        # contain: newline-folding via tr lets a single crafted arg
        # manufacture the fake "-name\nguest=..." pair (sol r19).
        T5NAME="$name" python3 - "$p" >/dev/null <<'PY'
import os, sys
pid = sys.argv[1]
needle = b"guest=" + os.environ["T5NAME"].encode() + b","
try:
    argv = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")
except OSError:
    sys.exit(2)
sys.exit(0 if any(
    a == b"-name" and argv[i + 1].startswith(needle)
    for i, a in enumerate(argv[:-1])) else 1)
PY
        _avrc=$?
        case $_avrc in 0) out="$out $p" ;; 2) unread=1 ;; esac
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

# _read_owner_record / _unlink_verified — same contracts as
# spawn-tier5.sh: the record read binds contents and file-identity to
# one open fd (a swap can't split the pair), and the unlink captures
# the entry by rename into a private same-fs dir so only the verified
# inode is ever removed — a check→rm swap restores the replacement
# instead of deleting it (sol r25). Every step runs through dir fds
# bound at open, so renaming the capture dir mid-op cannot redirect
# the final unlink at a planted path (sol r26).
_read_owner_record() {
    _sapp="" _sident="" _sfid=""
    local _p=$1 _rfd
    [ -f "$_p" ] && [ ! -L "$_p" ] || return 0
    # NB: 2>/dev/null on bare exec would apply to the shell
    # permanently — the brace group carries the suppression instead.
    { exec {_rfd}< "$_p"; } 2>/dev/null || return 0
    read -r _sapp _sident <&$_rfd 2>/dev/null || true
    _sfid=$(stat -Lc '%d %i' "/proc/$$/fd/$_rfd" 2>/dev/null)
    exec {_rfd}<&-
}

# Root-side ownership record — see spawn-tier5.sh (sol r33).
_oapp=""; _oident=""
_read_owned() {
    _oapp=""; _oident=""
    local _f="$OWNED_DIR/$1"
    [ -f "$_f" ] && [ ! -L "$_f" ] || return 0
    read -r _oapp _oident <"$_f" 2>/dev/null || true
}
_drop_owned() {
    rm -f "$OWNED_DIR/$1" 2>/dev/null || true
}

# _unlink_verified <path> <dev-ino> — remove <path> only if it still
# names the verified inode. The entry is captured into a root-owned
# 0700 dir created AND opened through the parent's dir fd, the opened
# fd verified as OUR EMPTY chamber (euid + mode + empty) before
# anything moves in — no create→open window (sol r28), no leftover-
# chamber clobber (sol r30), and a co-admin cannot reach inside the
# dir to swap the entry between stat and unlink (sol r29). The
# capture move itself is renameat2(RENAME_NOREPLACE). See
# spawn-tier5.sh for the full contract.
_unlink_verified() {
    local _p=$1 _want=$2
    [ -n "$_want" ] || return 1
    T5U_PATH="$_p" T5U_WANT="$_want" python3 - <<'PY'
import os, stat, sys

_p = os.environ["T5U_PATH"]
_wd, _wi = os.environ["T5U_WANT"].split()
_want = (int(_wd), int(_wi))
_parent = os.path.dirname(_p) or "."
_name = os.path.basename(_p)


def _rename_noreplace(srcfd, src, dstfd, dst):
    import ctypes
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        nr = {"x86_64": 316, "aarch64": 276}.get(os.uname().machine)
        if nr is None:
            return False
        return libc.syscall(nr, srcfd, os.fsencode(src),
                            dstfd, os.fsencode(dst), 1) == 0
    except (OSError, AttributeError):
        return False


try:
    pfd = os.open(_parent,
                  os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
except OSError:
    sys.exit(1)
rc = 1
cap = ".t5u-" + os.urandom(8).hex()
try:
    try:
        os.mkdir(cap, 0o700, dir_fd=pfd)
    except OSError:
        pass
    else:
        tfd = -1
        try:
            try:
                tfd = os.open(cap,
                              os.O_RDONLY | os.O_DIRECTORY
                              | os.O_NOFOLLOW, dir_fd=pfd)
            except OSError:
                tfd = -1
            else:
                _st = os.fstat(tfd)
                if not (stat.S_ISDIR(_st.st_mode)
                        and _st.st_uid == os.geteuid()
                        and _st.st_mode & 0o777 == 0o700
                        and os.listdir(tfd) == []):
                    os.close(tfd)
                    tfd = -1
            if tfd >= 0:
                if _rename_noreplace(pfd, _name, tfd, "v"):
                    try:
                        st = os.stat("v", dir_fd=tfd,
                                     follow_symlinks=False)
                    except OSError:
                        st = None
                    if st is not None and stat.S_ISREG(st.st_mode) \
                            and (st.st_dev, st.st_ino) == _want:
                        os.unlink("v", dir_fd=tfd)
                        rc = 0
                    elif st is not None:
                        restored = False
                        try:
                            os.link("v", _name, src_dir_fd=tfd,
                                    dst_dir_fd=pfd,
                                    follow_symlinks=False)
                            os.unlink("v", dir_fd=tfd)
                            restored = True
                        except OSError:
                            restored = _rename_noreplace(
                                tfd, "v", pfd, _name)
                        if not restored:
                            sys.stderr.write(
                                "[tier5-cleanup] WARN: preserved"
                                " foreign entry in %s/%s\n"
                                % (_parent, cap))
        finally:
            if tfd >= 0:
                os.close(tfd)
            try:
                os.rmdir(cap, dir_fd=pfd)
            except OSError:
                pass
finally:
    os.close(pfd)
sys.exit(rc)
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

# domain_disk_source / domain_holds_owned_disk — same contract as
# spawn-tier5.sh: the registry vouches for the disk pair, not for
# whichever domain currently holds the name, so a destroy also
# requires the domain to provably run the recorded overlay inode —
# a running guest through the open fd of an argv-verified
# guest=<name> qemu, a stopped
# one through its declared source being our pinned boot path or the
# recorded inode itself. An unreliable qemu scan is ambiguity, never
# a stopped-domain proof (sol r35/r36).
domain_disk_source() {
    run_as_admin virsh dumpxml "$VM_NAME" 2>/dev/null | python3 -c '
import sys, xml.etree.ElementTree as ET
try:
    root = ET.parse(sys.stdin).getroot()
except ET.ParseError:
    sys.exit(1)
disks = list(root.iter("disk"))
if len(disks) != 1:
    sys.exit(1)
src = disks[0].find("source")
p = (src.get("file") or src.get("dev")) if src is not None else ""
if p:
    print(p)
'
}

domain_holds_owned_disk() {
    local ident=$1 src qp qrc pid fd tgt
    [ -n "$ident" ] || return 1
    src=$(domain_disk_source)
    [ -n "$src" ] || return 1
    qp=$(qemu_pids_for_domain); qrc=$?
    # An unreliable scan is ambiguity — never converted into the
    # stopped-domain proof (sol r36).
    [ "$qrc" -eq 2 ] && return 1
    if [ "$qrc" -eq 0 ]; then
        # Any argv-verified guest=<name> qemu holding the recorded
        # inode open on the declared source proves it. (virsh has no
        # `dompid`; a same-uid decoy is out of scope — README.)
        for pid in $qp; do
            [ -d "/proc/$pid/fd" ] || continue
            for fd in "/proc/$pid"/fd/*; do
                [ -e "$fd" ] || continue
                tgt=$(readlink "$fd" 2>/dev/null) || continue
                [ "$tgt" = "$src" ] || [ "$tgt" = "$src (deleted)" ] \
                    || continue
                [ "$(stat -Lc '%d %i' "$fd" 2>/dev/null)" = "$ident" ] \
                    && return 0
            done
        done
        return 1
    fi
    [ "$src" = "$BOOT_PATH" ] \
        || [ "$(stat -Lc '%d %i' -- "$src" 2>/dev/null)" = "$ident" ]
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
# `>>` never replaces an existing inode (install unlinks first, which
# would split the flock namespace under a racing first user).
( umask 077; : >>"$HOLD_FILE" ) 2>/dev/null || true
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
    # The tag itself is forgeable: anyone in the admin session can
    # stamp a matching appId on a domain we never created. Destroying
    # needs the root-side ownership record to agree — a forged guest
    # can satisfy the XML check but can never mint it (sol r34). And
    # the record binds the disk pair, not the domain: a same-tag
    # replacement on a different disk must ALSO be refused — the
    # domain only goes when it provably runs the recorded inode
    # (sol r35).
    _read_owned "$VM_NAME"
    if [ "$_oapp" != "$_bound" ]; then
        echo "[tier5-cleanup] FAIL: $VM_NAME carries a tier-5 binding" >&2
        echo "        with no matching root-side ownership record —" >&2
        echo "        refusing to destroy a forged guest" >&2
        exit 3
    fi
    if ! domain_holds_owned_disk "$_oident"; then
        echo "[tier5-cleanup] FAIL: $VM_NAME does not run the recorded" >&2
        echo "        overlay — refusing to destroy a replaced guest" >&2
        exit 3
    fi
    # destroy can no-op under a lying domstate; there is no pid-signalling
    # fallback — the confirmed-dead gate below then preserves the overlay.
    run_as_admin virsh destroy "$VM_NAME" >/dev/null 2>&1 || true
    if ! run_as_admin virsh undefine "$VM_NAME" >/dev/null 2>&1; then
        echo "[tier5-cleanup] WARN: undefine $VM_NAME failed" >&2
    fi
    echo "[tier5-cleanup] $VM_NAME destroyed + undefined"
elif [ "$_ds" -eq 1 ]; then
    echo "[tier5-cleanup] $VM_NAME not present"
else
    echo "[tier5-cleanup] WARN: cannot query $VM_NAME — libvirt error" >&2
fi

if [ -e "$DISK" ] || [ -L "$DISK" ]; then
    # A symlinked or non-regular $DISK is foreign construction — a
    # symlink to the bound inode would satisfy the dev/ino check
    # below while the path itself is unowned (sol r24). Preserve it.
    if [ ! -f "$DISK" ] || [ -L "$DISK" ]; then
        echo "[tier5-cleanup] FAIL: $DISK is not a regular file —" >&2
        echo "        keeping it" >&2
        exit 5
    fi
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
    # owner record, stamped as "<appId> <dev> <ino>". A MISSING sidecar
    # means unproven ownership: refuse unless the operator explicitly
    # opts in (a deliberate pre-sidecar orphan cleanup — sol r17). A
    # PRESENT one must bind THIS inode — a record copied beside a
    # different disk is not proof (sol r19) — and for a t5-namespace
    # name it must re-derive to it. An unreadable backing or an
    # incoherent record is ambiguity -> keep the disk. The backing
    # read itself happens below on the PINNED inode — a path-based
    # qemu-img here could be raced into checking a different file
    # than the one we unlink (sol r27).
    _read_owner_record "$DISK.appid"
    _read_owned "$VM_NAME"
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
        # The admin-writable sidecar alone is forgeable — the
        # root-side registry must agree before root deletes
        # (sol r33).
        if [ "$_oapp" != "$_sapp" ] || [ "$_oident" != "$_dino" ]; then
            echo "[tier5-cleanup] FAIL: no matching root-side" >&2
            echo "        ownership record for $VM_NAME — a planted" >&2
            echo "        pair is not ours; keeping it" >&2
            exit 5
        fi
        if [[ "$VM_NAME" =~ ^t5- ]] \
                && [ "$(tier5_derived_name "$_sapp")" != "$VM_NAME" ]; then
            echo "[tier5-cleanup] FAIL: $DISK is bound to '$_sapp'," >&2
            echo "        which derives a different guest — keeping it" >&2
            exit 5
        fi
    fi
    qemu_pids_for_domain; _lv=$?
    if [ "$_lv" -eq 1 ]; then
        # confirmed dead — pin the verified inode at the root-owned
        # boot path: the mounted inode is the only read surface a
        # name-swap cannot redirect, and the backing check must run
        # on it, not on the mutable $DISK path (sol r27)
        install -d -m 0755 "$BOOT_DIR" 2>/dev/null
        [ -e "$BOOT_PATH" ] \
            || install -m 0644 /dev/null "$BOOT_PATH" 2>/dev/null
        umount -l "$BOOT_PATH" 2>/dev/null || true
        if ! mount --bind "$DISK" "$BOOT_PATH" 2>/dev/null \
                || [ "$(stat -c '%d %i' "$BOOT_PATH" 2>/dev/null)" \
                    != "$_dino" ]; then
            echo "[tier5-cleanup] FAIL: cannot pin $DISK for" >&2
            echo "        verification — keeping it" >&2
            umount -l "$BOOT_PATH" 2>/dev/null || true
            rm -f "$BOOT_PATH" 2>/dev/null || true
            exit 5
        fi
        _back=$(run_as_admin qemu-img info --output=json "$BOOT_PATH" \
            2>/dev/null \
            | sed -n 's/.*"backing-filename": "\([^"]*\)".*/\1/p')
        if [ "$_back" != "$DISK_BASE" ]; then
            umount -l "$BOOT_PATH" 2>/dev/null || true
            rm -f "$BOOT_PATH" 2>/dev/null || true
            echo "[tier5-cleanup] FAIL: $DISK is not a tier-5" >&2
            echo "        overlay (backing '${_back:-<unknown>}')" >&2
            echo "        — keeping it" >&2
            exit 5
        fi
        # Check→rm is a swap window (sol r25): verified-unlink removes
        # only the inode these checks passed; for the ownerless opt-in
        # path _dino still binds the disk itself, and a sidecar that
        # never existed simply has nothing to unlink.
        if _unlink_verified "$DISK" "$_dino"; then
            _unlink_verified "$DISK.appid" "${_sfid:-}" 2>/dev/null || true
            _drop_owned "$VM_NAME"
            umount -l "$BOOT_PATH" 2>/dev/null || true
            rm -f "$BOOT_PATH" 2>/dev/null || true
            echo "[tier5-cleanup] removed $DISK (bound to '$_sapp')"
        else
            umount -l "$BOOT_PATH" 2>/dev/null || true
            rm -f "$BOOT_PATH" 2>/dev/null || true
            echo "[tier5-cleanup] FAIL: $DISK changed under" >&2
            echo "        verification — keeping it" >&2
            exit 5
        fi
    else
        echo "[tier5-cleanup] FAIL: $VM_NAME qemu live or unverifiable" >&2
        echo "        — keeping $DISK" >&2
        exit 5
    fi
fi
