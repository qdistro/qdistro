#!/usr/bin/env bash
# build-custom-iso.sh — repack the stock Agama installer ISO into a
# qdistro-branded installer with the qdistro product embedded (no DUD
# needed). Prototype path; see packaging/README.md for the kiwi/OBS option.
#
# Usage: build-custom-iso.sh <out.iso>
# Inputs come from packaging/env.sh:
#   AGAMA_STOCK_ISO / AGAMA_STOCK_ISO_URL / AGAMA_STOCK_ISO_SHA256
#   QDISTRO_ISO_TOOL (container image with mksquashfs/unsquashfs)
#   QDISTRO_BUILD_TMP (scratch dir; needs ~15G)
# Requires: xorriso, guestfish, podman (host).
set -euo pipefail

OUT_ISO=${1:?usage: $0 <out.iso>}
here=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=../env.sh
. "$here/../env.sh"

PRODUCT_YAML=$here/out/qdistro.yaml
ICON_SVG=$here/qdistro.svg
[ -f "$PRODUCT_YAML" ] || { echo "$PRODUCT_YAML missing — run render-profile.sh first" >&2; exit 1; }

mkdir -p "$QDISTRO_BUILD_TMP"

# --- locate/fetch the stock ISO -------------------------------------------
STOCK_ISO=${AGAMA_STOCK_ISO:-}
if [ -z "$STOCK_ISO" ]; then
    STOCK_ISO=$QDISTRO_BUILD_TMP/agama-installer-stock.iso
    # The cache is keyed to its source URL — a changed AGAMA_STOCK_ISO_URL
    # must not silently reuse an ISO downloaded from somewhere else.
    if [ ! -f "$STOCK_ISO" ] || \
       [ "$(cat "$STOCK_ISO.url" 2>/dev/null)" != "$AGAMA_STOCK_ISO_URL" ]; then
        echo "==> downloading $AGAMA_STOCK_ISO_URL"
        curl -fL --retry 3 -o "$STOCK_ISO" "$AGAMA_STOCK_ISO_URL"
        echo "$AGAMA_STOCK_ISO_URL" > "$STOCK_ISO.url"
    fi
fi
[ -f "$STOCK_ISO" ] || { echo "stock ISO not found: $STOCK_ISO" >&2; exit 1; }
if [ -n "$AGAMA_STOCK_ISO_SHA256" ]; then
    echo "==> verifying stock ISO sha256"
    echo "$AGAMA_STOCK_ISO_SHA256  $STOCK_ISO" | sha256sum -c -
fi

WORK=$(mktemp -d "$QDISTRO_BUILD_TMP/agama-iso-build.XXXXXX")
trap 'rm -rf "$WORK"' EXIT

echo "==> extracting ISO tree"
mkdir -p "$WORK/cdroot" "$WORK/sq" "$WORK/bootimgs"
xorriso -osirrox on -indev "$STOCK_ISO" -extract / "$WORK/cdroot" >/dev/null 2>&1

# validate the expected layout before touching anything (this script encodes
# the stock image's internal structure — fail loudly on surprises)
for f in LiveOS/squashfs.img boot/x86_64/loader/eltorito.img \
         boot/x86_64/loader/linux boot/x86_64/loader/initrd \
         boot/grub2/grub.cfg LiveOS/.info; do
    [ -f "$WORK/cdroot/$f" ] || {
        echo "unsupported stock ISO layout: missing $f" >&2; exit 1; }
done

echo "==> extracting boot images + squashfs"
xorriso -osirrox on -indev "$STOCK_ISO" \
    -extract_boot_images "$WORK/bootimgs" >/dev/null 2>&1
[ -f "$WORK/bootimgs/gpt_part2_efi.img" ] || {
    echo "no appended EFI partition found in stock ISO" >&2; exit 1; }
podman run --rm -v "$WORK":/x:z "$QDISTRO_ISO_TOOL" \
    unsquashfs -f -d /x/sq /x/cdroot/LiveOS/squashfs.img >/dev/null
[ -f "$WORK/sq/LiveOS/rootfs.img" ] || {
    echo "unsupported squashfs payload: expected LiveOS/rootfs.img" >&2; exit 1; }

echo "==> injecting product into nested ext4 rootfs"
guestfish -a "$WORK/sq/LiveOS/rootfs.img" <<EOF
run
mount /dev/sda /
upload $PRODUCT_YAML /usr/share/agama/products.d/qdistro.yaml
chown 0 0 /usr/share/agama/products.d/qdistro.yaml
chmod 0644 /usr/share/agama/products.d/qdistro.yaml
upload $ICON_SVG /usr/share/agama/web_ui/assets/logos/qdistro.svg
chown 0 0 /usr/share/agama/web_ui/assets/logos/qdistro.svg
chmod 0644 /usr/share/agama/web_ui/assets/logos/qdistro.svg
EOF

echo "==> repacking squashfs (xz, 1M blocks, x86 BCJ — matching kiwi)"
podman run --rm --privileged -v "$WORK":/x:z "$QDISTRO_ISO_TOOL" \
    mksquashfs /x/sq /x/squashfs-new.img \
    -comp xz -b 1M -Xdict-size 1M -Xbcj x86 -noappend -quiet
mv -f "$WORK/squashfs-new.img" "$WORK/cdroot/LiveOS/squashfs.img"

echo "==> branding"
sed -i 's/Install openSUSE (x86_64)/Install qdistro (x86_64)/g; s/Failsafe -- Install openSUSE/Failsafe -- Install qdistro/g' \
    "$WORK/cdroot/boot/grub2/grub.cfg"
# inst.finish=halt on the primary entry: power stays off after install so the
# operator can remove the medium (avoids the cdrom reinstall loop).
sed -i 's|${isoboot} splash=silent ${plymouth} ${live_options}|${isoboot} splash=silent ${plymouth} inst.finish=halt ${live_options}|' \
    "$WORK/cdroot/boot/grub2/grub.cfg"
grep -q 'inst.finish=halt' "$WORK/cdroot/boot/grub2/grub.cfg" || {
    echo "grub.cfg cmdline pattern not found — upstream config changed?" >&2; exit 1; }
sed -i 's/Image profile: openSUSE/Image profile: qdistro (customized from openSUSE profile)/' \
    "$WORK/cdroot/LiveOS/.info"

echo "==> rebuilding ISO"
# NOTE: the live initrd finds the media by volume label
# (/dev/disk/by-label/Install-openSUSE-x86_64, baked in at kiwi time).
# Do NOT rebrand -V or the guest drops to dracut emergency.
xorriso -as mkisofs \
    -V 'Install-openSUSE-x86_64' \
    -R -J -joliet-long \
    --grub2-mbr --interval:local_fs:0s-15s:zero_mbrpt,zero_gpt:"$STOCK_ISO" \
    --protective-msdos-label \
    -partition_cyl_align off \
    -partition_offset 16 \
    -partition_hd_cyl 64 \
    -partition_sec_hd 32 \
    -append_partition 2 28732ac11ff8d211ba4b00a0c93ec93b \
        "$WORK/bootimgs/gpt_part2_efi.img" \
    -appended_part_as_gpt \
    -iso_mbr_part_type a2a0d0ebe5b9334487c068b6b72699c7 \
    --boot-catalog-hide \
    -b '/boot/x86_64/loader/eltorito.img' \
    -no-emul-boot \
    -boot-load-size 4 \
    -boot-info-table \
    --grub2-boot-info \
    -eltorito-alt-boot \
    -e '--interval:appended_partition_2_start_432277s_size_40960d:all::' \
    -no-emul-boot \
    -o "$OUT_ISO" "$WORK/cdroot"

echo "==> done: $OUT_ISO"
