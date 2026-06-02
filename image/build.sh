#!/bin/bash
# Build the qdistro OEM disk image with kiwi-ng.
#
# Output: $BUILD_DIR/qdistro.x86_64-0.1.0.install.iso
#         (an install medium you `dd` to USB; on first boot it lays the
#          system onto the target machine's internal disk, then auto-
#          reboots into the installed copy)
#
# Requirements (Tumbleweed dev host):
#   sudo zypper in python3-kiwi kiwi-systemdeps qemu-x86 qemu-tools
#
# Usage:
#   sudo ./build.sh                  # full build (rsyncs sources, runs kiwi)
#   sudo ./build.sh --clean          # nuke build dir first
#   ./build.sh --test                # boot the resulting image in qemu
#   ./build.sh --sync-only           # only refresh root/opt/qdistro-src

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SIBLINGS="$(cd "$HERE/.." && pwd)"
BUILD_DIR="${QDISTRO_BUILD_DIR:-/tmp/qdistro-build}"
SRC_OVERLAY="$HERE/root/root/qdistro-src"  # ends up at /root/qdistro-src in image

sync_sources() {
    install -d -m 0755 "$SRC_OVERLAY"
    # qdgreeter + qdlocker are part of the production boot/session path:
    # greetd execs /usr/bin/qdgreeter (greetd-config.toml) and the
    # qdwin-session.target Wants= qdlocker.service. config.sh pip-installs
    # both from this overlay, so they must be synced in (findings #16, #19).
    for repo in qdistro qdwin qdshell qdgreeter qdlocker; do
        if [ ! -d "$SIBLINGS/$repo" ]; then
            echo "[build] ERROR: $SIBLINGS/$repo not found (sibling repo missing)" >&2
            exit 2
        fi
        echo "[build] rsyncing $repo -> $SRC_OVERLAY/$repo"
        rsync -a --delete \
              --exclude=.git \
              --exclude=__pycache__ \
              --exclude='*.pyc' \
              --exclude=build \
              --exclude=node_modules \
              "$SIBLINGS/$repo/" "$SRC_OVERLAY/$repo/"
    done
    echo "[build] source overlay sizes:"
    du -sh "$SRC_OVERLAY"/* 2>&1 | sed 's/^/  /'
}

case "${1:-}" in
  --clean)
    rm -rf "$BUILD_DIR"
    rm -rf "$HERE/root/root"
    sync_sources
    ;;
  --sync-only)
    sync_sources
    exit 0
    ;;
  --no-sync)
    # Sources are already present (e.g. baked into a VM by build-in-vm.sh).
    ;;
  --test)
    img=$(find "$BUILD_DIR" -name '*.raw' 2>/dev/null | head -1)
    [ -n "$img" ] || { echo "no .raw image in $BUILD_DIR; run a build first" >&2; exit 1; }
    OVMF=""
    for c in /usr/share/qemu/ovmf-x86_64.bin /usr/share/qemu/ovmf-x86_64-code.bin /usr/share/OVMF/OVMF_CODE.fd; do
        [ -f "$c" ] && OVMF="$c" && break
    done
    [ -n "$OVMF" ] || { echo "no OVMF firmware found" >&2; exit 1; }
    exec qemu-system-x86_64 -enable-kvm -m 4096 -smp 2 \
      -bios "$OVMF" \
      -drive file="$img",format=raw,if=virtio \
      -netdev user,id=n0 -device virtio-net,netdev=n0
    ;;
  "")
    sync_sources
    ;;
  *)
    echo "unknown arg: $1" >&2
    exit 2
    ;;
esac

if [ "$EUID" -ne 0 ]; then
  echo "[build] build must run as root; re-exec with sudo" >&2
  exec sudo -E "$0" "$@"
fi

mkdir -p "$BUILD_DIR"

echo "[build] kiwi-ng building OEM image -> $BUILD_DIR"
mkdir -p "$HERE/logs"
kiwi-ng --debug \
  --type oem \
  system build \
    --description "$HERE" \
    --target-dir "$BUILD_DIR" \
  2>&1 | tee "$HERE/logs/kiwi-build.log"

echo
echo "[build] artifacts:"
ls -lh "$BUILD_DIR" | grep -E '\.(raw|iso|qcow2|xz)$' || true
