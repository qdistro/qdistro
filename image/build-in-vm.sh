#!/bin/bash
# build-in-vm.sh — drive the kiwi OEM build inside a libvirt VM
# (qemu:///session, rootless on the host) so we don't need sudo on
# the host. The VM cloned from baseweed.qcow2 has root, sudoers,
# all the qdistro deps already, and network via SLIRP.
#
# What this does:
#   1. Clones baseweed -> qdistro-builder-<ts> via qdistro/scripts/vm/.
#   2. Attaches a fresh 120 GiB qcow2 to host the kiwi workspace
#      ($BUILD_DIR inside the VM).
#   3. Bakes the entire qdistro-image/ description (with sources
#      already rsynced under root/root/qdistro-src/) into the VM at
#      /root/qdistro-image/ via virt-copy-in.
#   4. Starts the VM, waits for qga.
#   5. Inside the VM: zypper in python3-kiwi + systemdeps, format/mount
#      /dev/vdb as /build, run kiwi-ng system build.
#   6. virt-copy-out the resulting .raw / .install.iso back to the host
#      $BUILD_DIR.
#   7. (Default) keeps the VM around for re-runs; --teardown wipes it.
#
# Pattern lineage: qdistro/scripts/vm/clone-baseweed.sh +
# fresh-vm-bootstrap.sh; reuses vm-exec, vm-start-and-wait verbatim.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# image/ lives inside the qdistro repo; the old "$HERE/../qdistro" form dates
# from the pre-import sibling layout and cannot resolve from the in-repo path
# (iso/14 Phase A item 1).
QDISTRO="$(cd "$HERE/.." && pwd)"
VM_TOOLS="$QDISTRO/scripts/vm"
IMG_DIR="${QDWIN_IMG_DIR:-$HOME/.local/share/libvirt/images}"
URI="qemu:///session"
export LIBVIRT_DEFAULT_URI="$URI"

VM="${QDISTRO_BUILDER_VM:-qdistro-builder-$(date +%y%m%d-%H%M)}"
# 120 GiB: the raw, the kiwi image-root tree and the compressed output all
# live on this disk at once (iso/14 Phase A item 6).
BUILD_DISK_GB="${QDISTRO_BUILD_DISK_GB:-120}"
# Never /tmp: it is a tmpfs on the build hosts and a multi-GiB raw does not
# fit in RAM (iso/14 Phase A item 6).
HOST_BUILD_DIR="${QDISTRO_BUILD_DIR:-/var/tmp/qdistro-build}"
# Forwarded into the in-VM kiwi run so config.sh's profile gate sees it
# (release = no passwordless sudo; dev = passwordless sudo for test harnesses).
# This is a shell variable read by config.sh, NOT a kiwi XML profile.
# The default stays `release`, the SAFE profile: dev means default credentials
# and passwordless sudo, so an unqualified build must never produce it by
# accident. A tester build passes QDISTRO_PROFILE=dev explicitly (iso/14
# Phase A item 5). The value is validated so a typo cannot silently select
# the other image instead of failing.
QDISTRO_PROFILE="${QDISTRO_PROFILE:-release}"
case "$QDISTRO_PROFILE" in
    dev|release) ;;
    *) printf '\033[1;31m[in-vm] FATAL:\033[0m QDISTRO_PROFILE must be dev or release, got: %s\n' "$QDISTRO_PROFILE" >&2; exit 1 ;;
esac
LOGS="$HERE/logs/in-vm-$(date +%y%m%d-%H%M%S)"
mkdir -p "$LOGS" "$HOST_BUILD_DIR"

log()  { printf '\033[1;36m[in-vm]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[in-vm] WARN:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[in-vm] FATAL:\033[0m %s\n' "$*" >&2; exit 1; }

vmx() {
    "$VM_TOOLS/vm-exec" "$VM" "$@"
}
vms() {
    # vm-script handles multi-line + embedded quotes via base64.
    "$VM_TOOLS/vm-script" "$VM"
}

#-- 0. Args -------------------------------------------------------------------
TEARDOWN=0
KEEP_RUNNING=0
REUSE=0
TEARDOWN_VM=""
while [ $# -gt 0 ]; do
    case "$1" in
        --teardown)
            TEARDOWN=1
            if [ -n "${2:-}" ] && [[ "${2:-}" != --* ]]; then
                TEARDOWN_VM="$2"; shift
            fi
            ;;
        --keep)     KEEP_RUNNING=1 ;;
        --reuse)    REUSE=1 ;;
        -h|--help)
            sed -n '2,30p' "$0"; exit 0 ;;
        *) die "unknown arg: $1" ;;
    esac
    shift
done

if [ "$TEARDOWN" = 1 ]; then
    [ -n "$TEARDOWN_VM" ] && VM="$TEARDOWN_VM"
    log "tearing down $VM"
    virsh destroy  "$VM" 2>/dev/null || true
    virsh undefine "$VM" --remove-all-storage 2>/dev/null || true
    rm -f "$IMG_DIR/$VM.qcow2" "$IMG_DIR/$VM-build.qcow2"
    exit 0
fi

#-- 1. Pre-flight checks ------------------------------------------------------
# The overlay is a build product, not a checked-in tree (image/.gitignore),
# so a fresh checkout always lacks it. Sync it rather than telling the caller
# to run a second command (iso/14 Phase A item 3). The sync is idempotent.
log "syncing sources into the overlay"
bash "$HERE/build.sh" --sync-only 2>&1 | tee "$LOGS/sync.log"
[ -d "$HERE/root/root/qdistro-src" ] || die "sync did not produce $HERE/root/root/qdistro-src"

# Record what went in, so a built artifact can be traced to five commits.
# Best-effort: a synced-from-tarball tree has no .git.
SIBLINGS="$(cd "$HERE/../.." && pwd)"
# "DIRTY" alone cannot distinguish two different uncommitted states on the
# same parent, so a dirty tree also records the sha256 of its diff against
# HEAD (tracked changes; untracked files are listed by count).
for repo in qdistro qdwin qdshell qdgreeter qdlocker; do
    if [ -d "$SIBLINGS/$repo/.git" ]; then
        if git -C "$SIBLINGS/$repo" status --porcelain 2>/dev/null | grep -q .; then
            state="DIRTY diff-sha256=$(git -C "$SIBLINGS/$repo" diff HEAD 2>/dev/null | sha256sum | cut -c1-16) untracked=$(git -C "$SIBLINGS/$repo" status --porcelain 2>/dev/null | grep -c '^??')"
        else
            state=clean
        fi
        printf '%-10s %s %s\n' "$repo" \
            "$(git -C "$SIBLINGS/$repo" rev-parse HEAD 2>/dev/null || echo unknown)" "$state"
    else
        printf '%-10s %s\n' "$repo" "no-git"
    fi
done | tee "$LOGS/sources.txt"

# The clone below is --from-baked, so baseweed-baked.qcow2 is the image that
# must exist; baseweed.qcow2 is not used by this path (iso/14 Phase A item 4).
[ -f "$IMG_DIR/baseweed-baked.qcow2" ] || die "$IMG_DIR/baseweed-baked.qcow2 missing (build it via scripts/vm/build-baked-baseweed.sh)"
virsh dominfo qdistro-template >/dev/null 2>&1 || die "qdistro-template domain missing"

#-- 2. Clone baseweed via the project's own tool ------------------------------
if [ "$REUSE" = 1 ] && virsh dominfo "$VM" >/dev/null 2>&1; then
    log "reusing existing VM $VM"
else
    log "cloning baseweed -> $VM"
    # clone-baseweed.sh appends its own -YYMMDD-HHMM suffix and prints
    # the resulting name on stdout. We pass a fixed prefix and recover
    # the real name from its output.
    PREFIX="qdistro-builder"
    # --from-baked: baseweed-baked.qcow2 has qga enabled via device-units
    # (baseweed.qcow2 doesn't) and the qdistro deps preinstalled so the
    # in-VM zypper for kiwi-ng is fast.
    CLONE_OUT=$("$VM_TOOLS/clone-baseweed.sh" "$PREFIX" --from-baked 2>&1 | tee "$LOGS/clone.log")
    VM=$(printf '%s\n' "$CLONE_OUT" | grep -E '^qdistro-builder-' | tail -1)
    [ -n "$VM" ] || die "could not parse VM name from clone-baseweed output (see $LOGS/clone.log)"
    log "VM name: $VM"
fi

#-- 3. Attach the build disk -------------------------------------------------
BUILD_DISK="$IMG_DIR/$VM-build.qcow2"
if [ ! -f "$BUILD_DISK" ]; then
    log "creating $BUILD_DISK_GB GiB build disk: $BUILD_DISK"
    qemu-img create -f qcow2 "$BUILD_DISK" "${BUILD_DISK_GB}G" >/dev/null
    log "attaching as vdb (persistent)"
    virsh attach-disk "$VM" "$BUILD_DISK" vdb \
        --config --subdriver qcow2 --targetbus virtio
fi

#-- 4. Bake the description (with sources) into the VM via virt-copy-in -------
log "copying qdistro-image/ into the VM rootfs (offline)"
# Tar first so we copy one stream; virt-copy-in can take a directory
# but for ~50MB it's faster to land a single archive.
TAR="$LOGS/qdistro-image.tar"
tar --exclude='./logs' --exclude='./keys/gnupg' \
    --exclude='./root/root/qdistro-src/qdistro/tests/integration/qdwin-noctalia/.git' \
    -cf "$TAR" -C "$HERE" .

# Wait until VM is shut off; virt-copy-in needs the disk exclusive.
if [ "$(virsh domstate "$VM")" != "shut off" ]; then
    log "shutting down $VM for offline copy-in"
    virsh shutdown "$VM" 2>/dev/null || true
    for i in $(seq 1 60); do
        [ "$(virsh domstate "$VM")" = "shut off" ] && break
        sleep 2
    done
    if [ "$(virsh domstate "$VM")" != "shut off" ]; then
        virsh destroy "$VM" || true
        sleep 2
    fi
fi

virt-customize -a "$IMG_DIR/$VM.qcow2" \
    --copy-in "$TAR:/root" \
    --run-command 'rm -rf /root/qdistro-image && mkdir /root/qdistro-image && tar -xf /root/qdistro-image.tar -C /root/qdistro-image && rm -f /root/qdistro-image.tar' \
    >>"$LOGS/virt-customize.log" 2>&1 \
    || die "virt-customize failed; see $LOGS/virt-customize.log"

#-- 5. Start + wait ------------------------------------------------------------
"$VM_TOOLS/vm-start-and-wait" "$VM" | tee -a "$LOGS/start.log"

#-- 6. Prep build disk inside VM ---------------------------------------------
log "formatting /dev/vdb -> /build (xfs)"
vms <<'EOS' | tee "$LOGS/disk-prep.log"
set -eux
if ! blkid /dev/vdb >/dev/null 2>&1; then
    mkfs.xfs -f /dev/vdb >/dev/null
fi
mkdir -p /build
mountpoint -q /build || mount /dev/vdb /build
df -h /build
EOS

#-- 7. Make zypper abandon dead mirrors ---------------------------------------
# Observed 2026-09-04 (runs 4/5/6): a mirror completes the TCP/TLS handshake and
# then delivers ~0 B/s. libzypp's download.min_download_speed defaults to 0, so
# such a transfer is never abandoned: the build hangs until vm-exec's timeout
# kills it instead of retrying and eventually failing. Guest curl to the same
# URL stayed healthy throughout, so the transfer stalls, not DNS or routing.
# A speed floor plus bounded retries turns an unbounded hang into a few retries
# and then a real error -- the behaviour a flaky uplink should produce.
#
# Written as a zypp.conf.d drop-in, which the vendor zypp.conf asks for; note
# zypp.conf itself ships in /usr/etc here (same usr-etc split as qemu-ga's
# sysconfig), so editing /etc/zypp/zypp.conf would mean copying a vendor file.
# Builder-VM tuning only: it does not touch the image's repo list or sizes.
MIN_MIRROR_BPS="${QDISTRO_MIN_MIRROR_BPS:-20000}"
[[ "$MIN_MIRROR_BPS" =~ ^[0-9]+$ ]] || die "QDISTRO_MIN_MIRROR_BPS must be a non-negative integer, got: $MIN_MIRROR_BPS"
log "setting libzypp mirror timeouts in builder VM (floor ${MIN_MIRROR_BPS} B/s)"
vms <<EOS | tee "$LOGS/zypp-tuning.log"
set -eu
mkdir -p /etc/zypp/zypp.conf.d
cat > /etc/zypp/zypp.conf.d/99-qdistro-mirror.conf <<'CONF'
[main]
# Drop a mirror that stalls below this instead of hanging on it forever.
download.min_download_speed = $MIN_MIRROR_BPS
# NB: the key is transfer_timeout, NOT "timeout" -- libzypp silently ignores
# unknown keys, so a wrong name looks applied but does nothing (seen in run 8).
# Valid names confirmed from libzypp's own symbol table.
download.transfer_timeout = 120
download.connect_timeout = 30
download.max_silent_tries = 5
CONF
cat /etc/zypp/zypp.conf.d/99-qdistro-mirror.conf
EOS

#-- 7b. Install kiwi inside the VM --------------------------------------------
log "installing kiwi-ng inside VM (idempotent zypper)"
# J25: with the default QDISTRO_PROFILE=release this builds the SHIPPED
# image. No --no-gpg-checks and
# no `|| true` — a release image build MUST fail if it cannot refresh verified
# repo metadata, rather than baking packages from an unsigned/tampered mirror.
# J25: this host script runs under `set -euo pipefail`, so a failed (now
# gpg-verified) `zypper refresh` propagates out of `vms` and fails this whole
# `vms | tee` pipeline (pipefail) — the release build stops here rather than
# proceeding to install kiwi from unsigned/tampered metadata.
#
# Retried, because this step is transient-failure-prone in exactly the two ways
# the build loop below already defends against, and until now a single bad draw
# here killed the whole run before the loop got a turn (run 14): a mirror that
# stalls at ~0 B/s (bounded now by the drop-in above, which is why it is written
# BEFORE this refresh -- it used to be written after, so the one command it was
# meant to protect ran unprotected), and a repomd.xml whose signature does not
# match because the mirror was caught mid-update. The latter is what run 14 hit;
# zypper says so itself ("might be a transient issue if the server is in the
# midst of receiving new data"). `zypper clean -m` between attempts discards the
# mismatched metadata so the retry refetches rather than re-reading the bad copy.
# Verification is NOT relaxed: no --no-gpg-checks, and a run that fails every
# attempt still fails the build.
ZYPP_TRIES="${QDISTRO_ZYPP_TRIES:-3}"
[[ "$ZYPP_TRIES" =~ ^[1-9][0-9]*$ ]] || die "QDISTRO_ZYPP_TRIES must be a positive integer, got: $ZYPP_TRIES"
log "  ${ZYPP_TRIES} attempts, gpg verification unchanged"
vms <<EOS | tee "$LOGS/kiwi-install.log"
# pipefail matters: the install is piped through tail, so without it a failed
# zypper would be reported as tail's exit 0 and the retry would never trigger.
set -uo pipefail
rc=1
for attempt in \$(seq 1 $ZYPP_TRIES); do
    echo "[zypp] attempt \$attempt/$ZYPP_TRIES"
    zypper -n refresh \
      && zypper -n install --no-recommends python3-kiwi kiwi-systemdeps 2>&1 | tail -10
    rc=\$?
    if [ "\$rc" = 0 ]; then break; fi
    echo "[zypp] attempt \$attempt failed (rc=\$rc); dropping cached metadata"
    zypper clean -m >/dev/null 2>&1 || true
    sleep 20
done
echo "[zypp] final rc=\$rc"
exit \$rc
EOS

#-- 8. Run the kiwi build ------------------------------------------------------
log "running kiwi-ng build inside VM (17-26 min measured 2026-09-04, n=2)"
log "  tail with: $VM_TOOLS/vm-exec $VM 'tail -f /root/kiwi-build.log'"
# Use --no-sync because sources are already in root/root/qdistro-src/.
# Redirect inside the VM so qga doesn't have to ferry GB of output.
set +e
log "  building with QDISTRO_PROFILE=$QDISTRO_PROFILE"
# Retry on stall. Measured 2026-09-04: fetching repo metadata / the repo gpg
# key from download.opensuse.org hangs outright on roughly half of attempts on
# a flaky uplink -- an A/B of 6 runs failed 1/3 at 1 connection and 1/3 at 5,
# so it is neither concurrency nor one bad mirror (blackholing the first
# offender just moved the hang to the next). libzypp's download.* timeouts do
# not bound the gpg-key fetch, so the hang is unbounded: without this the build
# sits until vm-exec's timeout kills it, ~30 min per lost attempt.
#
# So bound it here: watch the build and treat a genuine stall as failure.
#
# "Silence == hung" is NOT a safe test, and run 16 proved it: attempts 2 and 3
# were both killed at the identical step, kiwi xz-compressing the 20 GiB raw
# into the install-ISO squashfs (mksquashfs -comp xz), which emits nothing for
# many minutes while working perfectly (5m03s in run 17). So an attempt counts
# as alive when ANY of these moved in the last sample (image/lib/build-guard.sh):
#   * the build log's mtime            -- kiwi printed something
#   * CPU ticks across the build's process tree -- mksquashfs, rpm, xz
#   * a process in the tree in state D -- blocked in kernel I/O: a 20 GiB
#     unmount/sync or mkfs burns no ticks of its own and prints nothing
#   * bytes received on the VM's uplink -- a slow-but-alive download; the
#     libzypp floor above deliberately tolerates ~20 kB/s, so the guard must
#     tolerate it too, or the two would encode opposite policies
# Stalled therefore means none of those for KIWI_STALL_S, which a dead
# transfer at 0 B/s satisfies and a working build should not. It is still a
# heuristic: a wedge that keeps logging or spinning is not caught here and
# costs one attempt budget, which the per-attempt cap bounds. Each attempt
# wipes /build/out -- kiwi refuses a non-empty target dir, and reusing a root
# killed mid-bootstrap risks a corrupt tree. kiwi's package cache lives at
# /var/cache/kiwi on the VM's root disk, not under /build/out, so it survives
# between attempts (which is why run 15's attempt 3 rebuilt in minutes).
KIWI_STALL_S="${QDISTRO_KIWI_STALL_S:-240}"
KIWI_TRIES="${QDISTRO_KIWI_TRIES:-3}"
# A healthy build is 17-26 min, so an attempt that is producing output but has
# run well past that is wedged in a way the stall guard cannot see; cap it too.
# Run 16: a cold-cache attempt was still partitioning at 1500s, so 1500 was too
# tight and killed a working build. The budget is a backstop against a wedge the
# stall guard cannot see, not a performance expectation -- keep it generous.
KIWI_ATTEMPT_BUDGET_S="${QDISTRO_KIWI_ATTEMPT_BUDGET_S:-2700}"
# Minimum CPU ticks (100/s per core) the build tree must burn in a sample for it
# to count as alive. 100 = 1 CPU-second per 15s poll, ~7% of one core: far below
# mksquashfs or rpm, far above a stalled socket.
KIWI_CPU_TICKS_MIN="${QDISTRO_KIWI_CPU_TICKS_MIN:-100}"
# Sample period. 15 s is the calibration for the floors below (they are
# scaled by it); a fault-injection run polls faster to hit a short phase.
KIWI_POLL_S="${QDISTRO_KIWI_POLL_S:-15}"
# Minimum bytes received per sample to count as a live download: the same
# floor libzypp is told to accept (MIN_MIRROR_BPS), over the sample period.
KIWI_RX_BYTES_MIN=$(( MIN_MIRROR_BPS * KIWI_POLL_S ))
# Fault injection for the retry path itself: kill attempt 1 after this many
# seconds (0 = off). The retry/cleanup path is the feature and a green
# attempt-1 build never exercises it, so a review run sets this to land the
# kill in a phase that holds mounts and a loop device, and the log must then
# show a clean attempt 2. Never set in a real build.
KIWI_FAULT_KILL_AT_S="${QDISTRO_KIWI_FAULT_KILL_AT_S:-0}"
# Same, keyed on a build-log line instead of a clock (empty = off): lands the
# kill deterministically in a chosen phase, e.g. "Syncing root filesystem
# data" while the raw's loop device and kiwi's /var/tmp/kiwi_volumes.* mounts
# are all live -- the state the cleanup exists for.
KIWI_FAULT_KILL_ON_LOG="${QDISTRO_KIWI_FAULT_KILL_ON_LOG:-}"
# Every knob feeds shell arithmetic or a comparison; a typo must fail here, not
# turn into an arithmetic error mid-build or a silently shortened host cap
# (the exact bug the derived cap below fixes). Same gate as QDISTRO_PROFILE.
for knob in KIWI_STALL_S KIWI_TRIES KIWI_ATTEMPT_BUDGET_S KIWI_CPU_TICKS_MIN KIWI_FAULT_KILL_AT_S KIWI_POLL_S; do
    [[ "${!knob}" =~ ^[0-9]+$ ]] || die "$knob must be a non-negative integer, got: ${!knob}"
done
[ "$KIWI_TRIES" -ge 1 ] || die "KIWI_TRIES must be >= 1"
[ "$KIWI_POLL_S" -ge 1 ] || die "KIWI_POLL_S must be >= 1"
# The CPU floor is calibrated per 15 s; scale it to the poll period.
KIWI_CPU_TICKS_MIN=$(( KIWI_CPU_TICKS_MIN * KIWI_POLL_S / 15 ))
[ "$KIWI_CPU_TICKS_MIN" -ge 1 ] || KIWI_CPU_TICKS_MIN=1
# The pattern is interpolated into the guest script by the host, so restrict
# it to a character class that cannot break out of either quoting context.
[[ "$KIWI_FAULT_KILL_ON_LOG" =~ ^[[:alnum:]\ _.:/=-]*$ ]] || die "QDISTRO_KIWI_FAULT_KILL_ON_LOG may only contain [A-Za-z0-9 _.:/=-], got: $KIWI_FAULT_KILL_ON_LOG"
# Fault injection is a dev-profile instrument; a release build refuses it so
# a leaked knob cannot silently cost a production build an attempt.
if { [ "$KIWI_FAULT_KILL_AT_S" != 0 ] || [ -n "$KIWI_FAULT_KILL_ON_LOG" ]; } && [ "$QDISTRO_PROFILE" != dev ]; then
    die "fault injection (QDISTRO_KIWI_FAULT_KILL_*) is only allowed with QDISTRO_PROFILE=dev"
fi
# vm-exec caps a guest command at QDISTRO_VM_EXEC_TIMEOUT, default 1800s. That
# default silently made the retry loop a lie: three ~16 min attempts cannot fit
# in 30 min, so only the first ever had room. Run 15 died exactly here -- two
# attempts stalled, the third had written the full 20 GiB raw and was in kiwi's
# final rpm verification when the HOST clock killed it at 1800s, and the run was
# reported as a kiwi failure (exit 124) rather than as the driver's own cap.
# So derive the host cap from the retry budget instead of leaving it defaulted:
# attempts * budget, plus the inter-attempt sleeps, kill waits and poll slack.
KIWI_EXEC_TIMEOUT=$(( KIWI_TRIES * (KIWI_ATTEMPT_BUDGET_S + 60) + 300 ))
log "  stall guard: abort+retry after ${KIWI_STALL_S}s with no log/CPU/D-state/rx activity, ${KIWI_TRIES} attempts"
log "  per-attempt cap ${KIWI_ATTEMPT_BUDGET_S}s; host vm-exec cap ${KIWI_EXEC_TIMEOUT}s"
log "  liveness floors: >=${KIWI_CPU_TICKS_MIN} ticks or >=${KIWI_RX_BYTES_MIN} rx bytes per ${KIWI_POLL_S}s sample"
if [ "$KIWI_FAULT_KILL_AT_S" != 0 ]; then
    warn "FAULT INJECTION: attempt 1 will be killed at ${KIWI_FAULT_KILL_AT_S}s"
fi
if [ -n "$KIWI_FAULT_KILL_ON_LOG" ]; then
    warn "FAULT INJECTION: attempt 1 will be killed once the log contains: $KIWI_FAULT_KILL_ON_LOG"
fi
export QDISTRO_VM_EXEC_TIMEOUT="$KIWI_EXEC_TIMEOUT"
vms <<EOS | tee "$LOGS/kiwi-driver.log"
cd /root/qdistro-image || exit 1
# Keep this script's stdout tiny: guest-exec ferries it through the qemu agent
# and libvirt caps an agent response at ~10 MiB (QEMU_AGENT_MAX_RESPONSE),
# dropping the agent connection when it overflows -- run 12 died with the
# agent wedged for the rest of the run after a chatty attempt. Everything goes
# to a file; only a bounded tail is emitted at the end.
exec 3>&1                      # keep the real stdout for the bounded tail
exec >/root/kiwi-loop.log 2>&1
set -u
. /root/qdistro-image/lib/build-guard.sh

# TMPDIR is intentionally NOT redirected to /build/tmp: dracut runs inside
# the image-root chroot and won't see anything mounted under /build there.
# QDISTRO_PROFILE is forwarded so config.sh's sudoers/profile gate matches the
# host invocation; kiwi inherits it into config.sh's environment.
rc=1
# Per-attempt logs of a PREVIOUS run on a reused VM would otherwise be
# fetched as this run's evidence (run 18b shipped run 18's attempt 2/3 logs).
rm -f /root/kiwi-build.attempt*.log
for attempt in \$(seq 1 $KIWI_TRIES); do
    echo "[kiwi] attempt \$attempt/$KIWI_TRIES"
    # kiwi refuses a non-empty --target-dir, and a killed attempt leaves one
    # behind with its bind mounts, /var/tmp/kiwi_* mounts and loop device
    # still live (run 13: rm -rf died with "Device or resource busy" and every
    # later attempt failed before it started). Release all of it and VERIFY
    # the release; a leftover fails this attempt rather than being papered
    # over with a fresh mkdir for kiwi to trip on later.
    if ! guard_cleanup_target /build/out; then
        echo "[kiwi] attempt \$attempt: previous attempt's resources could not be released - giving up"
        rc=1
        break
    fi
    mkdir -p /build/out
    : > /root/kiwi-build.log
    started=\$(date +%s)
    # setsid: the build leads its own process group, so the group can be
    # killed as a whole without touching this shell (the guest agent's child).
    env QDISTRO_PROFILE=$QDISTRO_PROFILE QDISTRO_BUILD_DIR=/build/out \\
        setsid bash build.sh --no-sync >/root/kiwi-build.log 2>&1 &
    kpid=\$!
    last_active=\$started
    prev_cpu=0
    prev_mtime=0
    prev_rx=\$(guard_rx_bytes)
    reason=""
    while kill -0 \$kpid 2>/dev/null; do
        sleep $KIWI_POLL_S
        now=\$(date +%s)
        mtime=\$(stat -c %Y /root/kiwi-build.log 2>/dev/null || echo 0)
        cpu=\$(guard_tree_cpu \$kpid)
        dst=\$(guard_tree_dstate \$kpid)
        rx=\$(guard_rx_bytes)
        why=""
        [ "\$mtime" != "\$prev_mtime" ] && why="log"
        [ \$(( cpu - prev_cpu )) -ge $KIWI_CPU_TICKS_MIN ] && why="\$why cpu=\$(( cpu - prev_cpu ))"
        [ "\$dst" -gt 0 ] && why="\$why dstate=\$dst"
        [ \$(( rx - prev_rx )) -ge $KIWI_RX_BYTES_MIN ] && why="\$why rx=\$(( rx - prev_rx ))"
        [ -n "\$why" ] && last_active=\$now
        prev_mtime=\$mtime
        prev_cpu=\$cpu
        prev_rx=\$rx
        idle=\$(( now - last_active ))
        # One line per sample so a kill can be explained afterwards.
        echo "[kiwi] t=\$(( now - started ))s idle=\${idle}s alive:\${why:- none}"
        if [ "\$idle" -ge $KIWI_STALL_S ]; then
            reason="STALLED \${idle}s: no log output, CPU, D-state or rx activity"
        elif [ \$(( now - started )) -ge $KIWI_ATTEMPT_BUDGET_S ]; then
            reason="exceeded the ${KIWI_ATTEMPT_BUDGET_S}s attempt budget (a wedge the stall guard cannot see)"
        elif [ "\$attempt" = 1 ] && [ $KIWI_FAULT_KILL_AT_S -gt 0 ] && [ \$(( now - started )) -ge $KIWI_FAULT_KILL_AT_S ]; then
            reason="FAULT INJECTION at ${KIWI_FAULT_KILL_AT_S}s (QDISTRO_KIWI_FAULT_KILL_AT_S)"
        elif [ "\$attempt" = 1 ] && [ -n '$KIWI_FAULT_KILL_ON_LOG' ] && grep -qF -- '$KIWI_FAULT_KILL_ON_LOG' /root/kiwi-build.log; then
            reason="FAULT INJECTION on log line '$KIWI_FAULT_KILL_ON_LOG' (QDISTRO_KIWI_FAULT_KILL_ON_LOG)"
            echo "[kiwi] mounts under /build/out + /var/tmp/kiwi_* at kill time: \$(guard_mounts_under /build/out | wc -l)+\$(guard_mounts_under /var/tmp | grep -c '^/var/tmp/kiwi_'); loops backed by /build/out: \$(guard_loops_under /build/out | tr '\n' ' ')"
        fi
        if [ -n "\$reason" ]; then
            echo "[kiwi] \$reason - aborting attempt \$attempt"
            if guard_kill_tree \$kpid 60; then
                echo "[kiwi] attempt \$attempt: build tree killed"
            else
                echo "[kiwi] attempt \$attempt: build tree NOT fully dead after 60s (D-state?)"
            fi
            break
        fi
    done
    wait \$kpid 2>/dev/null; rc=\$?
    if [ "\$rc" = 0 ]; then echo "[kiwi] attempt \$attempt succeeded"; break; fi
    echo "[kiwi] attempt \$attempt failed (rc=\$rc) after \$(( \$(date +%s) - started ))s"
    # Each attempt truncates kiwi-build.log, so without this the only surviving
    # build log is the last attempt's and a stall cannot be located afterwards.
    cp /root/kiwi-build.log /root/kiwi-build.attempt\$attempt.log 2>/dev/null || true
    # Counted BEFORE the next attempt's cleanup, so non-zero here is expected
    # after a kill; the cleanup's own verification is what gates reuse.
    echo "[kiwi] state after attempt \$attempt (pre-cleanup): mounts=\$(guard_mounts_under /build/out | wc -l)+\$(guard_mounts_under /var/tmp | grep -c '^/var/tmp/kiwi_') loops=\$(guard_loops_under /build/out | wc -l)"
    sleep 10
done
echo "[kiwi] final rc=\$rc"
tail -c 4000 /root/kiwi-loop.log >&3
exit \$rc
EOS
KIWI_RC=${PIPESTATUS[0]}
set -e
# Back to vm-exec's default for the short steps that follow; the long cap was
# only ever meant to cover the build itself.
unset QDISTRO_VM_EXEC_TIMEOUT
log "kiwi exit code: $KIWI_RC"

log "fetching kiwi-build.log to host"
vmx 'tail -100 /root/kiwi-build.log' > "$LOGS/kiwi-build.tail.log" 2>&1 || true
virt-cat -a "$IMG_DIR/$VM.qcow2" /root/kiwi-build.log > "$LOGS/kiwi-build.full.log" 2>&1 \
    || vmx 'cat /root/kiwi-build.log' > "$LOGS/kiwi-build.full.log" 2>&1 || true
# The loop log (one liveness line per 15 s sample, every kill with its reason)
# and any per-attempt logs of failed attempts: the evidence for a retry.
vmx 'cat /root/kiwi-loop.log' > "$LOGS/kiwi-loop.log" 2>&1 || true
for n in $(seq 1 "$KIWI_TRIES"); do
    vmx "cat /root/kiwi-build.attempt$n.log 2>/dev/null" > "$LOGS/kiwi-build.attempt$n.log" 2>/dev/null || true
    [ -s "$LOGS/kiwi-build.attempt$n.log" ] || rm -f "$LOGS/kiwi-build.attempt$n.log"
done

if [ "$KIWI_RC" != "0" ]; then
    warn "kiwi build failed (exit $KIWI_RC); see $LOGS/kiwi-build.full.log"
    warn "VM left running so you can inspect: virsh -c $URI console $VM"
    exit 1
fi

#-- 9. Inventory the artifacts inside the VM ----------------------------------
log "build artifacts in VM:"
vmx 'ls -lh /build/out/' | tee "$LOGS/artifacts.txt"
# Exact byte sizes, so the copy-out below can prove it copied THIS build.
vmx 'cd /build/out && stat -c "%s %n" *.raw *.install.iso *.packages *.changes *.verified 2>/dev/null' \
    > "$LOGS/artifact-sizes.txt" 2>/dev/null || true
IN_VM_RAW_SIZE="$(awk '$2 ~ /\.raw$/ { print $1; exit }' "$LOGS/artifact-sizes.txt")"
IN_VM_ISO_SIZE="$(awk '$2 ~ /\.install\.iso$/ { print $1; exit }' "$LOGS/artifact-sizes.txt")"
[ -n "$IN_VM_RAW_SIZE" ] || die "no .raw in /build/out despite kiwi exit 0 (see $LOGS/artifacts.txt)"

#-- 10. Copy artifacts back to host ------------------------------------------
log "copying artifacts back to host ($HOST_BUILD_DIR)"
# Get the list of files in /build/out/ and pull each via virt-copy-out
# (needs the VM stopped) — actually faster to scp via slirp? No — slirp
# has no inbound from host. Use virt-copy-out after a clean shutdown.
log "shutting down VM for offline copy-out"
virsh shutdown "$VM" 2>/dev/null || true
for _ in $(seq 1 90); do
    [ "$(virsh domstate "$VM")" = "shut off" ] && break
    sleep 2
done
if [ "$(virsh domstate "$VM")" != "shut off" ]; then
    virsh destroy "$VM" || true
fi

# The build disk is a bare xfs filesystem on /dev/sda (no partition table), so
# libguestfs auto-inspection finds no OS; mount /dev/sda explicitly.
# Use guestfish's synchronous copy-out, NOT guestmount: the FUSE mount is
# unreliable under rootless qemu:///session here (returns success but exposes an
# empty tree). Force the direct (appliance) backend — the default libvirt
# backend can't boot the appliance in this nested/rootless setup.
mkdir -p "$HOST_BUILD_DIR"
export LIBGUESTFS_BACKEND="${LIBGUESTFS_BACKEND:-direct}"
# qemu can keep flushing the build qcow2 for several seconds after libvirt
# reports the domain "shut off", so a too-early read sees an empty out/.
# Retry until the artifacts have settled on the host side.
# Copy only the artifact files (not /out/build/, the multi-GB extracted
# image-root tree kiwi leaves behind).
# $HOST_BUILD_DIR is under /var/tmp and persists across runs, so "a .raw
# exists" would be satisfied by the PREVIOUS build if this copy-out copied
# nothing -- and verify.sh would then verify the wrong image. Clear the old
# artifacts first and accept only a .raw whose size matches the one kiwi just
# wrote inside the VM.
rm -f "$HOST_BUILD_DIR"/*.raw "$HOST_BUILD_DIR"/*.install.iso "$HOST_BUILD_DIR"/*.packages \
      "$HOST_BUILD_DIR"/*.changes "$HOST_BUILD_DIR"/*.verified
copied=0
for attempt in $(seq 1 6); do
    guestfish --ro -a "$BUILD_DISK" -m /dev/sda <<EOF 2>>"$LOGS/copy-out.log"
glob copy-out /out/*.raw $HOST_BUILD_DIR/
glob copy-out /out/*.install.iso $HOST_BUILD_DIR/
glob copy-out /out/*.packages $HOST_BUILD_DIR/
glob copy-out /out/*.changes $HOST_BUILD_DIR/
glob copy-out /out/*.verified $HOST_BUILD_DIR/
EOF
    host_raw="$(ls "$HOST_BUILD_DIR"/*.raw 2>/dev/null | head -1)"
    host_iso="$(ls "$HOST_BUILD_DIR"/*.install.iso 2>/dev/null | head -1)"
    iso_ok=1
    if [ -n "$IN_VM_ISO_SIZE" ]; then
        [ -n "$host_iso" ] && [ "$(stat -c %s "$host_iso")" = "$IN_VM_ISO_SIZE" ] || iso_ok=0
    fi
    if [ -n "$host_raw" ] && [ "$(stat -c %s "$host_raw")" = "$IN_VM_RAW_SIZE" ] && [ "$iso_ok" = 1 ]; then copied=1; break; fi
    log "copy-out: artifacts not settled yet (attempt $attempt/6; want $IN_VM_RAW_SIZE bytes); waiting..."
    sleep 5
done
[ "$copied" = 1 ] || die "copy-out: no .raw of $IN_VM_RAW_SIZE bytes appeared (see $LOGS/copy-out.log)"

log "artifacts on host:"
ls -lh "$HOST_BUILD_DIR/" | tee -a "$LOGS/artifacts.txt"

if [ "$KEEP_RUNNING" = 1 ]; then
    virsh start "$VM"
fi

log "DONE. logs: $LOGS"
log "next: ./verify.sh"
