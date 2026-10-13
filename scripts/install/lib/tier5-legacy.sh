# shellcheck shell=bash
# tier5-legacy.sh — retire the pre-bridge, root-run tier-5 lifecycle.
#
# Sourced by install-tier5-for-vm.sh (and by its unit test). Before the
# bridge split, qdistro-tier5-spawn/-cleanup ran as ROOT and kept:
#   /run/qdistro-tier5/           flock files (hold-/create-/live-<vm>)
#   /run/qdistro-tier5-boot/      bind mounts the guests booted from
#   /var/lib/qdistro/tier5-owned/ the root ownership registry
# The new lifecycle runs as admin and keeps its locks under
# $XDG_RUNTIME_DIR/qdistro-tier5. Deleting the old state under a live old
# wrapper would split the lock namespace: the old wrapper's shared hold is
# invisible to the new wrappers, so a new launch could adopt its guest and
# then destroy it while the old app still runs — and the deleted registry
# would break the old wrapper's own ownership-backed cleanup.
#
# Design (migrate when quiescent, otherwise defer):
#   - The installer replaces the wrappers and the polkit policy BEFORE this
#     step, so no new old-lifecycle wrapper can start (pkexec of
#     qdistro-tier5-spawn now runs the new script, which refuses root).
#   - t5_legacy_retire takes every old lock exclusively, then scans for old
#     wrappers (root processes running the old spawn/cleanup) and old guests
#     (any process whose argv names the old boot dir). Quiescent: unmount,
#     delete all old state. Busy (or unverifiable): delete NOTHING.
#   - The new spawn/cleanup refuse to run while /run/qdistro-tier5 or
#     /run/qdistro-tier5-boot exists (root-owned, under root-only /run, so
#     admin cannot fake or clear them). A deferred migration therefore holds
#     the new lifecycle off until the installer is re-run once the old
#     sessions have ended, or until a reboot clears /run. The registry left
#     behind by a deferral is never read; the next quiescent run deletes it.
#
# Paths are variables so the unit test can point them at a scratch tree.

T5_LEGACY_HOLD_DIR="${T5_LEGACY_HOLD_DIR:-/run/qdistro-tier5}"
T5_LEGACY_BOOT_DIR="${T5_LEGACY_BOOT_DIR:-/run/qdistro-tier5-boot}"
T5_LEGACY_OWNED_DIR="${T5_LEGACY_OWNED_DIR:-/var/lib/qdistro/tier5-owned}"
# The old wrappers ran as root; new-lifecycle wrappers (admin) never count.
T5_LEGACY_WRAPPER_UID="${T5_LEGACY_WRAPPER_UID:-0}"

# t5_legacy_scan — rc 0 = quiescent, 1 = busy, 2 = unverifiable. Prints one
# reason per busy finding. Only ENOENT/ESRCH (the process is gone) are
# ignored; any other read failure is unverifiable, never "quiescent".
t5_legacy_scan() {
    T5L_BOOT="$T5_LEGACY_BOOT_DIR" T5L_UID="$T5_LEGACY_WRAPPER_UID" \
        python3 - <<'PY'
import errno, os, sys
boot = os.environ["T5L_BOOT"].rstrip("/").encode() + b"/"
wuid = int(os.environ["T5L_UID"])
tools = (b"spawn-tier5.sh", b"qdistro-tier5-spawn",
         b"qdistro-tier5-cleanup.sh", b"qdistro-tier5-cleanup")
GONE = (errno.ENOENT, errno.ESRCH)
me = os.getpid()
try:
    pids = [int(p) for p in os.listdir("/proc") if p.isdigit()]
except OSError:
    sys.exit(2)
busy = unsure = False
for pid in pids:
    if pid == me:
        continue
    try:
        uid = os.stat(f"/proc/{pid}").st_uid
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            argv = f.read().split(b"\0")
    except OSError as e:
        if e.errno not in GONE:
            unsure = True
        continue
    if any(boot in a for a in argv):
        print(f"pid {pid}: a guest still boots from {boot.decode()}")
        busy = True
    elif uid == wuid and any(os.path.basename(a) in tools for a in argv[:3]):
        print(f"pid {pid}: an old root-run tier-5 wrapper is running")
        busy = True
sys.exit(1 if busy else 2 if unsure else 0)
PY
}

# t5_legacy_present — rc 0 when any old-lifecycle state exists.
t5_legacy_present() {
    [ -e "$T5_LEGACY_HOLD_DIR" ] || [ -e "$T5_LEGACY_BOOT_DIR" ] \
        || [ -e "$T5_LEGACY_OWNED_DIR" ]
}

# t5_legacy_retire — rc 0 = migrated (or nothing to migrate), 1 = deferred:
# old sessions are live or unverifiable and ALL old state was left alone.
t5_legacy_retire() {
    t5_legacy_present || return 0
    local f fd rc=0 held=() m
    # Exclusive on every old lock: a wrapper holding one (shared for its
    # whole session) makes this fail; holding them keeps any wrapper still
    # between open and flock from proceeding until the files are gone.
    if [ -d "$T5_LEGACY_HOLD_DIR" ]; then
        for f in "$T5_LEGACY_HOLD_DIR"/*; do
            [ -f "$f" ] && [ ! -L "$f" ] || continue
            if ! { exec {fd}>>"$f"; } 2>/dev/null; then
                echo "[tier5-legacy] cannot open old lock $f"; rc=1; continue
            fi
            held+=("$fd")
            if ! flock -n -x "$fd" 2>/dev/null; then
                echo "[tier5-legacy] old lock $f is held by a live wrapper"
                rc=1
            fi
        done
    fi
    if [ "$rc" -eq 0 ]; then
        t5_legacy_scan; case $? in
            0) ;;
            1) rc=1 ;;
            *) echo "[tier5-legacy] cannot verify that no old tier-5 session runs"
               rc=1 ;;
        esac
    fi
    if [ "$rc" -eq 0 ]; then
        if [ -d "$T5_LEGACY_BOOT_DIR" ]; then
            for m in "$T5_LEGACY_BOOT_DIR"/*.qcow2; do
                [ -e "$m" ] || continue
                umount -l "$m" 2>/dev/null || true
            done
        fi
        rm -rf -- "$T5_LEGACY_BOOT_DIR" "$T5_LEGACY_HOLD_DIR" "$T5_LEGACY_OWNED_DIR"
    fi
    for fd in "${held[@]}"; do exec {fd}>&-; done
    return "$rc"
}
