#!/bin/bash
# task 4 piece 1 — build the qdistro-netvm OpenWrt guest image.
#
# Unlike the other qdistro VMs (Tumbleweed via virt-customize), the net VM is
# OpenWrt (doc/networking.md §"VM definition exception"): its whole value is the
# integrated UCI/fw4/hostapd/ubus stack. So the image is built with the OpenWrt
# **ImageBuilder** — a reproducible, declarative pipeline: a fixed release, an
# explicit PACKAGES set, and a host-generated FILES overlay (image-files/) laid
# over the read-only rootfs. Re-running with the same inputs yields the same
# image; the overlay is regenerated, never hand-patched in place.
#
# What the overlay (image-files/) carries:
#   - usr/libexec/rpcd/qdistro.netvm        the control-plane ubus object
#   - usr/libexec/qdistro-netvm-apply       the egress-config regenerator
#   - usr/share/rpcd/acl.d/qdistro-netvm.json the scoped admin ACL
#   - etc/qdistro-netvm/baseline/*          baseline network/firewall/dhcp
#   - etc/uci-defaults/99-qdistro-netvm     first-boot: uhttpd /ubus + rpcd login
#
# Lineage (doc/lineage.md / vm-definitions.md): the script records the release,
# package list, FILES manifest, and output sha256 next to the image.
#
# Run on the host (no root needed if --dest is user-writable):
#   ./build-netvm-image.sh --dest ~/.local/share/libvirt/images/qdistro-netvm-base.qcow2
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
FILES_DIR="$HERE/image-files"

RELEASE="${QDISTRO_NETVM_OPENWRT_RELEASE:-24.10.7}"
TARGET="${QDISTRO_NETVM_OPENWRT_TARGET:-x86/64}"
DEST="${QDISTRO_NETVM_DEST:-/var/lib/libvirt/images/qdistro-netvm-base.qcow2}"
CACHE_DIR="${QDISTRO_NETVM_CACHE:-$HOME/.cache/qdistro}"
FORCE=0

# The package set Probe 2 proved present in the stock 24.10 x86_64 feed plus
# the control-plane (uhttpd-mod-ubus + rpcd-mod-ucode) and tunnel stack.
PACKAGES="${QDISTRO_NETVM_PACKAGES:-\
uhttpd uhttpd-mod-ubus rpcd rpcd-mod-file rpcd-mod-iwinfo \
wireguard-tools kmod-wireguard ip-full \
dnsmasq-full firewall4 nftables kmod-nft-core \
luci-ssl-openssl}"
# dnsmasq-full replaces the default dnsmasq for the multi-instance / ipset
# features the per-silo resolvers use; it conflicts with `dnsmasq`, hence the
# explicit "-dnsmasq" removal below.

while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help) sed -n '2,28p' "$0"; exit 0;;
        --force)   FORCE=1; shift;;
        --dest)    shift; DEST="${1:?--dest requires PATH}"; shift;;
        --release) shift; RELEASE="${1:?}"; shift;;
        --cache)   shift; CACHE_DIR="${1:?}"; shift;;
        *) echo "unknown arg: $1" >&2; exit 2;;
    esac
done

for tool in wget tar make qemu-img sha256sum; do
    command -v "$tool" >/dev/null || { echo "[netvm-build] missing: $tool" >&2; exit 2; }
done
[ -d "$FILES_DIR" ] || { echo "[netvm-build] missing overlay dir $FILES_DIR" >&2; exit 2; }

if [ -f "$DEST" ] && [ "$FORCE" != 1 ]; then
    echo "[netvm-build] $DEST exists; --force to rebuild" >&2; exit 0
fi

# zstd-compressed ImageBuilder tarball (24.10 uses .tar.zst).
TARGET_PATH="${TARGET//\//-}"          # x86/64 -> x86-64
IB_NAME="openwrt-imagebuilder-${RELEASE}-${TARGET_PATH}.Linux-x86_64"
IB_URL="https://downloads.openwrt.org/releases/${RELEASE}/targets/${TARGET}/${IB_NAME}.tar.zst"

install -d -m 0755 "$CACHE_DIR"
IB_TARBALL="$CACHE_DIR/${IB_NAME}.tar.zst"
if [ ! -f "$IB_TARBALL" ]; then
    echo "[netvm-build] fetching ImageBuilder: $IB_URL"
    wget -q --show-progress -O "$IB_TARBALL.partial" "$IB_URL"
    mv "$IB_TARBALL.partial" "$IB_TARBALL"
fi

WORK="$(mktemp -d /tmp/netvm-build.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT
echo "[netvm-build] extracting ImageBuilder..."
tar --zstd -xf "$IB_TARBALL" -C "$WORK"
IB_DIR="$WORK/$IB_NAME"

# Stage the FILES overlay (copy so we can fix perms/ownership without touching
# the tracked tree). rpcd plugins + helpers must be executable.
STAGE="$WORK/files"
cp -a "$FILES_DIR" "$STAGE"
chmod 0755 "$STAGE/usr/libexec/rpcd/qdistro.netvm" \
           "$STAGE/usr/libexec/qdistro-netvm-apply"
find "$STAGE/etc/uci-defaults" -type f -exec chmod 0755 {} + 2>/dev/null || true

echo "[netvm-build] building image (release $RELEASE, target $TARGET)..."
make -C "$IB_DIR" image \
    PROFILE=generic \
    PACKAGES="$PACKAGES -dnsmasq" \
    FILES="$STAGE" \
    DISABLED_SERVICES="" \
    >/dev/null

# The combined EFI/squashfs image is the bootable one.
SRC_IMG="$(ls "$IB_DIR"/bin/targets/"$TARGET"/*generic-squashfs-combined-efi.img.gz 2>/dev/null \
           | head -n1 || true)"
[ -n "$SRC_IMG" ] || SRC_IMG="$(ls "$IB_DIR"/bin/targets/"$TARGET"/*generic-squashfs-combined.img.gz | head -n1)"
[ -n "$SRC_IMG" ] || { echo "[netvm-build] no combined image produced" >&2; exit 4; }

RAW="$WORK/netvm.img"
gunzip -c "$SRC_IMG" > "$RAW"
# OpenWrt's squashfs rootfs_data overlay is FIXED-SIZE; growing the raw disk
# does not enlarge it (Probe 2 step 6). The package set above fits the default
# overlay; if it ever overflows, switch the profile to an ext4 image + growpart.
install -d "$(dirname "$DEST")"
qemu-img convert -f raw -O qcow2 "$RAW" "$DEST"
chmod 0644 "$DEST"

# Lineage sidecar.
SHA="$(sha256sum "$DEST" | awk '{print $1}')"
cat >"${DEST%.qcow2}.manifest.txt" <<EOF
qdistro-netvm image lineage
release: openwrt-$RELEASE  target: $TARGET  profile: generic
packages: $PACKAGES -dnsmasq
files-overlay: $(cd "$FILES_DIR" && find . -type f | sort | tr '\n' ' ')
source-image: $(basename "$SRC_IMG")
output: $DEST
sha256: $SHA
EOF

echo "[netvm-build] done: $DEST"
echo "[netvm-build] sha256: $SHA"
ls -lh "$DEST"
