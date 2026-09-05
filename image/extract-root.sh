#!/bin/bash
# extract-root.sh — pull the parts of a built .raw that verify-contents.sh
# inspects into $QDISTRO_BUILD_DIR/extracted (the tree ci/lib/gates/image.sh
# looks for), without booting the image and without guestmount (FUSE is
# unreliable under rootless qemu:///session here; guestfish copy-out is not).
#
# Usage: extract-root.sh [raw] [dest]
#   raw   defaults to $QDISTRO_BUILD_DIR/qdistro.x86_64-0.1.0.raw
#   dest  defaults to $QDISTRO_BUILD_DIR/extracted (wiped first)
# Only the directories the checklist reads are copied (a few hundred MB, not
# the 20 GiB image); add a path here when a checklist row needs one.
set -euo pipefail
BUILD_DIR="${QDISTRO_BUILD_DIR:-/var/tmp/qdistro-build}"
RAW="${1:-$(ls "$BUILD_DIR"/*.raw 2>/dev/null | head -1)}"
DEST="${2:-$BUILD_DIR/extracted}"
[ -n "$RAW" ] && [ -f "$RAW" ] || { echo "extract-root: no such raw: ${RAW:-<none under $BUILD_DIR>}" >&2; exit 2; }
# The destination is WIPED (rm -rf) below, so it must be a child of the
# build dir -- never an arbitrary caller path -- and both paths go into a
# generated guestfish command stream, so refuse characters that could split
# or quote it.
BUILD_DIR="$(realpath -m "$BUILD_DIR")"
DEST="$(realpath -m "$DEST")"
case "$DEST" in
    "$BUILD_DIR"/?*) ;;
    *) echo "extract-root: refusing destination $DEST: must be a child of $BUILD_DIR" >&2; exit 2 ;;
esac
for p in "$RAW" "$DEST"; do
    case "$p" in
        *[[:space:]\"\'\\]*) echo "extract-root: refusing path with whitespace or quotes: $p" >&2; exit 2 ;;
    esac
done
export LIBGUESTFS_BACKEND="${LIBGUESTFS_BACKEND:-direct}"
PATHS=(
    /etc
    /usr/bin/qdgreeter /usr/bin/qdlocker /usr/bin/qterminator /usr/bin/qfileman
    /usr/libexec/qdistro
    /usr/local
    /usr/lib/qdistro /usr/lib/systemd /usr/lib/os-release
    /usr/lib64/weston
    /usr/share/quickshell /usr/share/qdistro /usr/share/polkit-1
    /usr/share/xdg-desktop-portal /usr/share/dbus-1 /usr/share/applications
    /usr/share/metainfo /usr/share/icons/hicolor /usr/share/selinux
    /home/admin/.config /home/admin/weston.ini
    /var/lib/systemd/linger /var/lib/qdistro /var/lib/selinux
    /root/qdistro-src
)
# The copy includes the image's /etc (shadow with the baked test-password
# hash, generated SSH host keys). File modes survive the copy, and the tree
# itself is made private to the invoking user.
rm -rf "$DEST"; mkdir -p "$DEST"; chmod 0700 "$DEST"
# The image root is btrfs with subvolumes (/home, /root, /var, /usr/local,
# ...), so mounting the partition alone shows empty stubs for those; `-i`
# (inspection) mounts the whole fstab. The `-` prefix makes a missing source
# non-fatal, so a path this image legitimately lacks (optional rows) is
# skipped. copy-out wants the destination PARENT to exist.
{
    for p in "${PATHS[@]}"; do
        mkdir -p "$DEST${p%/*}"
        echo "-copy-out $p $DEST${p%/*}"
    done
} | guestfish --ro -a "$RAW" -i
# /root/qdistro-src is only checked for presence of its three dirs; keep it
# small on the host by dropping everything below the second level.
if [ -d "$DEST/root/qdistro-src" ]; then
    find "$DEST/root/qdistro-src" -mindepth 2 -delete 2>/dev/null || true
fi
echo "extract-root: $RAW -> $DEST ($(du -sh "$DEST" | cut -f1))"
