#!/bin/bash
# extract-root.sh — pull the parts of a built .raw that verify-contents.sh
# inspects into $QDISTRO_BUILD_DIR/extracted (the tree ci/lib/gates/image.sh
# looks for), without booting the image and without guestmount (FUSE is
# unreliable under rootless qemu:///session here; guestfish copy-out is not).
#
# Usage: extract-root.sh [raw] [dest]
#   raw   defaults to the SINGLE *.raw under $QDISTRO_BUILD_DIR; with none
#         or several it exits 2 naming them (pass the path explicitly)
#   dest  defaults to $QDISTRO_BUILD_DIR/extracted (wiped first; must be a
#         child of $QDISTRO_BUILD_DIR)
# Only the directories the checklist reads are copied (a few hundred MB, not
# the 20 GiB image); add a path here when a checklist row needs one.
set -euo pipefail
BUILD_DIR="${QDISTRO_BUILD_DIR:-/var/tmp/qdistro-build}"
if [ -n "${1:-}" ]; then
    RAW="$1"
else
    # Exactly one raw may be inferred; two would make "which build?" a
    # lexical accident (round-2 review). Pass the path explicitly otherwise.
    mapfile -t _raws < <(ls "$BUILD_DIR"/*.raw 2>/dev/null)
    case "${#_raws[@]}" in
        1) RAW="${_raws[0]}" ;;
        0) echo "extract-root: no .raw under $BUILD_DIR" >&2; exit 2 ;;
        *) echo "extract-root: ${#_raws[@]} .raw files under $BUILD_DIR; pass the one to inspect explicitly: ${_raws[*]}" >&2; exit 2 ;;
    esac
fi
DEST="${2:-$BUILD_DIR/extracted}"
[ -f "$RAW" ] || { echo "extract-root: no such raw: $RAW" >&2; exit 2; }
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
    /usr/etc/sysconfig/qemu-ga
    /usr/share/quickshell /usr/share/qdistro /usr/share/polkit-1
    /usr/share/xdg-desktop-portal /usr/share/dbus-1 /usr/share/applications
    /usr/share/metainfo /usr/share/icons/hicolor /usr/share/selinux
    /home/admin/.config /home/admin/weston.ini
    /var/lib/systemd/linger /var/lib/qdistro /var/lib/selinux
    /root/qdistro-src
)
# (The chain's sdk step installs qdistro_app under /usr/local/lib/python3.N/
# site-packages -- openSUSE's purelib for non-RPM installs -- so /usr/local
# above already carries the [sdk] row's file.)
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
# /root/qdistro-src is checked for presence of its three dirs, and for the
# targets of the tier-3 spawn/cleanup symlinks (check_link resolves them);
# keep it small on the host by dropping everything else below the second
# level. Add an exemption here when a checklist row resolves into the tree.
if [ -d "$DEST/root/qdistro-src" ]; then
    find "$DEST/root/qdistro-src" -mindepth 2 \
        -not -path '*/qdistro/tier3' -not -path '*/qdistro/tier3/*' \
        -delete 2>/dev/null || true
fi
echo "extract-root: $RAW -> $DEST ($(du -sh "$DEST" | cut -f1))"
