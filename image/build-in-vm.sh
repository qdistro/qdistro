#!/bin/bash
# build-in-vm.sh — drive the kiwi OEM build inside a libvirt VM
# (qemu:///session, rootless on the host) so we don't need sudo on
# the host. The VM cloned from baseweed.qcow2 has root, sudoers,
# all the qdistro deps already, and network via SLIRP.
#
# What this does:
#   1. Clones baseweed -> qdistro-builder-<ts> via qdistro/scripts/vm/.
#   2. Attaches a fresh 60 GiB qcow2 to host the kiwi workspace
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
QDISTRO="$(cd "$HERE/../qdistro" && pwd)"
VM_TOOLS="$QDISTRO/scripts/vm"
IMG_DIR="${QDWIN_IMG_DIR:-$HOME/.local/share/libvirt/images}"
URI="qemu:///session"
export LIBVIRT_DEFAULT_URI="$URI"

VM="${QDISTRO_BUILDER_VM:-qdistro-builder-$(date +%y%m%d-%H%M)}"
BUILD_DISK_GB="${QDISTRO_BUILD_DISK_GB:-60}"
HOST_BUILD_DIR="${QDISTRO_BUILD_DIR:-/tmp/qdistro-build}"
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
[ -d "$HERE/root/root/qdistro-src" ] || die "sources not in overlay; run: ./build.sh --sync-only"
[ -f "$IMG_DIR/baseweed.qcow2" ]     || die "$IMG_DIR/baseweed.qcow2 missing"
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

#-- 3. Attach a fresh 60 GiB build disk ---------------------------------------
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

#-- 7. Install kiwi inside the VM ---------------------------------------------
log "installing kiwi-ng inside VM (idempotent zypper)"
vms <<'EOS' | tee "$LOGS/kiwi-install.log"
set -eu
zypper -n --no-gpg-checks refresh >/dev/null 2>&1 || true
zypper -n install --no-recommends python3-kiwi kiwi-systemdeps 2>&1 | tail -10
EOS

#-- 8. Run the kiwi build ------------------------------------------------------
log "running kiwi-ng build inside VM (this will take 30-60 min)"
log "  tail with: $VM_TOOLS/vm-exec $VM 'tail -f /root/kiwi-build.log'"
# Use --no-sync because sources are already in root/root/qdistro-src/.
# Redirect inside the VM so qga doesn't have to ferry GB of output.
set +e
vms <<'EOS' | tee "$LOGS/kiwi-driver.log"
cd /root/qdistro-image
# TMPDIR is intentionally NOT redirected to /build/tmp: dracut runs inside
# the image-root chroot and won't see anything mounted under /build there.
QDISTRO_BUILD_DIR=/build/out bash build.sh --no-sync \
    >/root/kiwi-build.log 2>&1
EOS
KIWI_RC=${PIPESTATUS[0]}
set -e
log "kiwi exit code: $KIWI_RC"

log "fetching kiwi-build.log to host"
vmx 'tail -100 /root/kiwi-build.log' > "$LOGS/kiwi-build.tail.log" 2>&1 || true
virt-cat -a "$IMG_DIR/$VM.qcow2" /root/kiwi-build.log > "$LOGS/kiwi-build.full.log" 2>&1 \
    || vmx 'cat /root/kiwi-build.log' > "$LOGS/kiwi-build.full.log" 2>&1 || true

if [ "$KIWI_RC" != "0" ]; then
    warn "kiwi build failed (exit $KIWI_RC); see $LOGS/kiwi-build.full.log"
    warn "VM left running so you can inspect: virsh -c $URI console $VM"
    exit 1
fi

#-- 9. Inventory the artifacts inside the VM ----------------------------------
log "build artifacts in VM:"
vmx 'ls -lh /build/out/' | tee "$LOGS/artifacts.txt"

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

# The build disk is a bare xfs filesystem on /dev/sda (no partition table),
# so libguestfs auto-inspection finds no OS and refuses; mount /dev/sda
# explicitly via guestfish (-m /dev/sda /). Trap unmount in case copy fails.
mkdir -p "$HOST_BUILD_DIR" "$LOGS/guestmnt"
trap 'guestunmount "$LOGS/guestmnt" 2>/dev/null || true' EXIT
guestmount -a "$BUILD_DISK" -m /dev/sda --ro "$LOGS/guestmnt" 2>>"$LOGS/copy-out.log"
cp -av "$LOGS/guestmnt/out/"* "$HOST_BUILD_DIR/" 2>&1 | tail -10
guestunmount "$LOGS/guestmnt"
trap - EXIT

log "artifacts on host:"
ls -lh "$HOST_BUILD_DIR/" | tee -a "$LOGS/artifacts.txt"

if [ "$KEEP_RUNNING" = 1 ]; then
    virsh start "$VM"
fi

log "DONE. logs: $LOGS"
log "next: ./verify.sh"
